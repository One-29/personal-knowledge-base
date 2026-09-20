"""ZIP 内逻辑路径的跨平台规范化规则。"""

import re
import unicodedata
from pathlib import PurePosixPath

from .models import UploadValidationError

MAX_ARCHIVE_PATH_BYTES = 1024
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def normalize_archive_path(raw_path: str) -> str:
    """返回规范的 POSIX 相对路径；拒绝转义、URI 和平台歧义。"""
    if not isinstance(raw_path, str) or not raw_path:
        raise UploadValidationError("ZIP 包含非法路径")
    if "\x00" in raw_path or "\\" in raw_path:
        raise UploadValidationError("ZIP 包含非法路径")
    path = unicodedata.normalize("NFC", raw_path)
    if path != raw_path:
        raise UploadValidationError("ZIP 路径必须使用统一的 Unicode NFC 编码")
    if len(path.encode("utf-8")) > MAX_ARCHIVE_PATH_BYTES:
        raise UploadValidationError("ZIP 路径过长")
    if _URI_SCHEME_RE.match(path):
        raise UploadValidationError(f"ZIP 包含不安全路径：{path}")
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or pure.as_posix() != path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise UploadValidationError(f"ZIP 包含不安全路径：{path}")
    return pure.as_posix()


def is_canonical_archive_path(value: object) -> bool:
    """供持久化清单校验复用，与上传入口保持完全相同的路径语义。"""
    if not isinstance(value, str):
        return False
    try:
        return normalize_archive_path(value) == value
    except UploadValidationError:
        return False
