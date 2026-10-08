"""问答证据状态、来源快照与灰区判定的稳定公共接口。"""

from .decision import classify_retrieval_evidence, classify_similarity
from .models import (
    AnswerStatus,
    CitationData,
    EvidenceBand,
    EvidenceCandidateData,
    RetrievalEvidenceData,
)

__all__ = [
    "AnswerStatus",
    "CitationData",
    "EvidenceBand",
    "EvidenceCandidateData",
    "RetrievalEvidenceData",
    "classify_retrieval_evidence",
    "classify_similarity",
]
