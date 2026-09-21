"""不跟随链接的原文目录复制与确定性摘要。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


class StorageTreeError(RuntimeError):
    """原文目录含不安全条目，或复制前后内容不一致。"""


@dataclass(frozen=True)
class TreeDigest:
    directories: int
    files: int
    bytes: int
    sha256: str


@dataclass(frozen=True)
class _Entry:
    relative: str
    path: Path
    is_directory: bool


def digest_tree(root: Path) -> TreeDigest:
    """摘要包含相对路径、空目录、文件大小与文件内容。"""
    root = _absolute_path(root)
    if root.exists() and _is_path_link(root):
        raise StorageTreeError(f"原文存储根不允许是符号链接或目录联接：{root}")
    resolved = root.resolve()
    if not resolved.exists():
        return TreeDigest(0, 0, 0, hashlib.sha256().hexdigest())
    if not resolved.is_dir():
        raise StorageTreeError(f"原文存储根不是目录：{resolved}")

    entries = _safe_entries(resolved)
    digest = hashlib.sha256()
    directories = 0
    files = 0
    total_bytes = 0
    for entry in entries:
        if entry.is_directory:
            directories += 1
            digest.update(f"D\0{entry.relative}\n".encode("utf-8"))
            continue
        size, file_hash = _digest_file(entry.path)
        files += 1
        total_bytes += size
        digest.update(
            f"F\0{entry.relative}\0{size}\0{file_hash}\n".encode("utf-8")
        )
    return TreeDigest(directories, files, total_bytes, digest.hexdigest())


def copy_tree_verified(source: Path, destination: Path) -> TreeDigest:
    """复制完整目录；源在复制期间变化时拒绝使用候选目录。"""
    source = _absolute_path(source)
    destination = _absolute_path(destination)
    if source.exists() and _is_path_link(source):
        raise StorageTreeError(f"原文存储根不允许是符号链接或目录联接：{source}")
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise StorageTreeError(f"原文候选目录已存在：{destination}")

    before = digest_tree(source)
    destination.mkdir(parents=True)
    if source.exists():
        for entry in _safe_entries(source):
            target = destination / Path(entry.relative)
            if entry.is_directory:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry.path, target)

    after = digest_tree(source)
    copied = digest_tree(destination)
    if before != after:
        raise StorageTreeError("复制期间原文目录发生变化")
    if copied != before:
        raise StorageTreeError("原文候选目录摘要与源目录不一致")
    return copied


def _safe_entries(root: Path) -> list[_Entry]:
    entries: list[_Entry] = []

    def visit(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise StorageTreeError(f"无法读取原文目录：{directory}") from exc
        for child in children:
            path = Path(child.path)
            if _is_link_or_reparse_point(child):
                raise StorageTreeError(f"原文目录不允许符号链接或目录联接：{path}")
            relative = path.relative_to(root).as_posix()
            if child.is_dir(follow_symlinks=False):
                entries.append(_Entry(relative, path, True))
                visit(path)
            elif child.is_file(follow_symlinks=False):
                entries.append(_Entry(relative, path, False))
            else:
                raise StorageTreeError(f"原文目录包含不支持的文件类型：{path}")

    visit(root)
    return sorted(entries, key=lambda item: (item.relative, not item.is_directory))


def _is_link_or_reparse_point(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _is_path_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(expanded))


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise StorageTreeError(f"无法读取原文文件：{path}") from exc
    return size, digest.hexdigest()
