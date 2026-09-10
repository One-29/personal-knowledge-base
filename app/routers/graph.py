"""关联图路由：GET /api/v1/graph?kb_id=N。

返回文档级关联图（节点 = 文档，边 = 语义关联强度），供前端画力导向图。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import crud, graph as graph_service
from ..db import get_db
from ..schemas import GraphEdgeOut, GraphNodeOut, GraphOut

router = APIRouter(tags=["Graph"])


@router.get("/graph", response_model=GraphOut)
def get_graph(
    kb_id: int = Query(..., description="知识库 id（关联图按库构建）"),
    top_k: int = Query(3, ge=1, le=10, description="每个块的跨文档近邻数"),
    min_similarity: float = Query(0.30, ge=0.0, le=1.0, description="连边的最低相似度"),
    db: Session = Depends(get_db),
):
    """构建知识库的文档关联图。"""
    if crud.get_kb(db, kb_id) is None:
        raise HTTPException(status_code=404, detail="知识库不存在")

    data = graph_service.build_graph(db, kb_id, top_k=top_k, min_similarity=min_similarity)
    return GraphOut(
        nodes=[
            GraphNodeOut(doc_id=n.doc_id, title=n.title, chunks=n.chunks, chars=n.chars)
            for n in data.nodes
        ],
        edges=[
            GraphEdgeOut(
                source=e.source, target=e.target, weight=e.weight,
                links=e.links, similarity=e.similarity,
            )
            for e in data.edges
        ],
        truncated=data.truncated,
    )
