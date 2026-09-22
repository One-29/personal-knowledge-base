"""本地 SQLite 的首次导入、schema 初始化和启动前自检。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.database import (
    UnsupportedSchemaVersion,
    create_database_engine,
    initialize_database,
)
from app.embedding_profile import (
    EmbeddingProfile,
    EmbeddingProfileError,
    ensure_embedding_profile,
)
from app.vault.external_sync import ExternalSourceSync, reconcile_external_sources
from app.vault.rebuild import VaultRebuildError, rebuild_database
from app.vault.store import VaultError, VaultStore
from app.vault.sync import ensure_snapshot

from .configuration import RuntimePaths, resolve_runtime_paths
from .project_import import ProjectDataImport, import_project_data


class RuntimeInitializationError(RuntimeError):
    """本地数据库无法在启动前完成一致初始化。"""


@dataclass(frozen=True)
class RuntimeInitialization:
    paths: RuntimePaths
    project_import: ProjectDataImport
    external_sync: ExternalSourceSync


def prepare_runtime(
    *,
    config: Settings = settings,
    project_root: Path | None = None,
    import_existing_project_data: bool = True,
) -> RuntimeInitialization:
    """准备日常 SQLite；可重复调用，不会覆盖已存在的用户库。"""
    paths = resolve_runtime_paths(config)
    root = (
        project_root.expanduser().resolve()
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    vault = VaultStore(paths.storage)
    try:
        if not paths.database.exists() and vault.exists():
            rebuilt = rebuild_database(
                paths.database,
                store=vault,
                config=config,
            )
            project_import = ProjectDataImport(
                status="vault-rebuilt",
                database=rebuilt.database,
            )
        elif import_existing_project_data:
            project_import = import_project_data(
                source_database=root / "data" / "knowbase.db",
                source_storage=root / "data" / "storage",
                target_database=paths.database,
                target_storage=paths.storage,
                embedding_dimension=config.embedding_dimension,
                timeout_seconds=config.db_pool_timeout,
            )
        else:
            project_import = ProjectDataImport(status="disabled")
    except (VaultError, VaultRebuildError) as exc:
        raise RuntimeInitializationError(f"SQLite 启动前重建失败：{exc}") from exc

    engine = None
    external_sync = ExternalSourceSync()
    try:
        paths.storage.mkdir(parents=True, exist_ok=True)
        assert config.database_url is not None
        engine = create_database_engine(
            config.database_url,
            pool_size=config.db_pool_size,
            max_overflow=config.db_max_overflow,
            pool_recycle=config.db_pool_recycle,
            pool_timeout=config.db_pool_timeout,
        )
        initialize_database(engine)
        with Session(engine, expire_on_commit=False) as db:
            ensure_embedding_profile(db, EmbeddingProfile.configured(config))
            snapshot = ensure_snapshot(db, store=vault)
            _snapshot, external_sync = reconcile_external_sources(
                db,
                snapshot,
                store=vault,
                config=config,
            )
        with engine.connect() as connection:
            quick_check = connection.exec_driver_sql("PRAGMA quick_check").scalar_one()
            foreign_keys = connection.exec_driver_sql(
                "PRAGMA foreign_key_check"
            ).fetchall()
        if quick_check != "ok":
            raise RuntimeInitializationError(f"SQLite quick_check 失败：{quick_check}")
        if foreign_keys:
            raise RuntimeInitializationError(
                f"SQLite 外键检查发现 {len(foreign_keys)} 条错误"
            )
    except RuntimeInitializationError:
        raise
    except (
        EmbeddingProfileError,
        OSError,
        SQLAlchemyError,
        UnsupportedSchemaVersion,
        VaultError,
    ) as exc:
        raise RuntimeInitializationError(f"SQLite 启动前检查失败：{exc}") from exc
    finally:
        if engine is not None:
            engine.dispose()

    return RuntimeInitialization(
        paths=paths,
        project_import=project_import,
        external_sync=external_sync,
    )
