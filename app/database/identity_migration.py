"""SQLite v2→v3：来源 ID 不复用；原行、外键、索引及触发器原子保留。"""

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import CreateTable


def upgrade_identity_tables(engine: Engine) -> None:
    """启动时独占写锁升级；任何异常回滚 DDL/数据并恢复连接的外键检查。"""
    from app.models import Chunk, Document, KnowledgeBase

    with engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            # sqlite3 的旧事务模式不会为 DDL 自动 BEGIN，必须显式开启事务。
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            version = connection.scalar(text("SELECT value FROM app_metadata WHERE key='schema_version'"))
            if version == "3":
                connection.commit()
                return
            if version != "2":
                raise RuntimeError("SQLite 来源 ID 升级需要 schema 2")
            for model in (KnowledgeBase, Document, Chunk):
                _rebuild_table(connection, model.__table__)
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise RuntimeError("SQLite 来源 ID 升级未通过外键检查，已回滚")
            connection.execute(text("UPDATE app_metadata SET value='3' WHERE key='schema_version'"))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql(f"PRAGMA foreign_keys={int(foreign_keys)}")
            connection.commit()


def _rebuild_table(connection: Connection, table) -> None:
    name = table.name
    temporary = f"{name}__identity_v3"
    quote = connection.dialect.identifier_preparer.quote
    objects = connection.execute(text(
        "SELECT sql FROM sqlite_master WHERE tbl_name=:name "
        "AND type IN ('index','trigger') AND sql IS NOT NULL"
    ), {"name": name}).scalars().all()
    has_sequence = connection.scalar(text("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"))
    previous_sequence = connection.scalar(text("SELECT seq FROM sqlite_sequence WHERE name=:name"),
                                          {"name": name}) if has_sequence else None
    ddl = str(CreateTable(table).compile(dialect=connection.dialect))
    ddl = ddl.replace(f"CREATE TABLE {quote(name)}", f"CREATE TABLE {quote(temporary)}", 1)
    connection.exec_driver_sql(ddl)
    columns = ", ".join(quote(column.name) for column in table.columns)
    connection.exec_driver_sql(f"INSERT INTO {quote(temporary)} ({columns}) SELECT {columns} FROM {quote(name)}")
    connection.exec_driver_sql(f"DROP TABLE {quote(name)}")
    connection.exec_driver_sql(f"ALTER TABLE {quote(temporary)} RENAME TO {quote(name)}")
    for statement in objects:
        connection.exec_driver_sql(statement)
    if previous_sequence is not None:
        connection.execute(text("UPDATE sqlite_sequence SET seq=max(seq,:seq) WHERE name=:name"),
                           {"name": name, "seq": previous_sequence})
