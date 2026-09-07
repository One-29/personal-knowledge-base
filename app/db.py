"""数据库引擎与会话管理。

职责边界（02 M1/M2 的存储底座）：
- engine：进程级连接池，应用启动时创建一次
- SessionLocal：会话工厂，每个请求一个会话（见 get_db）
- Base：所有 ORM 模型的声明基类（models.py 从这里继承）
- get_db：FastAPI 依赖，yield 会话 + 请求结束关闭
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """ORM 模型基类：SQLAlchemy 2.x 声明式（Mapped/mapped_column）。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话，用完即关（生命周期跟随请求）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
