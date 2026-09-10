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
REFUSAL_LOW_RELEVANCE = "low_relevance"       # 最高相似度低于阈值 τ
REFUSAL_INVALID_CITATION = "invalid_citation"  # 引用越界（幻觉引用）
REFUSAL_LLM_UNAVAILABLE = "llm_unavailable"   # 生成服务不可用

REFUSAL_MESSAGES = {
    REFUSAL_EMPTY_KB: "知识库里还没有相关内容，无法回答这个问题。可以先导入相关笔记再试。",
    REFUSAL_LOW_RELEVANCE: "知识库中没有找到与该问题足够相关的内容，无法给出可信回答。",
    REFUSAL_INVALID_CITATION: "生成的回答引用了不存在的来源，为保证可信性已拒绝这次回答。",
    REFUSAL_LLM_UNAVAILABLE: "回答生成服务暂时不可用，请稍后重试。",
}


@dataclass
class AnswerData:
    """服务层返回结构（schemas 层负责序列化）。"""

    question: str
    content: str
    citations: list[retrieval.RetrievedChunk] = field(default_factory=list)
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

    cited_indexes = generation.parse_citations(content)
    cited_chunks = [c for i, c in enumerate(candidates, start=1) if i in cited_indexes]
    return AnswerData(question=question, content=content, citations=cited_chunks)


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
