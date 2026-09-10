"""问答编排（M3）：把检索、生成、拒答串成一次问答（04 §5/§6）。

流程（对应 01 §4.2 业务流程图）：
1. 库范围校验（kb_id 指定时校验存在）
2. 问题向量化 → 双通道检索 → RRF 合并（retrieval）
3. **L1 拒答**：无候选（库空/无命中）或最高向量相似度 < τ → 拒答
4. LLM 生成带 [n] 标注的回答
5. **L2 引用校验**：越界引用 → 拒答（严格策略，04 §5）
6. 组装 Answer（含 citations 供前端溯源）

拒答是正常业务结果（HTTP 200 + refused=true），不是错误。
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from . import crud, generation, retrieval
from .core.config import settings
from .embedding import get_embedding_provider
from .generation import LLMError, LLMProvider

logger = logging.getLogger(__name__)

REFUSAL_EMPTY_KB = "empty_kb"                 # 库为空 / 无任何命中
REFUSAL_LOW_RELEVANCE = "low_relevance"       # 最高相似度低于阈值 τ / 模型自述资料不足
REFUSAL_INVALID_CITATION = "invalid_citation"  # 引用越界（幻觉引用）
REFUSAL_NO_CITATION = "no_citation"           # 有实质内容却零引用（不可溯源）
REFUSAL_LLM_UNAVAILABLE = "llm_unavailable"   # 生成服务不可用

REFUSAL_MESSAGES = {
    REFUSAL_EMPTY_KB: "知识库里还没有相关内容，无法回答这个问题。可以先导入相关笔记再试。",
    REFUSAL_LOW_RELEVANCE: "知识库中没有找到与该问题足够相关的内容，无法给出可信回答。",
    REFUSAL_INVALID_CITATION: "生成的回答引用了不存在的来源，为保证可信性已拒绝这次回答。",
    REFUSAL_NO_CITATION: "生成的回答没有标注任何来源，无法核对，为保证可信性已拒绝这次回答。",
    REFUSAL_LLM_UNAVAILABLE: "回答生成服务暂时不可用，请稍后重试。",
}

# 模型自述"资料不足"的常见说法（此时归入低相关度拒答，文案更贴切）
_INSUFFICIENT_MARKERS = ("资料不足", "无法回答", "没有相关", "未提及", "无法确定")


@dataclass
class CitationData:
    """一条引用：回答中 [n] 对应的块与来源文档（供前端溯源展示）。"""

    index: int          # 在回答中的 [n] 序号（= 候选列表中的位置）
    chunk_id: int
    doc_id: int
    doc_title: str
    chunk_text: str
    char_start: int
    char_end: int


@dataclass
class AnswerData:
    """服务层返回结构（schemas 层负责序列化）。"""

    question: str
    content: str
    citations: list[CitationData] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None


def answer_question(
    db: Session,
    question: str,
    kb_id: int | None,
    llm: LLMProvider | None = None,
) -> AnswerData:
    """一次完整问答（含两级拒答）。"""
    if kb_id is not None and crud.get_kb(db, kb_id) is None:
        raise LookupError("知识库不存在")

    query_vector = _embed_query(question)
    candidates = retrieval.retrieve(
        db, question, query_vector, kb_id, top_k=settings.retrieval_top_k
    )

    # L1-a：无候选
    if not candidates:
        return _refuse(question, REFUSAL_EMPTY_KB)

    # L1-b：最高向量相似度低于阈值 τ（素材与问题不够相关）
    similarities = [c.vector_similarity for c in candidates if c.vector_similarity is not None]
    if similarities and max(similarities) < settings.refusal_similarity_threshold:
        logger.info("L1 拒答：最高相似度 %.3f < τ %.2f", max(similarities),
                    settings.refusal_similarity_threshold)
        return _refuse(question, REFUSAL_LOW_RELEVANCE)

    # 生成
    try:
        content = generation.generate_answer(question, candidates, provider=llm)
    except LLMError as exc:
        logger.warning("生成失败: %s", exc)
        return _refuse(question, REFUSAL_LLM_UNAVAILABLE)

    # L2：引用越界校验（纯规则，必执行）
    invalid = generation.find_invalid_citations(content, provided_count=len(candidates))
    if invalid:
        logger.warning("L2 拒答：越界引用 %s（提供 %d 块）", invalid, len(candidates))
        return _refuse(question, REFUSAL_INVALID_CITATION)

    # L2-b：零引用（有实质内容却无来源）→ 不可溯源，同样拒答（可信优先）
    cited_indexes = generation.parse_citations(content)
    if not cited_indexes:
        reason = (
            REFUSAL_LOW_RELEVANCE
            if any(marker in content for marker in _INSUFFICIENT_MARKERS)
            else REFUSAL_NO_CITATION
        )
        logger.info("L2 拒答：回答无有效引用（reason=%s）", reason)
        return _refuse(question, reason)

    cited = [(i, c) for i, c in enumerate(candidates, start=1) if i in cited_indexes]
    return AnswerData(
        question=question,
        content=content,
        citations=_build_citations(db, cited),
    )


def _build_citations(
    db: Session,
    cited: list[tuple[int, retrieval.RetrievedChunk]],
) -> list[CitationData]:
    """组装引用：批量取文档标题（避免逐条查询）。"""
    from sqlalchemy import select

    from .models import Document

    doc_ids = {chunk.doc_id for _, chunk in cited}
    titles = {
        doc_id: title
        for doc_id, title in db.execute(
            select(Document.id, Document.title).where(Document.id.in_(doc_ids))
        ).all()
    }
    return [
        CitationData(
            index=index,
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            doc_title=titles.get(chunk.doc_id, ""),
            chunk_text=chunk.content,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
        )
        for index, chunk in cited
    ]


def _refuse(question: str, reason: str) -> AnswerData:
    return AnswerData(
        question=question,
        content=REFUSAL_MESSAGES[reason],
        refused=True,
        refusal_reason=reason,
    )


def _embed_query(question: str) -> list[float]:
    """问题向量化：与入库使用同一模型（04 DR2 全项目模型唯一）。"""
    return get_embedding_provider().embed_texts([question])[0]
