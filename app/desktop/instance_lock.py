"""跨平台、崩溃后由操作系统自动释放的桌面单实例锁。"""

from __future__ import annotations

import os
from pathlib import Path
from threading import RLock
from typing import BinaryIO

LOCK_FILE_NAME = ".knowbase-instance.lock"
_registry_lock = RLock()
_held_paths: set[Path] = set()


class SingleInstanceError(RuntimeError):
    """同一用户数据目录已经有一个 KnowBase 桌面实例。"""


class SingleInstanceLock:
    """持有一个字节的系统文件锁；锁文件本身可跨崩溃保留。"""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "SingleInstanceLock":
        with _registry_lock:
            if self.acquired or self.path in _held_paths:
                raise SingleInstanceError(
                    "KnowBase 已经使用这份用户数据运行；请切换到现有窗口"
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                _lock(handle)
                handle.seek(1)
                handle.truncate()
                handle.write(f"{os.getpid()}\n".encode("ascii"))
                handle.flush()
            except OSError as exc:
                handle.close()
                raise SingleInstanceError(
                    "KnowBase 已经使用这份用户数据运行；请切换到现有窗口"
                ) from exc
            self._handle = handle
            _held_paths.add(self.path)
            return self

    def release(self) -> None:
        with _registry_lock:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            try:
                _unlock(handle)
            finally:
                handle.close()
                _held_paths.discard(self.path)

    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def lock_path(database: Path) -> Path:
    return database.expanduser().resolve().parent / LOCK_FILE_NAME


def _lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
