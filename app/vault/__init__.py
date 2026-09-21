"""可独立于 SQLite 重建的文件系统 Vault 元数据。"""

from .models import SourceRecord, VaultSnapshot
from .store import CATALOG_NAME, VaultError, VaultStore

__all__ = [
    "CATALOG_NAME",
    "VaultError",
    "VaultSnapshot",
    "VaultStore",
    "SourceRecord",
]
