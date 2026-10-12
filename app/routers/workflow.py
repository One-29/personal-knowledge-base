"""多步工作流路由（M4）：POST /api/v1/workflow。

契约（docs/issues/m4-workflow.md §3）：同步返回全部步骤与最终结果（AW3），
每步状态（answered / needs_review / insufficient / error）与候选/缺料说明对用户可见（US-M4-02）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import workflow as workflow_service
from ..ask import KnowledgeBaseNotFound
from ..db import get_db
from ..schemas import (
    WorkflowRequest,
    WorkflowResultOut,
    WorkflowStepOut,
    RetrievalEvidenceOut,
    citations_out,
    evidence_candidates_out,
)

router = APIRouter(tags=["Workflow"])


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
                citations=citations_out(step.citations),
                possible_sources=evidence_candidates_out(step.possible_sources),
                kind=step.kind,
                depends_on=step.depends_on,
                source_scope=step.source_scope,
                search_query=step.search_query,
                evidence=(RetrievalEvidenceOut(
                    band=step.evidence.band,
                    candidate_count=step.evidence.candidate_count,
                    max_vector_similarity=step.evidence.max_vector_similarity,
                    refusal_threshold=step.evidence.refusal_threshold,
                    answer_threshold=step.evidence.answer_threshold,
                ) if step.evidence is not None else None),
                refusal_reason=step.refusal_reason,
                evidence_origin=step.evidence_origin,
            )
            for step in result.steps
        ],
        answer=result.answer,
        citations=citations_out(result.citations),
    )
