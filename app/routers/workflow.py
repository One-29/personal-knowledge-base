"""多步工作流路由（M4）：POST /api/v1/workflow。

契约（docs/issues/m4-workflow.md §3）：同步返回全部步骤与最终结果（AW3），
每步状态（answered / insufficient / error）与缺料说明对用户可见（US-M4-02）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import workflow as workflow_service
from ..ask import KnowledgeBaseNotFound
from ..db import get_db
from ..schemas import (
    CitationOut,
    WorkflowRequest,
    WorkflowResultOut,
    WorkflowStepOut,
)

router = APIRouter(tags=["Workflow"])


def _citation_out(citations) -> list[CitationOut]:
    return [
        CitationOut(
            index=c.index,
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            doc_title=c.doc_title,
            chunk_text=c.chunk_text,
            char_start=c.char_start,
            char_end=c.char_end,
        )
        for c in citations
    ]


@router.post("/workflow", response_model=WorkflowResultOut)
def run_workflow(payload: WorkflowRequest, db: Session = Depends(get_db)):
    """执行跨文档综合任务：自动拆步、逐步检索、汇总并标注缺料。"""
    try:
        result = workflow_service.run_workflow(
            db, payload.task, payload.kb_id, max_steps=payload.max_steps
        )
    except KnowledgeBaseNotFound as exc:             # 知识库不存在
        raise HTTPException(status_code=404, detail=str(exc)) from None

    return WorkflowResultOut(
        task=result.task,
        steps=[
            WorkflowStepOut(
                index=step.index,
                goal=step.goal,
                query=step.query,
                status=step.status,
                conclusion=step.conclusion,
                note=step.note,
                citations=_citation_out(step.citations),
            )
            for step in result.steps
        ],
        answer=result.answer,
        citations=_citation_out(result.citations),
    )
