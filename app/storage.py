"""原文文件存储（决策 D6）：正文存文件系统，库内只存相对路径。

路径约定（03 §3）：旧文件为 storage/{kb_id}/{doc_id}.md，新上传使用
storage/{kb_id}/{doc_id}/v{version}-{hash}.md。路径只使用数据库 id、版本与摘要，
不使用用户文件名。
"""

import os
import shutil
from pathlib import Path
from uuid import uuid4

from app.core.config import settings


def _abs_path(kb_id: int, doc_id: int) -> Path:
    """相对路径 → 绝对路径：根目录由 settings.storage_dir 配置。"""
    return settings.storage_dir / f"{kb_id}/{doc_id}.md"


def save(kb_id: int, doc_id: int, content: bytes) -> str:
    """写文件；目录不存在自动创建；返回相对路径供 documents.file_path 存储。"""
    p = _abs_path(kb_id, doc_id)
    _write_atomic(p, content)
    return f"{kb_id}/{doc_id}.md"       # 返回相对路径


def save_version(
    kb_id: int,
    doc_id: int,
    version: int,
    content_hash: str,
    content: bytes,
) -> str:
    """把一次上传写成不可变候选文件，供成功后的数据库事务切换引用。"""
    rel_path = f"{kb_id}/{doc_id}/v{version}-{content_hash[:16]}.md"
    _write_atomic(settings.storage_dir / rel_path, content)
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


def read(rel_path: str) -> str:
    """读原文。文件丢失抛 FileNotFoundError（调用方转 404）。"""
    return (settings.storage_dir / rel_path).read_text(encoding="utf-8")   # 读文本


def delete(rel_path: str) -> bool:
    """删文件。不存在返回 False（幂等，调用方决定 204/404）。

    顺带清理变空的父目录（如删掉某库最后一篇文档后，{kb_id}/ 不留空壳）。
    """
    p = settings.storage_dir / rel_path
    if not p.exists():
        return False
    p.unlink()
    try:
        p.parent.rmdir()                # 目录已空才成功；非空抛 OSError，忽略
    except OSError:
        pass
    return True


def delete_document_files(kb_id: int, doc_id: int) -> None:
    """删除文档的旧式原文以及所有版本文件，并清理空知识库目录。"""
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
    """删除整个知识库的原文目录（02 §3 删库编排：元数据 → 原文 → 块）。

    幂等：目录不存在时无操作。
    """
    shutil.rmtree(settings.storage_dir / str(kb_id), ignore_errors=True)
