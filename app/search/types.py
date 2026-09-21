"""跨数据库检索后端共享的值对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphPair:
    source_doc: int
    target_doc: int
    similarity: float
