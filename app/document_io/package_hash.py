"""Markdown 图片包的稳定逻辑摘要。

摘要只覆盖归档内的规范化路径与原始字节，不受 ZIP 时间戳、压缩级别或
条目顺序影响。上传解析与迁移完整性检查共用这里，避免两套算法漂移。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable


def calculate_package_hash(
    source_path: str,
    source: bytes,
    assets: Iterable[tuple[str, bytes]],
) -> str:
    """返回 Markdown 原文及图片资源的格式 v1 稳定摘要。"""
    digest = hashlib.sha256(b"knowbase-image-package-v1\0")
    entries = [(source_path, source), *assets]
    for path, data in sorted(entries, key=lambda item: item[0]):
        path_bytes = path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()
