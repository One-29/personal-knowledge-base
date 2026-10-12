"""问答编排（M3）：把检索、生成、会话追问与拒答串成一次问答（04 §5/§6）。

流程（对应 01 §4.2 业务流程图）：
1. 库范围校验（kb_id 指定时校验存在）
2. **会话追问改写**（D5/DR5）：带 session_id 且存在上文时，把指代句改写为自包含问题
3. 问题向量化 → 双通道检索 → RRF 合并（retrieval）
4. **L1 双阈值证据门**：低相关拒答，中间灰区只展示候选，高相关才进入生成
5. LLM 生成带 [n] 标注的回答（灰区不会调用模型）
6. **L2 引用校验**：越界或零引用 → 拒答（严格策略，04 §5）
7. 只记录通过校验的回答供下一轮追问改写；所有拒答都不进入历史；
   组装 Answer（含 citations 供前端溯源）

拒答是正常业务结果（HTTP 200 + refused=true），不是错误。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import (
    crud,
    document_images,
    embedding,
    embedding_profile,
    generation,
    package_storage,
    retrieval,
    session,
)
from .core.config import settings
from .diagnostics import timed_stage
from .embedding import EmbeddingError
from .evidence import (
    AnswerStatus,
    CitationData,
    EvidenceBand,
    EvidenceCandidateData,
    RetrievalEvidenceData,
    SourceSnapshot,
    classify_retrieval_evidence,
)
from .generation import LLMError, LLMProvider
from .evidence.context import ContextIncomplete, expand_evidence
from .models import Chunk, Document
from .session import Turn as SessionTurn

if TYPE_CHECKING:
    from .session import SessionStore

logger = logging.getLogger(__name__)


class KnowledgeBaseNotFound(LookupError):
    """指定的知识库不存在（路由层据此返回 404）。

    用专用异常而非裸 LookupError：后者会把 KeyError 之类的编程错误
    也误判成 404（KeyError 是 LookupError 的子类）。
    """


REFUSAL_EMPTY_KB = "empty_kb"                 # 库为空 / 无任何命中
REFUSAL_LOW_RELEVANCE = "low_relevance"       # 检索相似度低于明确拒答线
REFUSAL_GENERATION_INSUFFICIENT = "generation_insufficient"
REFUSAL_DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
REFUSAL_SOURCE_NOT_SUPPORTED = "source_not_supported"
REFUSAL_SOURCE_CHANGED = "source_changed"
REFUSAL_SOURCE_VALIDATION_FAILED = "source_validation_failed"
REFUSAL_INVALID_SOURCE_SELECTION = "invalid_source_selection"
REFUSAL_SOURCE_CONTEXT_LIMIT = "source_context_limit"
REFUSAL_CONTEXT_INCOMPLETE = "context_incomplete"
REFUSAL_BORDERLINE_RELEVANCE = "borderline_relevance"  # 灰区：有候选但不足以自动回答
REFUSAL_INVALID_CITATION = "invalid_citation"  # 引用越界（幻觉引用）
REFUSAL_NO_CITATION = "no_citation"           # 有实质内容却零引用（不可溯源）
REFUSAL_LLM_UNAVAILABLE = "llm_unavailable"   # 生成服务不可用
REFUSAL_EMBEDDING_UNAVAILABLE = "embedding_unavailable"  # 查询向量服务不可用
REFUSAL_EMBEDDING_MISMATCH = "embedding_mismatch"  # 配置与现有向量空间不一致

REFUSAL_MESSAGES = {
    REFUSAL_CONTEXT_INCOMPLETE: "找到相关内容，但必要的前提、指代或完整原文暂时无法补齐，未生成明确回答。请核对原文、更新索引或缩小问题范围。",
    REFUSAL_EMPTY_KB: "本次检索未取得候选原文，无法回答这个问题。请核对知识库范围或补充相关笔记。",
    REFUSAL_LOW_RELEVANCE: "本次检索到的原文相关性未达到回答条件，无法给出可信回答。",
    REFUSAL_GENERATION_INSUFFICIENT: "已取得相关原文，但提供的内容不足以支持本问题的回答。",
    REFUSAL_DEPENDENCY_UNAVAILABLE: "前序步骤尚未形成可用的正式引用，无法整理该结论的出处。",
    REFUSAL_SOURCE_NOT_SUPPORTED: "前序步骤的原文中没有找到能支持本项内容的出处，需要补充检索或资料。",
    REFUSAL_SOURCE_CHANGED: "原文来源已更新或删除，本次引用无法继续核对，请重新执行。",
    REFUSAL_SOURCE_VALIDATION_FAILED: "原文来源暂时无法核对，本次结论未发布，请稍后重新执行。",
    REFUSAL_INVALID_SOURCE_SELECTION: "来源核对结果未通过校验，请重新执行。",
    REFUSAL_SOURCE_CONTEXT_LIMIT: "待核对的原文超过本次来源核对的长度限制，请按主题拆分任务。",
    REFUSAL_BORDERLINE_RELEVANCE: (
        "找到可能相关的内容，但证据强度处于灰区。系统没有调用回答模型，也没有形成"
        "正式引用；请核对下方候选原文，或换一种更具体的问法。"
    ),
    REFUSAL_INVALID_CITATION: "生成的回答引用了不存在的来源，为保证可信性已拒绝这次回答。",
    REFUSAL_NO_CITATION: "生成的回答没有标注任何来源，无法核对，为保证可信性已拒绝这次回答。",
    REFUSAL_LLM_UNAVAILABLE: "回答生成服务暂时不可用，请稍后重试。",
    REFUSAL_EMBEDDING_UNAVAILABLE: "知识库检索服务暂时不可用，请稍后重试。",
    REFUSAL_EMBEDDING_MISMATCH: (
        "当前向量模型与知识库索引不一致。请改回原模型，或重建全部向量后再提问。"
    ),
}

# 模型自述资料不足与 L1 检索低相关度分别记录。
_INSUFFICIENT_MARKERS = ("资料不足", "无法回答", "没有相关", "未提及", "无法确定")


@dataclass
class AnswerData:
    """服务层返回结构（schemas 层负责序列化）。"""

    question: str
    content: str
    status: AnswerStatus = AnswerStatus.ANSWERED
    citations: list[CitationData] = field(default_factory=list)
    possible_sources: list[EvidenceCandidateData] = field(default_factory=list)
    evidence: RetrievalEvidenceData | None = None
    refused: bool = False
    refusal_reason: str | None = None
    search_query: str | None = None    # 实际用于检索的问题（有会话时可能被改写）
    source_snapshots: list[SourceSnapshot] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        """兼容旧调用方只填写 ``refused``/``refusal_reason`` 的构造方式。"""
        self.status = AnswerStatus(self.status)
        if self.status is not AnswerStatus.ANSWERED:
            self.refused = True
            return
        if not self.refused:
            return
        if self.refusal_reason in {REFUSAL_BORDERLINE_RELEVANCE, REFUSAL_CONTEXT_INCOMPLETE}:
            self.status = AnswerStatus.NEEDS_REVIEW
        elif self.refusal_reason in {
            REFUSAL_INVALID_CITATION,
            REFUSAL_NO_CITATION,
            REFUSAL_INVALID_SOURCE_SELECTION,
        }:
            self.status = AnswerStatus.UNVERIFIED
        elif self.refusal_reason in {
            REFUSAL_LLM_UNAVAILABLE,
            REFUSAL_EMBEDDING_UNAVAILABLE,
            REFUSAL_EMBEDDING_MISMATCH,
            REFUSAL_SOURCE_CHANGED,
            REFUSAL_SOURCE_VALIDATION_FAILED,
            REFUSAL_SOURCE_CONTEXT_LIMIT,
        }:
            self.status = AnswerStatus.ERROR
        else:
            self.status = AnswerStatus.INSUFFICIENT


@dataclass
class AnswerPreparation:
    """已结束数据库读取、可在无连接状态下进入生成阶段的问答快照。"""

    question: str
    search_query: str | None
    candidates: list[retrieval.RetrievedChunk]
    candidate_citations: list[CitationData]
    session_id: str | None
    session_store: SessionStore
    retrieval_evidence: RetrievalEvidenceData | None = None
    terminal_result: AnswerData | None = None
    candidate_source_snapshots: list[SourceSnapshot] = field(default_factory=list)
    validate_sources: Callable[[list[SourceSnapshot]], set[int]] | None = field(default=None, repr=False)


def answer_question(
    db: Session,
    question: str,
    kb_id: int | None,
    session_id: str | None = None,
    history: list[tuple[str, str]] | None = None,
    llm: LLMProvider | None = None,
    session_store: "SessionStore | None" = None,
) -> AnswerData:
    """一次完整问答（含会话追问改写与两级拒答）。

    :param session_id: 进程内会话 id（D5）
    :param history: 请求携带的对话历史 [(question, answer), ...]，优先于进程内会话。
        前端把会话持久化在本地并随请求回传，服务端因此保持无状态——
        刷新页面或重启服务都不会丢失追问上下文（也不必把会话落库）。
    """
    prepared = prepare_answer(
        db,
        question,
        kb_id,
        session_id=session_id,
        history=history,
        llm=llm,
        session_store=session_store,
    )
    return complete_prepared_answer(prepared, llm=llm)


def prepare_answer(
    db: Session,
    question: str,
    kb_id: int | None,
    session_id: str | None = None,
    history: list[tuple[str, str]] | None = None,
    llm: LLMProvider | None = None,
    session_store: SessionStore | None = None,
) -> AnswerPreparation:
    """完成生成前的校验与检索，并把 ORM 数据复制为普通对象。

    返回后数据库事务已经回滚。同步问答和 SSE 问答共用这条路径，避免两种
    传输方式在拒答门、追问改写或引用候选上产生行为漂移。
    """
    store = session_store or session.store
    scope = kb_id if kb_id is not None else "all"
    with timed_stage("ask.validate_scope", kb_id=scope):
        validate_kb(db, kb_id)
    try:
        with timed_stage("ask.embedding_profile", kb_id=scope):
            embedding_profile.ensure_embedding_profile(db)
    except embedding_profile.EmbeddingProfileError as exc:
        logger.warning("embedding 模型指纹不兼容: %s", exc)
        return _terminal_preparation(
            question=question,
            search_query=question,
            session_id=session_id,
            store=store,
            result=_refuse(question, REFUSAL_EMBEDDING_MISMATCH),
        )

    if history:
        context = [SessionTurn(question=q, answer=a) for q, a in history]
    else:
        context = store.history(session_id) if session_id else []

    # 追问改写（04 DR5）：失败退化为原问题，不阻断检索
    search_query = question
    if context:
        try:
            with timed_stage("ask.query_rewrite", history_turns=len(context)):
                search_query = generation.rewrite_query(question, context, provider=llm)
        except LLMError as exc:
            logger.warning("追问改写失败，退化为原问题检索: %s", exc)

    try:
        with timed_stage("ask.query_embedding", query_chars=len(search_query)):
            query_vector = _embed_query(search_query)
    except EmbeddingError as exc:
        logger.warning("查询向量化失败: %s", exc)
        return _terminal_preparation(
            question=question,
            search_query=search_query,
            session_id=session_id,
            store=store,
            result=_refuse(question, REFUSAL_EMBEDDING_UNAVAILABLE),
        )
    # 本服务使用只读会话：把候选与标题都复制成普通数据后结束事务。
    # 生成阶段不再访问 ORM，避免长时间等待模型时占用连接池。
    with timed_stage(
        "ask.retrieval",
        kb_id=scope,
        top_k=settings.retrieval_top_k,
    ):
        try:
            candidates = retrieval.retrieve(
                db, search_query, query_vector, kb_id, top_k=settings.retrieval_top_k
            )
            candidate_sources = _build_source_snapshots(
                db,
                list(enumerate(candidates, start=1)),
            )
            candidate_citations = [source.citation for source in candidate_sources]
        finally:
            db.rollback()

    evidence = classify_retrieval_evidence(
        candidates,
        refusal_threshold=settings.refusal_similarity_threshold,
        answer_threshold=settings.answer_similarity_threshold,
    )

    # L1-a：无候选，或最高相似度低于明确拒答线。
    if evidence.band is EvidenceBand.INSUFFICIENT:
        reason = REFUSAL_EMPTY_KB if not candidates else REFUSAL_LOW_RELEVANCE
        logger.info(
            "L1 证据不足：最高相似度=%s，拒答线=%.2f",
            evidence.max_vector_similarity,
            evidence.refusal_threshold,
        )
        return _terminal_preparation(
            question=question,
            search_query=search_query,
            session_id=session_id,
            store=store,
            result=_refuse(question, reason, evidence=evidence),
        )

    # L1-b：候选处于双阈值之间，或只有关键词分数。保留候选供人工核对，
    # 但不调用生成模型，也不把候选冒充为正式引用。
    if evidence.band is EvidenceBand.BORDERLINE:
        possible_sources = _build_possible_sources(candidates, candidate_citations)
        logger.info(
            "L1 灰区：最高相似度=%s，区间=[%.2f, %.2f)",
            evidence.max_vector_similarity,
            evidence.refusal_threshold,
            evidence.answer_threshold,
        )
        return _terminal_preparation(
            question=question,
            search_query=search_query,
            session_id=session_id,
            store=store,
            candidates=candidates,
            candidate_citations=candidate_citations,
            result=_refuse(
                question,
                REFUSAL_BORDERLINE_RELEVANCE,
                status=AnswerStatus.NEEDS_REVIEW,
                evidence=evidence,
                possible_sources=possible_sources,
            ),
        )

    # 只在原始检索证据足够时补全；补充块不重新参与 L1 或 RRF。
    try:
        with timed_stage("ask.context", anchors=len(candidates)):
            expanded, dependencies = expand_evidence(
                db, candidates, max_chars=settings.answer_context_max_chars,
                neighbor_chars=settings.answer_context_neighbor_chars,
            )
            expanded_sources = _build_source_snapshots(db, list(enumerate(expanded, start=1)))
            for source in expanded_sources:
                source.citation.context_chunk_ids = dependencies.get(source.citation.chunk_id, ())
            if len(generation._source_context(expanded, [s.citation for s in expanded_sources])) > settings.answer_context_max_chars:
                raise ContextIncomplete("含来源元数据的完整证据超过回答预算")
    except ContextIncomplete as exc:
        logger.info("context_incomplete: %s", exc)
        return _terminal_preparation(
            question=question, search_query=search_query, session_id=session_id, store=store,
            candidates=candidates, candidate_citations=candidate_citations,
            result=_refuse(question, REFUSAL_CONTEXT_INCOMPLETE, status=AnswerStatus.NEEDS_REVIEW,
                           evidence=evidence, possible_sources=_build_possible_sources(candidates, candidate_citations)),
        )
    finally:
        db.rollback()

    return AnswerPreparation(
        question=question,
        search_query=search_query,
        candidates=expanded,
        candidate_citations=[source.citation for source in expanded_sources],
        session_id=session_id,
        session_store=store,
        retrieval_evidence=evidence,
        candidate_source_snapshots=expanded_sources,
        validate_sources=lambda sources: invalid_source_chunk_ids(db, sources, kb_id),
    )


def complete_prepared_answer(
    prepared: AnswerPreparation,
    llm: LLMProvider | None = None,
) -> AnswerData:
    """同步生成并完成 L2 校验；保留原 `/ask` 的完整响应契约。"""
    if prepared.terminal_result is not None:
        return finish_prepared_answer(prepared, prepared.terminal_result)

    # 生成
    try:
        with timed_stage("ask.answer_generation", candidates=len(prepared.candidates)):
            content = generation.generate_answer(
                prepared.question,
                prepared.candidates,
                provider=llm,
                citations=prepared.candidate_citations,
            )
    except LLMError as exc:
        logger.warning("生成失败: %s", exc)
        return refuse_prepared_answer(prepared, REFUSAL_LLM_UNAVAILABLE)

    return finish_generated_answer(prepared, content)


def finish_generated_answer(prepared: AnswerPreparation, content: str) -> AnswerData:
    """对完整模型输出执行 L2 校验，并且只提交经过校验的最终结果。"""
    content = content.strip()
    if content.startswith("上下文不足"):
        return refuse_prepared_answer(prepared, REFUSAL_CONTEXT_INCOMPLETE)

    # L2：引用越界校验（纯规则，必执行）
    invalid = generation.find_invalid_citations(
        content,
        provided_count=len(prepared.candidates),
    )
    if invalid:
        logger.warning(
            "L2 拒答：越界引用 %s（提供 %d 块）",
            invalid,
            len(prepared.candidates),
        )
        return refuse_prepared_answer(prepared, REFUSAL_INVALID_CITATION)

    # L2-b：零引用（有实质内容却无来源）→ 不可溯源，同样拒答（可信优先）
    cited_indexes = generation.parse_citations(content)
    if not cited_indexes:
        reason = (
            REFUSAL_CONTEXT_INCOMPLETE if "上下文不足" in content else
            REFUSAL_GENERATION_INSUFFICIENT
            if any(marker in content for marker in _INSUFFICIENT_MARKERS)
            else REFUSAL_NO_CITATION
        )
        logger.info("L2 拒答：回答无有效引用（reason=%s）", reason)
        return refuse_prepared_answer(prepared, reason)

    # 被引用片段依赖的前提同属本次已用证据，后续出处步骤必须保留它们。
    cited_ids = {c.chunk_id for c in prepared.candidate_citations if c.index in cited_indexes}
    while True:
        required = {cid for c in prepared.candidate_citations if c.chunk_id in cited_ids for cid in c.context_chunk_ids}
        if required <= cited_ids:
            break
        if not required <= {c.chunk_id for c in prepared.candidate_citations}:
            return refuse_prepared_answer(prepared, REFUSAL_CONTEXT_INCOMPLETE)
        cited_ids.update(required)
    cited_indexes = [c.index for c in prepared.candidate_citations if c.chunk_id in cited_ids]

    result = AnswerData(
        question=prepared.question,
        content=content,
        citations=[
            citation
            for citation in prepared.candidate_citations
            if citation.index in cited_indexes
        ],
        source_snapshots=[
            source for source in prepared.candidate_source_snapshots
            if source.citation.index in cited_indexes
        ],
    )
    if prepared.validate_sources is not None:
        try:
            if prepared.validate_sources(result.source_snapshots):
                return refuse_prepared_answer(prepared, REFUSAL_SOURCE_CHANGED)
        except Exception:
            logger.warning("回答发布前来源核对失败", exc_info=True)
            return refuse_prepared_answer(prepared, REFUSAL_SOURCE_VALIDATION_FAILED)
    return finish_prepared_answer(prepared, result)


def refuse_prepared_answer(prepared: AnswerPreparation, reason: str) -> AnswerData:
    """把生成阶段故障或 L2 失败转换为与同步接口一致的可信拒答。"""
    return finish_prepared_answer(prepared, _refuse(prepared.question, reason))


def finish_prepared_answer(
    prepared: AnswerPreparation,
    result: AnswerData,
) -> AnswerData:
    """完成会话记录；流式路径只在最终结果形成后调用。"""
    if result.evidence is None:
        result.evidence = prepared.retrieval_evidence
    return _finish(
        prepared.session_store,
        prepared.session_id,
        result,
        prepared.search_query,
    )


def _terminal_preparation(
    *,
    question: str,
    search_query: str | None,
    session_id: str | None,
    store: SessionStore,
    result: AnswerData,
    candidates: list[retrieval.RetrievedChunk] | None = None,
    candidate_citations: list[CitationData] | None = None,
) -> AnswerPreparation:
    return AnswerPreparation(
        question=question,
        search_query=search_query,
        candidates=candidates or [],
        candidate_citations=candidate_citations or [],
        session_id=session_id,
        session_store=store,
        retrieval_evidence=result.evidence,
        terminal_result=result,
    )


def validate_kb(db: Session, kb_id: int | None) -> None:
    """短只读事务校验库范围，在规划/改写/向量化前归还连接。

    问答与工作流传入的会话不得包含待提交写入；事务边界由服务层管理。
    """
    try:
        if kb_id is not None and crud.get_kb(db, kb_id) is None:
            raise KnowledgeBaseNotFound("知识库不存在")
    finally:
        db.rollback()


def _finish(
    store: "SessionStore",
    session_id: str | None,
    result: AnswerData,
    search_query: str | None,
) -> AnswerData:
    """只记录通过两级证据校验的回答，拒答不能成为下一轮的事实前提。"""
    result.search_query = search_query
    if session_id and result.status is AnswerStatus.ANSWERED:
        store.append(session_id, SessionTurn(question=result.question, answer=result.content))
    return result


def invalid_source_chunk_ids(
    db: Session,
    sources: list[SourceSnapshot],
    kb_id: int | None,
) -> set[int]:
    """批量核对已发布内容与片段，返回失效/越范围编号；读取后归还连接。"""
    if not sources:
        return set()
    ids = {source.citation.chunk_id for source in sources}
    try:
        current = {
            row.id: row for row in db.execute(
                select(
                    Chunk.id, Chunk.doc_id, Chunk.kb_id, Chunk.content,
                    Chunk.char_start, Chunk.char_end,
                    Document.content_hash, Document.kb_id.label("document_kb_id"),
                ).join(Document, Document.id == Chunk.doc_id).where(Chunk.id.in_(ids))
            ).all()
        }
        invalid: set[int] = set()
        for source in sources:
            citation = source.citation
            row = current.get(citation.chunk_id)
            if row is None or (
                row.doc_id != citation.doc_id or row.kb_id != source.kb_id
                or row.document_kb_id != source.kb_id
                or (kb_id is not None and row.kb_id != kb_id)
                or row.content_hash != source.indexed_content_hash
                or row.content != citation.chunk_text
                or row.char_start != citation.char_start or row.char_end != citation.char_end
            ):
                invalid.add(citation.chunk_id)
        logger.info("source_validation sources=%s invalid=%s", len(ids), len(invalid))
        return invalid
    finally:
        db.rollback()


def answer_from_sources(
    db: Session,
    question: str,
    kb_id: int | None,
    sources: list[SourceSnapshot],
    *,
    source_scope: str = "topic",
    llm: LLMProvider | None = None,
) -> AnswerData:
    """核对前序正式引用的出处，复用 L2；不计算查询向量或沿用旧相似度。"""
    validate_kb(db, kb_id)
    if source_scope not in {"topic", "all"}:
        raise ValueError("source_scope 必须是 topic 或 all")
    if not sources:
        return _refuse(question, REFUSAL_DEPENDENCY_UNAVAILABLE)
    if invalid_source_chunk_ids(db, sources, kb_id):
        return _refuse(question, REFUSAL_SOURCE_CHANGED)
    unique = {source.citation.chunk_id: source for source in sources}
    normalized = [
        replace(source, citation=replace(source.citation, index=index))
        for index, source in enumerate(unique.values(), start=1)
    ]
    citations = [source.citation for source in normalized]
    chunks = [
        retrieval.RetrievedChunk(
            chunk_id=c.chunk_id, doc_id=c.doc_id, content=c.chunk_text,
            char_start=c.char_start, char_end=c.char_end, rrf_score=0.0,
            vector_similarity=None, vector_rank=None, keyword_rank=None,
        ) for c in citations
    ]
    if source_scope == "all":
        indexes = list(range(1, len(citations) + 1))
    else:
        if sum(len(c.chunk_text) + len(c.doc_title) for c in citations) > settings.workflow_source_max_chars:
            return _refuse(question, REFUSAL_SOURCE_CONTEXT_LIMIT)
        try:
            with timed_stage("ask.source_selection", candidates=len(citations)):
                indexes = generation.select_source_indexes(question, chunks, citations, provider=llm)
        except LLMError:
            return _refuse(question, REFUSAL_LLM_UNAVAILABLE)
        except generation.InvalidSourceSelection:
            return _refuse(question, REFUSAL_INVALID_SOURCE_SELECTION)
    if not indexes:
        return _refuse(question, REFUSAL_SOURCE_NOT_SUPPORTED)
    lines: list[str] = []
    for index in indexes:
        citation = citations[index - 1]
        title = " ".join(citation.doc_title.split()).replace("[", "［").replace("]", "］")
        lines.append(
            f"- {title}，原文字符位置 {citation.char_start}–{citation.char_end}。[{index}]"
        )
    prepared = AnswerPreparation(
        question=question, search_query=None, candidates=chunks,
        candidate_citations=citations, session_id=None, session_store=session.store,
        candidate_source_snapshots=normalized,
    )
    result = finish_generated_answer(prepared, "\n".join(lines))
    if invalid_source_chunk_ids(db, result.source_snapshots, kb_id):
        return _refuse(question, REFUSAL_SOURCE_CHANGED)
    return result


def _build_possible_sources(
    candidates: list[retrieval.RetrievedChunk],
    snapshots: list[CitationData],
    *,
    limit: int = 3,
) -> list[EvidenceCandidateData]:
    """把灰区前几名候选复制成可脱离数据库展示的来源快照。"""
    by_index = {snapshot.index: snapshot for snapshot in snapshots}
    result: list[EvidenceCandidateData] = []
    for index, candidate in enumerate(candidates[:limit], start=1):
        snapshot = by_index.get(index)
        if snapshot is None:
            continue
        result.append(
            EvidenceCandidateData(
                index=index,
                chunk_id=snapshot.chunk_id,
                doc_id=snapshot.doc_id,
                doc_title=snapshot.doc_title,
                chunk_text=snapshot.chunk_text,
                char_start=snapshot.char_start,
                char_end=snapshot.char_end,
                vector_similarity=candidate.vector_similarity,
                rrf_score=candidate.rrf_score,
                vector_rank=candidate.vector_rank,
                keyword_rank=candidate.keyword_rank,
                images=list(snapshot.images),
            )
        )
    return result


def _build_source_snapshots(
    db: Session,
    cited: list[tuple[int, retrieval.RetrievedChunk]],
) -> list[SourceSnapshot]:
    """批量复制文档元数据与已发布版本，生成阶段不持有 ORM 对象。"""
    if not cited:
        return []
    doc_ids = {chunk.doc_id for _, chunk in cited}
    documents = {
        doc_id: (title, file_path, kb_id, content_hash)
        for doc_id, title, file_path, kb_id, content_hash in db.execute(
            select(
                Document.id, Document.title, Document.file_path,
                Document.kb_id, Document.content_hash,
            ).where(
                Document.id.in_(doc_ids)
            )
        ).all()
    }
    result: list[SourceSnapshot] = []
    image_cache: dict[tuple[int, str], list[document_images.ImageReferenceData]] = {}
    for index, chunk in cited:
        if chunk.doc_id not in documents:
            raise RuntimeError("引用文档在检索期间发生变化")
        title, source_path, kb_id, content_hash = documents[chunk.doc_id]
        try:
            key = (chunk.doc_id, source_path)
            if source_path and key not in image_cache:
                image_cache[key] = document_images.images_for_source(
                    chunk.doc_id,
                    source_path,
                )
            images = [
                image
                for image in image_cache.get(key, [])
                if image.char_end > chunk.char_start and image.char_start < chunk.char_end
            ]
        except (package_storage.StorageIntegrityError, ValueError):
            image_cache[key] = []
            logger.warning(
                "引用图片清单读取失败: doc_id=%s chunk_id=%s",
                chunk.doc_id,
                chunk.chunk_id,
                exc_info=True,
            )
            images = []
        citation = CitationData(
            index=index,
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            doc_title=title,
            chunk_text=chunk.content,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            images=images,
        )
        result.append(SourceSnapshot(
            citation=citation, kb_id=kb_id, indexed_content_hash=content_hash,
        ))
    return result


def _refuse(
    question: str,
    reason: str,
    *,
    status: AnswerStatus | None = None,
    evidence: RetrievalEvidenceData | None = None,
    possible_sources: list[EvidenceCandidateData] | None = None,
) -> AnswerData:
    if status is None:
        if reason == REFUSAL_CONTEXT_INCOMPLETE:
            status = AnswerStatus.NEEDS_REVIEW
        elif reason in {
            REFUSAL_LLM_UNAVAILABLE,
            REFUSAL_EMBEDDING_UNAVAILABLE,
            REFUSAL_EMBEDDING_MISMATCH,
            REFUSAL_SOURCE_CHANGED,
            REFUSAL_SOURCE_VALIDATION_FAILED,
            REFUSAL_SOURCE_CONTEXT_LIMIT,
        }:
            status = AnswerStatus.ERROR
        elif reason in {
            REFUSAL_INVALID_CITATION, REFUSAL_NO_CITATION,
            REFUSAL_INVALID_SOURCE_SELECTION,
        }:
            status = AnswerStatus.UNVERIFIED
        else:
            status = AnswerStatus.INSUFFICIENT
    return AnswerData(
        question=question,
        content=REFUSAL_MESSAGES[reason],
        status=status,
        possible_sources=possible_sources or [],
        evidence=evidence,
        refused=True,
        refusal_reason=reason,
    )


def _embed_query(question: str) -> list[float]:
    """问题向量化：与入库使用同一模型（04 DR2 全项目模型唯一）。

    经 embedding 模块动态调用（而非模块级导入函数）：保证测试替换 provider 时
    只需改一处，也避免各调用方持有独立的函数引用。
    """
    return embedding.get_embedding_provider().embed_texts([question])[0]
