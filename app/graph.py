"""关联图（M5+）：把跨文档的语义相似度聚合成文档级图。

对应 Obsidian 的 graph view：**节点 = 文档，边 = 文档间的语义关联强度**。

数据来源：`chunks` 表的向量（pgvector）。一次 LATERAL 近邻查询即可算出
"每个块在其它文档中的最近邻"，再按文档对聚合——不引入图数据库，
复用已有 HNSW 索引（04 DM2）。

设计取舍：
- 节点用**文档**而非块：块级图在大库上会变成毛线团，文档级更可读；
- 边权重要同时反映"关联条数"（跨文档近邻对数）与"关联强度"（平均相似度）；
- 计算量有上限（`max_chunks`），超出时截断并在响应里标注（诚实告知不全）。
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_CHUNKS = 400          # 参与近邻计算的块上限（防大库上算太久）
DEFAULT_TOP_K = 3         # 每个块取几个跨文档近邻
DEFAULT_MIN_SIMILARITY = 0.30   # 低于此相似度不连边（噪声）


@dataclass
class GraphNode:
    """文档节点。"""

    doc_id: int
    title: str
    chunks: int
    chars: int


@dataclass
class GraphEdge:
    """文档间的关联边。"""

    source: int          # doc_id
    target: int          # doc_id
    weight: float        # 归一化强度（0–1，用于线宽/透明度）
    links: int           # 跨文档近邻对数
    similarity: float    # 平均相似度


@dataclass
class GraphData:
    """一次关联图的完整数据。"""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    truncated: bool = False


_NODES_SQL = text("""
    SELECT d.id AS doc_id,
           d.title,
           d.char_count,
           COUNT(c.id) AS chunks
    FROM documents d
    LEFT JOIN chunks c ON c.doc_id = d.id
    WHERE d.kb_id = :kb_id
    GROUP BY d.id, d.title, d.char_count
    ORDER BY d.id
""")

_PAIRS_SQL = text("""
    WITH pool AS (
        SELECT id, doc_id, embedding
        FROM chunks
        WHERE kb_id = :kb_id
        ORDER BY id
        LIMIT :max_chunks
    ),
    total AS (
        SELECT COUNT(*) AS n FROM chunks WHERE kb_id = :kb_id
    )
    SELECT (SELECT n FROM total) AS total_chunks,
           p.doc_id AS source_doc,
           n.doc_id AS target_doc,
           1 - (p.embedding <=> n.embedding) AS similarity
    FROM pool p
    JOIN LATERAL (
        SELECT c2.doc_id, c2.embedding
        FROM chunks c2
        WHERE c2.kb_id = :kb_id AND c2.doc_id <> p.doc_id
        ORDER BY c2.embedding <=> p.embedding
        LIMIT :top_k
    ) n ON true
    WHERE 1 - (p.embedding <=> n.embedding) >= :min_similarity
""")


def build_graph(
    db: Session,
    kb_id: int,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    max_chunks: int = MAX_CHUNKS,
) -> GraphData:
    """构建某个知识库的文档关联图。"""
    nodes = [
        GraphNode(
            doc_id=row.doc_id,
            title=row.title,
            chunks=row.chunks,
            chars=row.char_count,
        )
        for row in db.execute(_NODES_SQL, {"kb_id": kb_id}).all()
    ]
    if len(nodes) < 2:
        return GraphData(nodes=nodes)          # 单文档没有"关联"可言

    rows = db.execute(
        _PAIRS_SQL,
        {
            "kb_id": kb_id,
            "top_k": top_k,
            "min_similarity": min_similarity,
            "max_chunks": max_chunks,
        },
    ).all()

    truncated = bool(rows) and rows[0].total_chunks > max_chunks

    # 按文档对聚合：跨文档近邻对数 + 平均相似度
    buckets: dict[tuple[int, int], list[float]] = {}
    for row in rows:
        key = (min(row.source_doc, row.target_doc), max(row.source_doc, row.target_doc))
        buckets.setdefault(key, []).append(float(row.similarity))

    raw_edges: list[GraphEdge] = []
    for (source, target), similarities in buckets.items():
        links = len(similarities)
        avg = sum(similarities) / links
        # 条数与强度都参与：单条关联打折，多条关联逐渐饱和
        raw_edges.append(
            GraphEdge(
                source=source,
                target=target,
                weight=avg * min(1.0, links / 2),
                links=links,
                similarity=avg,
            )
        )

    top_weight = max((edge.weight for edge in raw_edges), default=0.0) or 1.0
    edges = [
        GraphEdge(
            source=edge.source,
            target=edge.target,
            weight=round(edge.weight / top_weight, 4),
            links=edge.links,
            similarity=round(edge.similarity, 4),
        )
        for edge in raw_edges
    ]
    edges.sort(key=lambda e: e.weight, reverse=True)

    logger.info("关联图: kb_id=%s 节点=%d 边=%d 截断=%s", kb_id, len(nodes), len(edges), truncated)
    return GraphData(nodes=nodes, edges=edges, truncated=truncated)
