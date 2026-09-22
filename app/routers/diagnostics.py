"""只读诊断端点：为桌面界面生成隐私安全的可复制摘要。"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.diagnostics.report import build_diagnostic_report
from app.schemas import DiagnosticsOut

router = APIRouter(tags=["Diagnostics"])


@router.get("/diagnostics", response_model=DiagnosticsOut)
def get_diagnostics(
    response: Response,
    db: Session = Depends(get_db),
) -> DiagnosticsOut:
    response.headers["Cache-Control"] = "no-store"
    report = build_diagnostic_report(db)
    return DiagnosticsOut(
        request_id=report.request_id,
        report=report.report,
        log_path=report.log_path,
        privacy_notice=report.privacy_notice,
    )
