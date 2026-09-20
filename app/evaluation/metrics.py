"""可聚合到整体、知识库与难度层级的评估指标。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dataset import EvalDifficulty


def is_hit(
    expected_doc: str,
    actual_doc: str,
    keywords: list[str],
    chunk_text: str,
) -> bool:
    """命中判定：来源文档一致，且块含至少一个期望关键词。"""
    if expected_doc.casefold() != actual_doc.casefold():
        return False
    lowered = chunk_text.casefold()
    return any(keyword.casefold() in lowered for keyword in keywords)


@dataclass
class RetrievalMetrics:
    """整体及逐库 recall@k / MRR。"""

    total: int = 0
    hits: int = 0
    reciprocal_ranks: list[float] = field(default_factory=list)
    by_library: dict[str, RetrievalMetrics] = field(default_factory=dict)
    by_difficulty: dict[str, RetrievalMetrics] = field(default_factory=dict)

    @property
    def recall_at_k(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        return sum(self.reciprocal_ranks) / self.total if self.total else 0.0

    def record(
        self,
        library: str,
        difficulty: EvalDifficulty,
        rank: int | None,
    ) -> None:
        self.total += 1
        buckets = (
            self.by_library.setdefault(library, RetrievalMetrics()),
            self.by_difficulty.setdefault(difficulty, RetrievalMetrics()),
        )
        for bucket in buckets:
            bucket.total += 1
        if rank is None:
            return
        reciprocal = 1.0 / rank
        self.hits += 1
        self.reciprocal_ranks.append(reciprocal)
        for bucket in buckets:
            bucket.hits += 1
            bucket.reciprocal_ranks.append(reciprocal)

    def summary(self) -> str:
        lines = [
            f"overall recall@k={self.recall_at_k:.3f}  MRR={self.mrr:.3f}  "
            f"({self.hits}/{self.total})"
        ]
        lines.extend(
            f"  {key}: recall@k={metrics.recall_at_k:.3f}  MRR={metrics.mrr:.3f}  "
            f"({metrics.hits}/{metrics.total})"
            for key, metrics in sorted(self.by_library.items())
        )
        lines.extend(
            f"  difficulty/{key}: recall@k={metrics.recall_at_k:.3f}  "
            f"MRR={metrics.mrr:.3f}  ({metrics.hits}/{metrics.total})"
            for key, metrics in sorted(self.by_difficulty.items())
        )
        return "\n".join(lines)


@dataclass
class RefusalMetrics:
    """整体及逐库拒答统计。"""

    out_of_kb_total: int = 0
    out_of_kb_refused: int = 0
    in_kb_total: int = 0
    in_kb_refused: int = 0
    refusal_reasons: dict[str, int] = field(default_factory=dict)
    by_library: dict[str, RefusalMetrics] = field(default_factory=dict)
    by_difficulty: dict[str, RefusalMetrics] = field(default_factory=dict)

    @property
    def out_of_kb_refusal_rate(self) -> float:
        if not self.out_of_kb_total:
            return 0.0
        return self.out_of_kb_refused / self.out_of_kb_total

    @property
    def in_kb_false_refusal_rate(self) -> float:
        if not self.in_kb_total:
            return 0.0
        return self.in_kb_refused / self.in_kb_total

    def record(
        self,
        library: str,
        difficulty: EvalDifficulty,
        *,
        in_kb: bool,
        refused: bool,
        reason: str | None = None,
    ) -> None:
        self._record_one(in_kb=in_kb, refused=refused, reason=reason)
        self.by_library.setdefault(library, RefusalMetrics())._record_one(
            in_kb=in_kb,
            refused=refused,
            reason=reason,
        )
        self.by_difficulty.setdefault(difficulty, RefusalMetrics())._record_one(
            in_kb=in_kb,
            refused=refused,
            reason=reason,
        )

    def _record_one(self, *, in_kb: bool, refused: bool, reason: str | None) -> None:
        if in_kb:
            self.in_kb_total += 1
            if refused:
                self.in_kb_refused += 1
        else:
            self.out_of_kb_total += 1
            if refused:
                self.out_of_kb_refused += 1
        if refused and reason:
            self.refusal_reasons[reason] = self.refusal_reasons.get(reason, 0) + 1

    def summary(self) -> str:
        def line(label: str, metrics: RefusalMetrics) -> str:
            return (
                f"{label} 库外拒答率={metrics.out_of_kb_refusal_rate:.3f} "
                f"库内误拒率={metrics.in_kb_false_refusal_rate:.3f} "
                f"拒答原因={metrics.refusal_reasons}"
            )

        return "\n".join(
            [line("overall", self)]
            + [
                line(f"  {key}:", metrics)
                for key, metrics in sorted(self.by_library.items())
            ]
            + [
                line(f"  difficulty/{key}:", metrics)
                for key, metrics in sorted(self.by_difficulty.items())
            ]
        )
