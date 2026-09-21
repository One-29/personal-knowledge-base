"""一次性数据迁移工具。

运行时的双方言访问留在 :mod:`app.database` / :mod:`app.search`；这里仅处理
可审计的数据搬迁，不参与 API 请求路径。
"""

from .errors import MigrationError
from .postgresql_to_sqlite import migrate_postgresql_to_sqlite
from .report import MigrationReport

__all__ = [
    "MigrationError",
    "MigrationReport",
    "migrate_postgresql_to_sqlite",
]
