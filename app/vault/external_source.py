"""已登记 Vault 来源的稳定读取与外部变化识别。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app import package_storage, storage
from app.core.config import Settings

from .models import SourceRecord
from .store import VaultError

__all__ = ["ExternalSourceSyncError", "ObservedSource", "observe_source"]


class ExternalSourceSyncError(VaultError):
    """外部原文无法在不猜测用户意图的前提下安全同步。"""


@dataclass(frozen=True)
class ObservedSource:
    record: SourceRecord
    text: str | None
    package_manifest: dict | None
    content_changed: bool


def observe_source(
    expected: SourceRecord,
    *,
    root: Path,
    config: Settings,
    force_text: bool,
) -> ObservedSource:
    """读取稳定来源；普通文本用 mtime/size 筛选后再计算内容摘要。"""
    path = _source_path(root, expected.path)
    if PurePosixPath(expected.path).name == package_storage.SOURCE_NAME:
        return _observe_package(expected, path=path, root=root)
    return _observe_plain_text(
        expected,
        path=path,
        config=config,
        force_text=force_text,
    )


def _observe_package(
    expected: SourceRecord,
    *,
    path: Path,
    root: Path,
) -> ObservedSource:
    try:
        before = path.stat()
        manifest, text = package_storage.verify_stored_package(
            expected.path,
            expected_package_hash=expected.content_hash,
            storage_root=root,
        )
        after = path.stat()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ExternalSourceSyncError(
            f"图片包 {expected.path} 被外部修改或已损坏；"
            "图片包必须通过 ZIP 重传更新"
        ) from exc
    _require_stable_stat(expected.path, before, after)
    if len(text) != expected.char_count:
        raise ExternalSourceSyncError(
            f"图片包 {expected.path} 的字符数与 Vault 不一致"
        )
    return ObservedSource(
        record=expected.model_copy(
            update={"size": after.st_size, "mtime_ns": after.st_mtime_ns}
        ),
        text=text,
        package_manifest=manifest,
        content_changed=False,
    )


def _observe_plain_text(
    expected: SourceRecord,
    *,
    path: Path,
    config: Settings,
    force_text: bool,
) -> ObservedSource:
    try:
        before = path.stat()
    except OSError as exc:
        raise ExternalSourceSyncError(
            f"无法读取外部原文 {expected.path}：{exc}"
        ) from exc
    if (
        not force_text
        and before.st_size == expected.size
        and before.st_mtime_ns == expected.mtime_ns
    ):
        return ObservedSource(
            record=expected,
            text=None,
            package_manifest=None,
            content_changed=False,
        )

    limit = max(config.max_upload_bytes, expected.size)
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        after = path.stat()
    except OSError as exc:
        raise ExternalSourceSyncError(
            f"无法读取外部原文 {expected.path}：{exc}"
        ) from exc
    _require_stable_stat(expected.path, before, after)
    if len(data) > limit:
        raise ExternalSourceSyncError(
            f"外部原文 {expected.path} 超过允许大小 {limit} 字节"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExternalSourceSyncError(
            f"外部原文 {expected.path} 必须保持 UTF-8 编码"
        ) from exc
    digest = hashlib.sha256(data).hexdigest()
    changed = digest != expected.content_hash
    if changed and not data.strip():
        raise ExternalSourceSyncError(
            f"外部原文 {expected.path} 为空；为防误删，未更新索引"
        )
    if not changed and len(text) != expected.char_count:
        raise ExternalSourceSyncError(
            f"外部原文 {expected.path} 的字符数与 Vault 不一致"
        )
    return ObservedSource(
        record=SourceRecord(
            path=expected.path,
            content_hash=digest,
            char_count=len(text),
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
        ),
        text=text,
        package_manifest=None,
        content_changed=changed,
    )


def _source_path(root: Path, rel_path: str) -> Path:
    try:
        path = storage.resolve_relative(rel_path, storage_root=root)
    except ValueError as exc:
        raise ExternalSourceSyncError(f"原文路径越出 Vault：{rel_path}") from exc
    if not path.is_file():
        raise ExternalSourceSyncError(
            f"已登记原文不存在：{rel_path}。当前不会猜测外部删除或重命名；"
            "请恢复原路径，再通过 KnowBase 删除或重传文档"
        )
    return path


def _require_stable_stat(
    rel_path: str,
    before: os.stat_result,
    after: os.stat_result,
) -> None:
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ExternalSourceSyncError(
            f"读取期间原文仍在变化：{rel_path}；请保存完成后重试"
        )
