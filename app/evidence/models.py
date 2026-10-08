"""与传输层无关的证据领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..document_images import ImageReferenceData


class AnswerStatus(StrEnum):
    """一次问答的最终业务状态。"""

    ANSWERED = "answered"
    NEEDS_REVIEW = "needs_review"
    INSUFFICIENT = "insufficient"
    UNVERIFIED = "unverified"
    ERROR = "error"


class EvidenceBand(StrEnum):
    """L1 检索证据所在区间。"""

    SUFFICIENT = "sufficient"
    BORDERLINE = "borderline"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class RetrievalEvidenceData:
    """一次检索判定的可解释摘要。"""

    band: EvidenceBand
    candidate_count: int
    max_vector_similarity: float | None
    refusal_threshold: float
    answer_threshold: float


@dataclass
class CitationData:
    """正式回答中 ``[n]`` 对应的、已经通过校验的来源快照。"""

    index: int
    chunk_id: int
    doc_id: int
    doc_title: str
    chunk_text: str
    char_start: int
    char_end: int
    images: list[ImageReferenceData] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceCandidateData:
    """灰区中供人工核对的候选来源；它不是正式引用。"""

    index: int
    chunk_id: int
    doc_id: int
    doc_title: str
    chunk_text: str
    char_start: int
    char_end: int
    vector_similarity: float | None
    rrf_score: float
    vector_rank: int | None
    keyword_rank: int | None
    images: list[ImageReferenceData] = field(default_factory=list)
