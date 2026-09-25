"""问答路由（M3）：POST /ask 与引用溯源 GET /citations/{chunk_id}。

契约（docs/issues/m3-retrieval.md §3）：
- 拒答是 200 + refused=true 的正常业务结果，不是错误响应；
- 引用以 chunk_id 为标识（块是溯源的原子单元）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import answer_stream
from .. import ask as ask_service
from .. import crud, document_images, package_storage
from ..db import get_db
from ..diagnostics import current_request_id
from ..models import Chunk
from ..schemas import (
    AnswerOut,
    AskRequest,
    CitationDetailOut,
    DocumentImageOut,
    citations_out,
)
from ..sse import encode_event

router = APIRouter(tags=["Q&A"])
logger = logging.getLogger(__name__)


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

    return _answer_out(payload, result)


@router.post(
    "/ask/stream",
    response_class=StreamingResponse,
    response_model=None,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "问答元数据、模型文本增量与最终可信结果",
        }
    },
)
def ask_question_stream(payload: AskRequest, db: Session = Depends(get_db)):
    """检索与 L1 拒答后，以 SSE 返回生成草稿和经过 L2 校验的最终结果。"""
    try:
        prepared = ask_service.prepare_answer(
            db,
            payload.question,
            payload.kb_id,
            session_id=payload.session_id,
            history=[(t.question, t.answer) for t in payload.history] if payload.history else None,
        )
    except ask_service.KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    return StreamingResponse(
        _answer_events(payload, prepared),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _answer_events(
    payload: AskRequest,
    prepared: ask_service.AnswerPreparation,
) -> Iterator[bytes]:
    yield encode_event(
        "metadata",
        {
            "question": prepared.question,
            "search_query": prepared.search_query,
        },
    )
    try:
        for event in answer_stream.stream_prepared_answer(prepared):
            if isinstance(event, answer_stream.AnswerDelta):
                yield encode_event("delta", {"content": event.content})
            else:
                yield encode_event(
                    "result",
                    _answer_out(payload, event.result).model_dump(mode="json"),
                )
    except Exception:
        request_id = current_request_id()
        logger.exception(
            "流式问答发生未处理异常",
            extra={"request_id": request_id},
        )
        yield encode_event(
            "error",
            {
                "detail": "流式回答中断，请复制诊断信息后重试",
                "request_id": request_id,
            },
        )


def _answer_out(payload: AskRequest, result: ask_service.AnswerData) -> AnswerOut:
    return AnswerOut(
        question=result.question,
        content=result.content,
        session_id=payload.session_id,
        search_query=result.search_query,
        citations=citations_out(result.citations),
        refused=result.refused,
        refusal_reason=result.refusal_reason,
    )


@router.get(
    "/citations/{chunk_id}",
    response_model=CitationDetailOut,
    response_model_exclude_defaults=True,
)
def get_citation(chunk_id: int, db: Session = Depends(get_db)):
    """引用溯源：按块 id 取回原文片段与字符区间（前端据此高亮）。"""
    chunk = db.get(Chunk, chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="引用不存在（块可能已被重传替换）")
    doc = crud.get_document(db, chunk.doc_id)
    try:
        images = document_images.images_for_source(
            chunk.doc_id,
            doc.file_path if doc else "",
            char_start=chunk.char_start,
            char_end=chunk.char_end,
        ) if doc else []
    except package_storage.StorageIntegrityError:
        raise HTTPException(status_code=500, detail="引用图片完整性校验失败") from None
    return CitationDetailOut(
        doc_id=chunk.doc_id,
        doc_title=doc.title if doc else "",
        chunk_text=chunk.content,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        images=[DocumentImageOut.model_validate(image) for image in images],
    )
