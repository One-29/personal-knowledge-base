"""pytest 共享 fixture：测试库、事务回滚隔离、TestClient。

隔离机制（两层）：
1. 独立测试库 `knowbase_test`——绝不碰开发库；
2. 每个测试跑在一个外层事务里：crud 内部的 `commit()` 只释放 SAVEPOINT
   （`join_transaction_mode="create_savepoint"`），测试结束整体 rollback，
   数据零残留、测试之间互不干扰。

注意：建表用 models 元数据（快），测试库结构与迁移的一致性由 alembic 保证
（CI 阶段改为对测试库执行 `alembic upgrade head`）。
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models  # noqa: F401  注册模型到 Base.metadata
from app.core.config import settings
from app.db import Base, get_db
from app.main import app

# 测试库 URL：把开发库名替换为 knowbase_test（保持其余连接参数一致）
TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/knowbase_test"

engine = create_engine(TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def _prepare_schema() -> Iterator[None]:
    """会话级：建表一次（结构与 models 一致）。

    非 autouse——只有依赖数据库的 fixture（db/client）才触发，
    纯单元测试（chunking/embedding）无需数据库即可运行。
    """
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def db(_prepare_schema) -> Iterator[Session]:
    """每个测试一个事务；crud 的 commit 只落到 SAVEPOINT，结束整体回滚。"""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint"
    )()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def client(db: Session) -> Iterator[TestClient]:
    """TestClient：把 get_db 依赖替换为测试事务会话。"""

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
