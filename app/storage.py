"""普通原文与版本文件的安全路径、原子写入和删除操作。"""

import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

from app.core.config import settings


def _abs_path(
    kb_id: int,
    doc_id: int,
    *,
    storage_root: Path | None = None,
) -> Path:
    return (storage_root or settings.storage_dir) / f"{kb_id}/{doc_id}.md"


def save(
    kb_id: int,
    doc_id: int,
    content: bytes,
    *,
    storage_root: Path | None = None,
) -> str:
    """写入旧式原文路径；保留此入口供现有维护与测试代码使用。"""
    path = _abs_path(kb_id, doc_id, storage_root=storage_root)
    _write_atomic(path, content)
    return f"{kb_id}/{doc_id}.md"


def save_version(
    kb_id: int,
    doc_id: int,
    version: int,
    content_hash: str,
    content: bytes,
    *,
    storage_root: Path | None = None,
) -> str:
    """把普通文本上传写成不可变候选版本。"""
    rel_path = f"{kb_id}/{doc_id}/v{version}-{content_hash[:16]}.md"
    _write_atomic(resolve_relative(rel_path, storage_root=storage_root), content)
    return rel_path


def _write_atomic(path: Path, content: bytes) -> None:
    """在目标目录写临时文件后原子替换，失败时不截断原文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def resolve_relative(rel_path: str, *, storage_root: Path | None = None) -> Path:
    """解析存储相对路径，并拒绝任何越出 STORAGE_DIR 的值。"""
    root = (storage_root or settings.storage_dir).resolve()
    candidate = (root / rel_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("storage path escapes configured root")
    return candidate


def verify_hash(path: Path, expected_hash: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        raise OSError(f"文件完整性校验失败: {path.name}")


def read(rel_path: str, *, storage_root: Path | None = None) -> str:
    """按原始 UTF-8 字节读取原文，避免 Windows 把 CRLF 隐式转换成 LF。"""
    return resolve_relative(rel_path, storage_root=storage_root).read_bytes().decode("utf-8")


def delete(rel_path: str, *, storage_root: Path | None = None) -> bool:
    """幂等删除普通候选文件或一整个带清单的图片包候选目录。"""
    path = resolve_relative(rel_path, storage_root=storage_root)
    if not path.exists():
        return False
    if path.name == "source.md" and (path.parent / "manifest.json").is_file():
        removed = path.parent
        shutil.rmtree(removed)
    else:
        path.unlink()
        removed = path
    try:
        removed.parent.rmdir()
    except OSError:
        pass
    return True


def delete_document_files(kb_id: int, doc_id: int) -> None:
    """删除文档旧式原文以及所有不可变版本，并清理空知识库目录。"""
    kb_dir = settings.storage_dir / str(kb_id)
    legacy = kb_dir / f"{doc_id}.md"
    try:
        legacy.unlink()
    except FileNotFoundError:
        pass
    shutil.rmtree(kb_dir / str(doc_id), ignore_errors=True)
    try:
        kb_dir.rmdir()
    except OSError:
        pass


def delete_kb_dir(kb_id: int) -> None:
    """幂等删除一个知识库的完整原文目录。"""
    shutil.rmtree(settings.storage_dir / str(kb_id), ignore_errors=True)
