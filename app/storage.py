"""原文文件存储（决策 D6）：正文存文件系统，库内只存相对路径。

路径约定（03 §3）：storage/{kb_id}/{doc_id}.md
—— 文件名用 doc_id 而非用户文件名：防路径注入与重名（01 §1.2 原则 2）
"""

from pathlib import Path

from app.core.config import settings


def _abs_path(kb_id: int, doc_id: int) -> Path:
    """相对路径 → 绝对路径：根目录由 settings.storage_dir 配置。"""
    return settings.storage_dir / f"{kb_id}/{doc_id}.md"


def save(kb_id: int, doc_id: int, content: bytes) -> str:
    """写文件；目录不存在自动创建；返回相对路径供 documents.file_path 存储。"""
    p = _abs_path(kb_id, doc_id)
    p.parent.mkdir(parents=True, exist_ok=True)   # 建 kb_id 目录，已存在不报错
    p.write_bytes(content)
    return f"{kb_id}/{doc_id}.md"       # 返回相对路径


def read(rel_path: str) -> str:
    """读原文。文件丢失抛 FileNotFoundError（调用方转 404）。"""
    return (settings.storage_dir / rel_path).read_text(encoding="utf-8")   # 读文本


def delete(rel_path: str) -> bool:
    """删文件。不存在返回 False（幂等，调用方决定 204/404）。"""
    p = settings.storage_dir / rel_path
    if not p.exists():
        return False
    p.unlink()
    return True
