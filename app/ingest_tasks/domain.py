"""文档入库任务的稳定值对象与状态常量。"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_SUPERSEDED)

STAGE_QUEUED = "queued"
STAGE_VALIDATING = "validating"
STAGE_READING = "reading"
STAGE_CHUNKING = "chunking"
STAGE_EMBEDDING = "embedding"
STAGE_PUBLISHING = "publishing"
STAGE_COMPLETE = "complete"

RUNNING_STAGES = (
    STAGE_VALIDATING,
    STAGE_READING,
    STAGE_CHUNKING,
    STAGE_EMBEDDING,
    STAGE_PUBLISHING,
)


@dataclass(frozen=True, slots=True)
class IngestTaskSpec:
    """后台执行所需的不可变任务身份。"""

    doc_id: int
    ingest_version: int
    candidate_path: str | None


@dataclass(frozen=True, slots=True)
class IngestRecovery:
    """一次启动恢复的可观测结果。"""

    inspected: int = 0
    recovered: int = 0
    succeeded: int = 0
    failed: int = 0
    reconciled: int = 0
