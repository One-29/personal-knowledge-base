"""单进程桌面运行时的滚动文件日志与凭据脱敏。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from threading import RLock
import time
from typing import Iterable

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core.config import Settings

from .context import current_request_id

LOG_DIRECTORY_NAME = "logs"
LOG_FILE_NAME = "knowbase.log"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 4

_state_lock = RLock()
_handler: RotatingFileHandler | None = None
_log_path: Path | None = None
_logger_names = ("app", "uvicorn.error")
_previous_levels: dict[str, int] = {}

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_BASIC_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9._~+/=-]+")
_CREDENTIAL_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|secret|authorization)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\"',\s}\]]+"
)
_URL_USERINFO_RE = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id()
        return True


class _RedactingFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, *, secrets: Iterable[str]) -> None:
        super().__init__(
            fmt=(
                "%(asctime)sZ %(levelname)s %(name)s "
                "request_id=%(request_id)s %(message)s"
            ),
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        self._secrets = tuple(
            sorted(
                {secret for secret in secrets if secret},
                key=len,
                reverse=True,
            )
        )

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), secrets=self._secrets)


def configure_runtime_logging(
    config: Settings,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path | None:
    """只为日常 SQLite 运行时启用日志；迁移/测试 PostgreSQL 不落用户目录。"""
    if not config.database_url:
        return None
    try:
        backend = make_url(config.database_url).get_backend_name()
    except ArgumentError:
        return None
    if backend != "sqlite":
        return None
    return configure_local_logging(
        config.data_dir,
        secrets=(config.embedding_api_key or "", config.llm_api_key or ""),
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def configure_local_logging(
    data_dir: Path,
    *,
    secrets: Iterable[str] = (),
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path | None:
    """幂等安装滚动日志；日志故障不会阻止知识库继续启动。"""
    global _handler, _log_path
    if max_bytes < 1:
        raise ValueError("日志轮转大小必须大于 0")
    if backup_count < 1:
        raise ValueError("日志备份数量必须大于 0")

    try:
        path = data_dir.expanduser().resolve() / LOG_DIRECTORY_NAME / LOG_FILE_NAME
    except (OSError, RuntimeError):
        logging.getLogger(__name__).warning(
            "无法解析本地诊断日志目录；应用将继续运行",
            exc_info=True,
        )
        return None
    with _state_lock:
        if _handler is not None:
            if _log_path == path:
                _attach_handler(_handler)
                return path
            # 一个进程只应服务一份用户数据。测试可显式 shutdown 后再换目录。
            raise RuntimeError(f"本进程已把日志写入另一目录：{_log_path}")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        except OSError:
            logging.getLogger(__name__).warning(
                "无法创建本地诊断日志；应用将继续运行",
                exc_info=True,
            )
            return None

        handler.setLevel(logging.INFO)
        handler.addFilter(_RequestContextFilter())
        handler.setFormatter(_RedactingFormatter(secrets=secrets))
        _handler = handler
        _log_path = path
        _attach_handler(handler)

    logging.getLogger(__name__).info(
        "本地滚动日志已启用 max_bytes=%d backups=%d",
        max_bytes,
        backup_count,
    )
    return path


def get_log_path() -> Path | None:
    with _state_lock:
        return _log_path


def flush_local_logging() -> None:
    with _state_lock:
        if _handler is not None:
            _handler.flush()


def shutdown_local_logging() -> None:
    """测试与可复用宿主显式释放文件句柄；普通进程退出也会自动关闭。"""
    global _handler, _log_path
    with _state_lock:
        handler = _handler
        if handler is None:
            return
        for name in _logger_names:
            target = logging.getLogger(name)
            target.removeHandler(handler)
            if name in _previous_levels:
                target.setLevel(_previous_levels[name])
        try:
            handler.flush()
        except OSError:
            # 日志是辅助能力，关闭失败不能阻止 API 或桌面进程退出。
            pass
        try:
            handler.close()
        except OSError:
            pass
        _handler = None
        _log_path = None
        _previous_levels.clear()


def redact_text(value: str, *, secrets: Iterable[str] = ()) -> str:
    """脱敏已知密钥、Bearer、常见凭据字段与 URL userinfo。"""
    rendered = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        rendered = rendered.replace(secret, "[REDACTED]")
    rendered = _BEARER_RE.sub("Bearer [REDACTED]", rendered)
    rendered = _BASIC_RE.sub("Basic [REDACTED]", rendered)
    rendered = _CREDENTIAL_RE.sub(r"\1[REDACTED]", rendered)
    return _URL_USERINFO_RE.sub(r"\1[REDACTED]@", rendered)


def _attach_handler(handler: RotatingFileHandler) -> None:
    for name in _logger_names:
        target = logging.getLogger(name)
        if name == "app":
            if name not in _previous_levels:
                _previous_levels[name] = target.level
            target.setLevel(logging.INFO)
        if handler not in target.handlers:
            target.addHandler(handler)
