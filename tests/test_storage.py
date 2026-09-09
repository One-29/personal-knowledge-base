"""storage 层测试（D6 原文文件系统：路径约定与清理语义）。

原文目录由 conftest 的 _isolated_storage 隔离到临时目录，测试直接用
settings.storage_dir 读写，互不干扰。
"""

from app import storage
from app.core.config import settings


def test_save_creates_dir_and_returns_relpath():
    """写文件自动建目录，返回相对路径（供 documents.file_path 存储）。"""
    rel = storage.save(1, 2, "内容".encode("utf-8"))
    assert rel == "1/2.md"
    assert (settings.storage_dir / rel).read_text(encoding="utf-8") == "内容"


def test_delete_removes_file_and_empty_dir():
    """删文件后，空的 {kb_id}/ 目录一并清理（不留空壳）。"""
    rel = storage.save(3, 4, b"x")
    assert storage.delete(rel) is True
    assert not (settings.storage_dir / "3").exists()


def test_delete_keeps_dir_when_other_docs_remain():
    """同库还有别的文档时，目录必须保留。"""
    storage.save(5, 1, b"a")
    rel2 = storage.save(5, 2, b"b")
    assert storage.delete(rel2) is True
    assert (settings.storage_dir / "5" / "1.md").exists()
    assert (settings.storage_dir / "5").exists()


def test_delete_missing_returns_false():
    """删不存在的文件 → False（幂等，调用方决定 204/404）。"""
    assert storage.delete("9/9.md") is False


def test_delete_kb_dir_removes_whole_tree():
    """删库：整个 {kb_id}/ 目录消失（02 §3 删库编排第三步）。"""
    storage.save(7, 1, b"a")
    storage.save(7, 2, b"b")
    storage.delete_kb_dir(7)
    assert not (settings.storage_dir / "7").exists()
    storage.delete_kb_dir(7)          # 幂等：目录已不存在也不报错
