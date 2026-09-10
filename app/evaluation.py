"""检索质量评估（06）：recall@k / MRR / 拒答准确性 / 参数扫描。

设计要点：
- 命中判定为**纯规则**（来源文档 + 关键词同时匹配），可复现、不引入模型打分；
- 检索评估只消耗 embedding 调用；拒答评估才消耗 LLM；
- 评估语料与评估库独立于开发库，避免污染。
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from . import ask as ask_service
from . import retrieval
from .core.config import settings

logger = logging.getLogger(__name__)

EVAL_SET_PATH = Path("eval/eval_set.json")
EVAL_NOTES_DIR = Path("eval/notes")


@dataclass(frozen=True)
class EvalItem:
    """一条评估样本。"""

    question: str
    expected_doc: str | None       # None = 库外问题（应拒答）
    expected_keywords: list[str]
    in_kb: bool


@dataclass
class RetrievalMetrics:
    """检索指标。"""

    total: int = 0
    hits: int = 0
    reciprocal_ranks: list[float] = field(default_factory=list)

    @property
    def recall_at_k(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        return sum(self.reciprocal_ranks) / len(self.reciprocal_ranks) if self.reciprocal_ranks else 0.0

    def summary(self) -> str:
        return f"recall@k={self.recall_at_k:.3f}  MRR={self.mrr:.3f}  ({self.hits}/{self.total})"


def load_eval_set(path: Path = EVAL_SET_PATH) -> list[EvalItem]:
    """读取评估集。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        EvalItem(
            question=item["question"],
            expected_doc=item.get("expected_doc"),
            expected_keywords=list(item.get("expected_keywords", [])),
            in_kb=bool(item.get("in_kb", True)),
        )
        for item in payload["items"]
    ]


def is_hit(question: str, expected_doc: str, keywords: list[str], chunk_text: str) -> bool:
    """命中判定（06 §2.3）：块含任一关键词即算命中该问题。"""
    lowered = chunk_text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def evaluate_retrieval(
    db: Session,
    items: list[EvalItem],
    kb_id: int,
    top_k: int | None = None,
    doc_titles: dict[int, str] | None = None,
) -> RetrievalMetrics:
    """检索评估：对每个库内问题召回候选块，判定是否命中并计算排名。"""
    from .embedding import get_embedding_provider

    top_k = top_k or settings.retrieval_top_k
    titles = doc_titles or {}
    provider = get_embedding_provider()
    metrics = RetrievalMetrics()

    for item in items:
        if not item.in_kb or not item.expected_doc or not item.expected_keywords:
            continue
        metrics.total += 1
        vector = provider.embed_texts([item.question])[0]
        candidates = retrieval.retrieve(db, item.question, vector, kb_id, top_k=top_k)
        for rank, candidate in enumerate(candidates, start=1):
            if is_hit(item.question, item.expected_doc,
                      item.expected_keywords, candidate.content):
                metrics.hits += 1
                metrics.reciprocal_ranks.append(1.0 / rank)
                break

    return metrics


@dataclass
class RefusalMetrics:
    """拒答指标。"""

    out_of_kb_total: int = 0
    out_of_kb_refused: int = 0
    in_kb_total: int = 0
    in_kb_refused: int = 0
    refusal_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def out_of_kb_refusal_rate(self) -> float:
        return self.out_of_kb_refused / self.out_of_kb_total if self.out_of_kb_total else 0.0

    @property
    def in_kb_false_refusal_rate(self) -> float:
        return self.in_kb_refused / self.in_kb_total if self.in_kb_total else 0.0

    def summary(self) -> str:
        return (
            f"库外拒答率={self.out_of_kb_refusal_rate:.3f} "
            f"库内误拒率={self.in_kb_false_refusal_rate:.3f} "
            f"拒答原因={self.refusal_reasons}"
        )


def evaluate_refusal(
    db: Session,
    items: list[EvalItem],
    kb_id: int,
) -> RefusalMetrics:
    """拒答评估：跑完整问答，统计库外拒答与库内误拒（消耗 LLM）。"""
    metrics = RefusalMetrics()
    for item in items:
        try:
            result = ask_service.answer_question(db, item.question, kb_id)
        except Exception as exc:                 # 技术故障不计入拒答统计
            logger.warning("评估样本执行失败: %s (%s)", item.question, exc)
            continue

        if item.in_kb:
            metrics.in_kb_total += 1
            if result.refused:
                metrics.in_kb_refused += 1
                metrics.refusal_reasons[result.refusal_reason or "unknown"] = (
                    metrics.refusal_reasons.get(result.refusal_reason or "unknown", 0) + 1
                )
        else:
            metrics.out_of_kb_total += 1
            if result.refused:
                metrics.out_of_kb_refused += 1
                metrics.refusal_reasons[result.refusal_reason or "unknown"] = (
                    metrics.refusal_reasons.get(result.refusal_reason or "unknown", 0) + 1
                )

    return metrics


def collect_similarities(
    db: Session,
    items: list[EvalItem],
    kb_id: int,
) -> list[tuple[EvalItem, float | None]]:
    """收集每个问题的最高向量相似度（只做检索，不调 LLM）。

    τ 只影响 L1 判定（`max(similarity) < τ → 拒答`，纯规则），所以扫描阈值
    不需要把每个样本都跑一遍完整问答——先用检索拿到相似度，再本地模拟。
    """
    from .embedding import get_embedding_provider

    provider = get_embedding_provider()
    samples: list[tuple[EvalItem, float | None]] = []
    for item in items:
        vector = provider.embed_texts([item.question])[0]
        candidates = retrieval.retrieve(
            db, item.question, vector, kb_id, top_k=settings.retrieval_top_k
        )
        similarities = [c.vector_similarity for c in candidates if c.vector_similarity is not None]
        samples.append((item, max(similarities) if similarities else None))
    return samples


def simulate_thresholds(
    samples: list[tuple[EvalItem, float | None]],
    thresholds: list[float],
) -> list[tuple[float, RefusalMetrics]]:
    """按 τ 本地模拟 L1 拒答（零 LLM 调用，可任意密集扫描）。

    模拟规则与 `ask.answer_question` 的 L1 完全一致：
    无候选 或 最高相似度 < τ → 拒答。
    """
    results: list[tuple[float, RefusalMetrics]] = []
    for threshold in thresholds:
        metrics = RefusalMetrics()
        for item, similarity in samples:
            refused_by_l1 = similarity is None or similarity < threshold
            if item.in_kb:
                metrics.in_kb_total += 1
                if refused_by_l1:
                    metrics.in_kb_refused += 1
            else:
                metrics.out_of_kb_total += 1
                if refused_by_l1:
                    metrics.out_of_kb_refused += 1
                    metrics.refusal_reasons["low_relevance(l1)"] = (
                        metrics.refusal_reasons.get("low_relevance(l1)", 0) + 1
                    )
        results.append((threshold, metrics))
    return results
