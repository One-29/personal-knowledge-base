"""问答路由（M3）：POST /ask 与引用溯源 GET /citations/{chunk_id}。

契约（docs/issues/m3-retrieval.md §3）：
- 拒答是 200 + refused=true 的正常业务结果，不是错误响应；
- 引用以 chunk_id 为标识（块是溯源的原子单元）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import ask as ask_service
from .. import crud
from ..db import get_db
from ..models import Chunk
from ..schemas import (
    AnswerOut,
    AskRequest,
    CitationDetailOut,
    CitationOut,
)

router = APIRouter(tags=["Q&A"])


@router.post("/ask", response_model=AnswerOut)
def ask_question(payload: AskRequest, db: Session = Depends(get_db)):
    """基于知识库回答问题；覆盖不足时拒答（refused=true）。"""
    try:
        result = ask_service.answer_question(
            db,
            payload.question,
            payload.kb_id,
            session_id=payload.session_id,
            history=[(t.question, t.answer) for t in payload.history] if payload.history else None,
        )
    except ask_service.KnowledgeBaseNotFound as exc:   # 知识库不存在
        raise HTTPException(status_code=404, detail=str(exc)) from None

    return AnswerOut(
        question=result.question,
        content=result.content,
        session_id=payload.session_id,
        search_query=result.search_query,
        citations=[
            CitationOut(
                index=c.index,
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                doc_title=c.doc_title,
                chunk_text=c.chunk_text,
                char_start=c.char_start,
                char_end=c.char_end,
            )
            for c in result.citations
        ],
        refused=result.refused,
        refusal_reason=result.refusal_reason,
    )


@router.get("/citations/{chunk_id}", response_model=CitationDetailOut)
def get_citation(chunk_id: int, db: Session = Depends(get_db)):
    """引用溯源：按块 id 取回原文片段与字符区间（前端据此高亮）。"""
    chunk = db.get(Chunk, chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="引用不存在（块可能已被重传替换）")
    doc = crud.get_document(db, chunk.doc_id)
    return CitationDetailOut(
        doc_id=chunk.doc_id,
        doc_title=doc.title if doc else "",
        chunk_text=chunk.content,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
    )
