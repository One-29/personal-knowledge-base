"""Vault 清单的限量读取、校验和与原子发布。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4

from pydantic import ValidationError

from app.core.config import settings

from .models import VAULT_FORMAT_VERSION, VaultSnapshot

CATALOG_NAME = ".knowbase-vault.json"
MAX_CATALOG_BYTES = 16 * 1024 * 1024
_catalog_lock = RLock()


class VaultError(RuntimeError):
    """文件系统真相清单损坏、越界或无法持久化。"""


class VaultStore:
    def __init__(self, root: Path | None = None) -> None:
        configured = root or settings.storage_dir
        if configured is None:
            raise VaultError("原文存储目录尚未配置")
        self.root = configured.expanduser().resolve()
        self.path = self.root / CATALOG_NAME

    def exists(self) -> bool:
        # “目录项存在”与“清单有效”必须分开：目录、悬空链接等损坏形态
        # 也要进入严格 load() 并失败，不能被当作首次启动而创建空数据库。
        return self.path.exists() or self.path.is_symlink()

    def load(self, *, required: bool = False) -> VaultSnapshot | None:
        with _catalog_lock:
            if not self.path.exists():
                if required:
                    raise VaultError(f"Vault 清单不存在：{self.path}")
                return None
            if not self.path.is_file() or self.path.is_symlink():
                raise VaultError("Vault 清单必须是普通文件")
            try:
                with self.path.open("rb") as handle:
                    raw = handle.read(MAX_CATALOG_BYTES + 1)
            except OSError as exc:
                raise VaultError(f"无法读取 Vault 清单：{exc}") from exc
            if len(raw) > MAX_CATALOG_BYTES:
                raise VaultError("Vault 清单超过大小上限")
            try:
                envelope = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_json_constant,
                )
                if not isinstance(envelope, dict) or set(envelope) != {
                    "format",
                    "payload",
                    "sha256",
                }:
                    raise ValueError("invalid envelope fields")
                if (
                    type(envelope["format"]) is not int
                    or envelope["format"] != VAULT_FORMAT_VERSION
                ):
                    raise ValueError(
                        f"unsupported vault format: {envelope['format']!r}"
                    )
                payload_bytes = _canonical_json(envelope["payload"])
                if not isinstance(envelope["sha256"], str) or not _matches_hash(
                    payload_bytes,
                    envelope["sha256"],
                ):
                    raise ValueError("vault checksum mismatch")
                return VaultSnapshot.model_validate(envelope["payload"])
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise VaultError(f"Vault 清单损坏：{exc}") from exc

    def write(self, snapshot: VaultSnapshot) -> None:
        with _catalog_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = snapshot.model_dump(mode="json")
            payload_bytes = _canonical_json(payload)
            envelope = {
                "format": VAULT_FORMAT_VERSION,
                "payload": payload,
                "sha256": hashlib.sha256(payload_bytes).hexdigest(),
            }
            encoded = json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).encode("utf-8") + b"\n"
            if len(encoded) > MAX_CATALOG_BYTES:
                raise VaultError("Vault 清单超过大小上限")
            temporary = self.root / f".{CATALOG_NAME}.{uuid4().hex}.tmp"
            try:
                with temporary.open("xb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                _fsync_directory(self.root)
            except OSError as exc:
                raise VaultError(f"无法原子写入 Vault 清单：{exc}") from exc
            finally:
                temporary.unlink(missing_ok=True)

    def update(self, transform) -> VaultSnapshot:
        with _catalog_lock:
            current = self.load() or VaultSnapshot()
            updated = transform(current)
            if not isinstance(updated, VaultSnapshot):
                raise TypeError("vault transform must return VaultSnapshot")
            self.write(updated)
            return updated


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _matches_hash(payload: bytes, expected: str) -> bool:
    return len(expected) == 64 and hashlib.sha256(payload).hexdigest() == expected


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _fsync_directory(directory: Path) -> None:
    """POSIX 上同步目录项；Windows 的 replace 已提供本阶段所需原子语义。"""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
