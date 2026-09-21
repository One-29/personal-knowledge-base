"""幂等加载“大一上高等数学演示库”，并输出关联图阈值摘要。"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import crud, graph, ingest, storage
from app.core.config import settings
from app.database import (
    DatabaseLocation,
    DatabaseLocationError,
    initialize_database,
    parse_database_location,
    session_database_location,
)
from app.db import SessionLocal, engine
from app.models import Document, KnowledgeBase
from app.schemas import KnowledgeBaseCreate

DEMO_KB_NAME = "大一上高等数学演示库"
STAGING_KB_NAME = f"{DEMO_KB_NAME}（导入中）"
DEMO_DESCRIPTION = "函数、极限、导数、积分与常微分方程；用于问答、引用和关联图演示"
NOTES_DIR = Path(__file__).resolve().parent / "calculus"
# 使用项目示例配置 BAAI/bge-m3 对本语料实测校准：从“整体关系”逐步收紧到核心关系。
GRAPH_THRESHOLDS = (0.60, 0.68, 0.72, 0.76)
FORBIDDEN_DATABASES = frozenset({"knowbase_test", "knowbase_eval"})
FORBIDDEN_PATH_PARTS = frozenset({"test", "tests", "eval", "evaluation"})


def validate_demo_target(database_url: str) -> None:
    """演示数据只能进入日常/演示实例，绝不写入自动测试或评估数据库。"""
    try:
        location = parse_database_location(database_url)
    except DatabaseLocationError as exc:
        raise RuntimeError("DATABASE_URL 无效，拒绝加载演示知识库") from exc
    if _is_forbidden_demo_location(location):
        target = location.name or str(location.path)
        raise RuntimeError(
            f"目标数据库 {target!r} 属于测试或评估环境，拒绝加载演示知识库"
        )


def validate_demo_database_session(db: Session, database_url: str) -> None:
    """核对 Session 的真实目标，避免配置已变而连接工厂仍指向另一数据库。"""
    configured = parse_database_location(database_url)
    actual = session_database_location(db, database_url)
    if actual != configured:
        raise RuntimeError(
            f"数据库配置指向 {_location_label(configured)}，"
            f"当前连接实际指向 {_location_label(actual)}，拒绝加载演示知识库"
        )
    if _is_forbidden_demo_location(actual):
        raise RuntimeError(
            f"当前连接 {_location_label(actual)!r} 属于测试或评估环境，"
            "拒绝加载演示知识库"
        )


def _is_forbidden_demo_location(location: DatabaseLocation) -> bool:
    if location.backend == "postgresql":
        name = (location.name or "").lower()
        return (
            name in FORBIDDEN_DATABASES
            or name.endswith("_test")
            or name.endswith("_eval")
        )
    if location.path is None:
        return True
    parts = {part.lower() for part in location.path.parts}
    stem_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", location.path.stem.lower())
        if token
    }
    return bool(parts & FORBIDDEN_PATH_PARTS or stem_tokens & {"test", "eval"})


def _location_label(location: DatabaseLocation) -> str:
    return location.name or str(location.path or "未知数据库")


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
    assert settings.database_url is not None
    validate_demo_target(settings.database_url)
    initialize_database(engine)
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
