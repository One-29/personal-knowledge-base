"""幂等加载“大一上高等数学演示库”，并输出关联图阈值摘要。"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import crud, graph, ingest, storage
from app.core.config import settings
from app.db import SessionLocal
from app.models import Document, KnowledgeBase
from app.schemas import KnowledgeBaseCreate

DEMO_KB_NAME = "大一上高等数学演示库"
STAGING_KB_NAME = f"{DEMO_KB_NAME}（导入中）"
DEMO_DESCRIPTION = "函数、极限、导数、积分与常微分方程；用于问答、引用和关联图演示"
NOTES_DIR = Path(__file__).resolve().parent / "calculus"
# 使用项目示例配置 BAAI/bge-m3 对本语料实测校准：从“整体关系”逐步收紧到核心关系。
GRAPH_THRESHOLDS = (0.60, 0.68, 0.72, 0.76)
FORBIDDEN_DATABASES = frozenset({"knowbase_test", "knowbase_eval"})


def database_name(database_url: str) -> str:
    """从 SQLAlchemy URL 取得数据库名，供安全边界与测试复用。"""
    return (make_url(database_url).database or "").lower()


def validate_demo_target(database_url: str) -> None:
    """演示数据只能进入日常/演示实例，绝不写入自动测试或评估数据库。"""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise RuntimeError("DATABASE_URL 无效，拒绝加载演示知识库") from exc
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("演示知识库只允许加载到 PostgreSQL")
    name = database_name(database_url)
    if not name:
        raise RuntimeError("DATABASE_URL 没有数据库名，拒绝加载演示知识库")
    if name in FORBIDDEN_DATABASES or name.endswith("_test") or name.endswith("_eval"):
        raise RuntimeError(f"目标数据库 {name!r} 属于测试或评估环境，拒绝加载演示知识库")


def validate_demo_database_session(db: Session, database_url: str) -> None:
    """核对 Session 的真实目标，避免配置已变而连接工厂仍指向另一数据库。"""
    configured = database_name(database_url)
    actual = str(db.scalar(select(func.current_database())) or "").lower()
    if actual != configured:
        raise RuntimeError(
            f"数据库配置指向 {configured or '未知数据库'}，"
            f"当前连接实际指向 {actual or '未知数据库'}，拒绝加载演示知识库"
        )
    if actual in FORBIDDEN_DATABASES or actual.endswith("_test") or actual.endswith("_eval"):
        raise RuntimeError(f"当前连接 {actual!r} 属于测试或评估环境，拒绝加载演示知识库")


def note_paths() -> list[Path]:
    """按文件名稳定返回演示语料；空目录视为配置错误。"""
    paths = sorted(NOTES_DIR.glob("*.md"))
    if not paths:
        raise RuntimeError(f"没有找到演示文档：{NOTES_DIR}")
    return paths


def _delete_kb(db: Session, kb: KnowledgeBase | None) -> None:
    if kb is None:
        return
    kb_id = kb.id
    if not crud.delete_kb(db, kb_id):
        raise RuntimeError(f"删除旧演示知识库失败：kb_id={kb_id}")
    storage.delete_kb_dir(kb_id)


def _create_staging_kb(db: Session) -> KnowledgeBase:
    stale = crud.get_kb_by_name(db, STAGING_KB_NAME)
    _delete_kb(db, stale)
    return crud.create_kb(
        db,
        KnowledgeBaseCreate(
            name=STAGING_KB_NAME,
            description="演示语料正在导入；全部文档成功后自动切换为正式名称",
        ),
    )


def _add_and_process_document(db: Session, kb_id: int, path: Path) -> Document:
    content = path.read_bytes()
    text = content.decode("utf-8")
    doc = Document(
        kb_id=kb_id,
        title=path.name,
        file_path="",
        content_hash=hashlib.sha256(content).hexdigest(),
        char_count=len(text),
    )
    db.add(doc)
    db.flush()
    try:
        doc.file_path = storage.save(kb_id, doc.id, content)
        db.commit()
    except Exception:
        db.rollback()
        storage.delete_kb_dir(kb_id)
        raise

    ingest.process_document(doc.id, db)
    db.refresh(doc)
    if doc.status != ingest.STATUS_READY or doc.chunk_count < 1:
        detail = doc.last_error_message or doc.last_error_code or "未知错误"
        raise RuntimeError(f"文档 {path.name} 处理失败：{detail}")
    return doc


def replace_demo_kb(db: Session) -> tuple[int, list[Document]]:
    """先完整构建临时库，再原子切换名称；失败时保留旧的正式演示库。"""
    old = crud.get_kb_by_name(db, DEMO_KB_NAME)
    staging = _create_staging_kb(db)
    staging_id = staging.id
    documents: list[Document] = []

    try:
        for path in note_paths():
            documents.append(_add_and_process_document(db, staging_id, path))

        old_id = old.id if old is not None else None
        if old is not None:
            db.delete(old)
            db.flush()
        staging = db.get(KnowledgeBase, staging_id)
        if staging is None:
            raise RuntimeError("演示知识库在名称切换前消失")
        staging.name = DEMO_KB_NAME
        staging.description = DEMO_DESCRIPTION
        db.commit()
    except Exception:
        db.rollback()
        failed = db.get(KnowledgeBase, staging_id)
        if failed is not None:
            _delete_kb(db, failed)
        raise

    if old_id is not None:
        # 正式名称切换已经提交；旧目录清理失败时保留新库，避免异常清理
        # 把刚导入成功的数据也删除。
        storage.delete_kb_dir(old_id)
    return staging_id, documents


def graph_summary(db: Session, kb_id: int) -> list[tuple[float, int, int]]:
    """返回 (阈值, 节点数, 边数)，用于演示前核对阈值确实产生变化。"""
    result: list[tuple[float, int, int]] = []
    for threshold in GRAPH_THRESHOLDS:
        data = graph.build_graph(db, kb_id, min_similarity=threshold)
        result.append((threshold, len(data.nodes), len(data.edges)))
    return result


def main() -> int:
    validate_demo_target(settings.database_url)
    db = SessionLocal()
    try:
        validate_demo_database_session(db, settings.database_url)
        kb_id, documents = replace_demo_kb(db)
        ready_count = db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.kb_id == kb_id, Document.status == ingest.STATUS_READY)
        )
        print(f"[演示库] kb_id={kb_id}，文档 {len(documents)} 篇，ready={ready_count}")
        edge_counts: list[int] = []
        for threshold, nodes, edges in graph_summary(db, kb_id):
            edge_counts.append(edges)
            print(f"  连边阈值 {threshold:.2f}：节点 {nodes}，边 {edges}")
        if any(left <= right for left, right in zip(edge_counts, edge_counts[1:])):
            print("警告：当前 Embedding 模型下四档阈值没有逐档减少边数，请重新校准阈值。")
            return 2
        print("演示知识库加载完成。刷新 KnowBase 页面后即可选择使用。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
