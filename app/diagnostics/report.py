"""生成可直接复制、默认不含个人知识内容的诊断摘要。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
import platform

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.core.paths import default_user_data_dir
from app.database import SQLITE_SCHEMA_VERSION, dialect_name
from app.embedding_profile import (
    PROFILE_KEY,
    EmbeddingProfile,
    EmbeddingProfileError,
)
from app.models import AppMetadata, Chunk, Document, IngestTask, KnowledgeBase

from .context import current_request_id
from .local_logging import (
    LOG_DIRECTORY_NAME,
    LOG_FILE_NAME,
    get_log_path,
    redact_text,
)

PRIVACY_NOTICE = (
    "复制内容不含 API Key、服务地址、问题、回答、文档标题或原文；"
    "日志文件可能包含本地路径和错误摘要，分享日志前请自行检查。"
)


@dataclass(frozen=True)
class DiagnosticReport:
    request_id: str
    report: str
    log_path: str | None
    privacy_notice: str = PRIVACY_NOTICE


def build_diagnostic_report(
    db: Session,
    *,
    config: Settings = settings,
    frontend_root: Path | None = None,
) -> DiagnosticReport:
    """读取计数与版本；数据库异常时仍返回不含异常正文的基础报告。"""
    request_id = current_request_id()
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    backend = dialect_name(db)
    database_status = "ok"
    kb_count: int | None = None
    chunk_count: int | None = None
    document_counts: dict[str, int] = {}
    task_counts: dict[str, int] = {}
    profile_label = "not-recorded"

    try:
        kb_count = int(
            db.scalar(select(func.count()).select_from(KnowledgeBase)) or 0
        )
        chunk_count = int(db.scalar(select(func.count()).select_from(Chunk)) or 0)
        document_counts = {
            str(status): int(count)
            for status, count in db.execute(
                select(Document.status, func.count())
                .group_by(Document.status)
                .order_by(Document.status)
            ).all()
        }
        task_counts = {
            str(status): int(count)
            for status, count in db.execute(
                select(IngestTask.status, func.count())
                .group_by(IngestTask.status)
                .order_by(IngestTask.status)
            ).all()
        }
        profile_row = db.get(AppMetadata, PROFILE_KEY)
        if profile_row is not None:
            try:
                profile = EmbeddingProfile.from_json(profile_row.value)
                profile_label = (
                    f"{profile.model} / {profile.dimension} / "
                    f"{profile.fingerprint[:12]}"
                )
            except EmbeddingProfileError:
                profile_label = "invalid"
    except SQLAlchemyError:
        db.rollback()
        database_status = "unavailable"

    root = frontend_root or Path(__file__).resolve().parents[2] / "frontend" / "dist"
    active_log = get_log_path()
    secrets = (config.embedding_api_key or "", config.llm_api_key or "")
    profile_label = redact_text(profile_label, secrets=secrets)
    llm_model = redact_text(config.llm_model, secrets=secrets)
    status_text = ",".join(
        f"{status}:{document_counts.get(status, 0)}"
        for status in ("pending", "processing", "ready", "failed")
    )
    task_status_text = ",".join(
        f"{status}:{task_counts.get(status, 0)}"
        for status in ("queued", "running", "succeeded", "failed", "superseded")
    )
    lines = [
        "KnowBase diagnostics",
        f"generated_utc: {generated}",
        f"request_id: {request_id}",
        f"app_version: {_app_version()}",
        f"python: {platform.python_version()}",
        f"platform: {platform.system()} {platform.release()} {platform.machine()}",
        f"database_backend: {backend}",
        f"database_status: {database_status}",
        f"schema_version: {SQLITE_SCHEMA_VERSION if backend == 'sqlite' else 'alembic'}",
        f"data_location: {_data_location(config)}",
        f"knowledge_bases: {_optional_count(kb_count)}",
        f"documents: {status_text if database_status == 'ok' else 'unavailable'}",
        f"ingest_tasks: {task_status_text if database_status == 'ok' else 'unavailable'}",
        f"chunks: {_optional_count(chunk_count)}",
        f"embedding_profile: {profile_label}",
        f"llm_model: {llm_model}",
        f"frontend_build: {'present' if (root / 'index.html').is_file() else 'missing'}",
        (
            f"local_log: {LOG_DIRECTORY_NAME}/{LOG_FILE_NAME}"
            if active_log is not None
            else "local_log: inactive"
        ),
        f"privacy: {PRIVACY_NOTICE}",
    ]
    return DiagnosticReport(
        request_id=request_id,
        report="\n".join(lines),
        log_path=str(active_log) if active_log is not None else None,
    )


def _app_version() -> str:
    try:
        return metadata.version("knowbase")
    except metadata.PackageNotFoundError:
        return "source"


def _data_location(config: Settings) -> str:
    try:
        expected = default_user_data_dir().expanduser().resolve()
    except (OSError, RuntimeError):
        return "custom"
    return "default" if config.data_dir == expected else "custom"


def _optional_count(value: int | None) -> str:
    return str(value) if value is not None else "unavailable"
