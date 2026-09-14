r"""评估入口：建评估语料库 → 跑检索与拒答评估 → 输出报告。

Windows 推荐入口（自动使用 knowbase_eval + data/eval-storage）：
    .\scripts\run-eval.ps1                # 完整评估（检索 + 拒答 + τ 扫描）
    .\scripts\run-eval.ps1 -Retrieval     # 只跑检索评估（不消耗回答 LLM）

直接运行 ``python -m eval.run_eval`` 只适用于已经显式设置上述隔离环境的进程；
日常 ``knowbase + data/storage`` 配置会在任何数据库操作前被拒绝。
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app import crud, evaluation, ingest, storage
from app.core.config import settings
from app.db import SessionLocal
from app.models import Document, KnowledgeBase
from app.schemas import KnowledgeBaseCreate

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_NAME = "评估语料库"
STAGING_KB_NAME = f"{KB_NAME}（导入中）"
NOTES_DIR = PROJECT_ROOT / "eval" / "notes"
EVAL_SET_PATH = PROJECT_ROOT / "eval" / "eval_set.json"
EVAL_DATABASE_NAME = "knowbase_eval"


class UnsafeEvalEnvironmentError(ValueError):
    """评估环境可能影响日常数据时抛出。"""


def _resolve_path(path: str | Path, base_dir: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve(strict=False)


def validate_eval_environment(
    database_url: str,
    storage_dir: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
    working_directory: Path | None = None,
) -> None:
    """确认评估只会修改专用 PostgreSQL 数据库和隔离的原文目录。

    该函数只解析配置，不建立数据库连接，便于在执行任何删除操作前调用和测试。
    """
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise UnsafeEvalEnvironmentError("评估数据库连接地址无效。") from exc

    if url.get_backend_name() != "postgresql":
        raise UnsafeEvalEnvironmentError("评估只允许使用 PostgreSQL 数据库。")
    if url.database != EVAL_DATABASE_NAME:
        raise UnsafeEvalEnvironmentError(
            f"评估只允许修改专用数据库 {EVAL_DATABASE_NAME}。"
        )

    root = project_root.resolve(strict=False)
    current_directory = (working_directory or Path.cwd()).resolve(strict=False)
    configured_storage = _resolve_path(storage_dir, current_directory)
    expected_storage = _resolve_path(Path("data/eval-storage"), root)
    if configured_storage != expected_storage:
        raise UnsafeEvalEnvironmentError(
            f"评估原文目录必须精确使用隔离目录 {expected_storage}。"
        )


def validate_eval_database_session(db) -> None:
    """核对传入 Session 的真实数据库，阻止配置与连接工厂不一致时误删数据。"""
    actual_database = str(
        db.scalar(select(func.current_database())) or ""
    ).lower()
    if actual_database != EVAL_DATABASE_NAME:
        raise UnsafeEvalEnvironmentError(
            f"当前数据库连接实际指向 {actual_database or '未知数据库'}；"
            f"评估只允许修改 {EVAL_DATABASE_NAME}。"
        )


def _delete_eval_kb(db, kb: KnowledgeBase | None) -> None:
    if kb is None:
        return
    kb_id = kb.id
    if not crud.delete_kb(db, kb_id):
        raise RuntimeError(f"删除评估知识库失败：kb_id={kb_id}")
    storage.delete_kb_dir(kb_id)


def _create_staging_kb(db) -> KnowledgeBase:
    stale = crud.get_kb_by_name(db, STAGING_KB_NAME)
    _delete_eval_kb(db, stale)
    return crud.create_kb(
        db,
        KnowledgeBaseCreate(
            name=STAGING_KB_NAME,
            description="评估语料正在导入；全部文档成功后自动切换",
        ),
    )


def build_eval_kb(
    db,
    *,
    notes_dir: Path = NOTES_DIR,
) -> int:
    """在临时库完整建好固定语料，再原子替换正式评估库。"""
    validate_eval_environment(
        settings.database_url,
        settings.storage_dir,
        project_root=PROJECT_ROOT,
    )
    validate_eval_database_session(db)

    paths = sorted(notes_dir.glob("*.md"))
    if not paths:
        raise RuntimeError(f"没有找到评估文档：{notes_dir}")
    old = crud.get_kb_by_name(db, KB_NAME)
    staging = _create_staging_kb(db)
    staging_id = staging.id

    try:
        for path in paths:
            document_text = path.read_text(encoding="utf-8")
            doc = Document(
                kb_id=staging_id,
                title=path.name,
                file_path="",
                content_hash=f"eval-{path.name}",
                char_count=len(document_text),
            )
            db.add(doc)
            db.flush()
            doc.file_path = storage.save(
                staging_id, doc.id, document_text.encode("utf-8")
            )
            db.commit()
            ingest.process_document(doc.id, db)
            db.refresh(doc)
            if doc.status != ingest.STATUS_READY or doc.chunk_count < 1:
                detail = doc.last_error_message or doc.last_error_code or "未知错误"
                raise RuntimeError(f"评估文档 {path.name} 处理失败：{detail}")

        old_id = old.id if old is not None else None
        if old is not None:
            db.delete(old)
            db.flush()
        staging = db.get(KnowledgeBase, staging_id)
        if staging is None:
            raise RuntimeError("评估知识库在名称切换前消失")
        staging.name = KB_NAME
        staging.description = "检索质量评估专用"
        db.commit()
    except Exception:
        db.rollback()
        failed = db.get(KnowledgeBase, staging_id)
        if failed is not None:
            _delete_eval_kb(db, failed)
        else:
            storage.delete_kb_dir(staging_id)
        raise

    if old_id is not None:
        # 数据库切换已提交。即使旧原文清理失败，也不能进入上面的异常分支
        # 删除刚建好的正式评估库。
        storage.delete_kb_dir(old_id)
    return staging_id


def main() -> int:
    parser = argparse.ArgumentParser(description="KnowBase 检索质量评估")
    parser.add_argument("--retrieval", action="store_true", help="只跑检索评估（不调 LLM）")
    parser.add_argument("--top-k", type=int, default=None, help="检索评估的候选数")
    args = parser.parse_args()

    try:
        validate_eval_environment(settings.database_url, settings.storage_dir)
    except UnsafeEvalEnvironmentError as exc:
        parser.error(str(exc))

    items = evaluation.load_eval_set(EVAL_SET_PATH)
    db = SessionLocal()
    try:
        kb_id = build_eval_kb(db)
        docs = db.scalars(select(Document).where(Document.kb_id == kb_id)).all()
        print(f"[评估库] kb_id={kb_id} 文档 {len(docs)} 篇 / 样本 {len(items)} 条")

        print("\n=== Q1 检索质量 ===")
        metrics = evaluation.evaluate_retrieval(db, items, kb_id, top_k=args.top_k)
        print(metrics.summary())

        if args.retrieval:
            return 0

        print("\n=== Q2 拒答质量（真 LLM） ===")
        refusal = evaluation.evaluate_refusal(db, items, kb_id)
        print(refusal.summary())

        print("\n=== Q3 τ 扫描（本地模拟 L1，零 LLM 调用） ===")
        samples = evaluation.collect_similarities(db, items, kb_id)
        print(f"{'τ':>6} | {'库外拒答率':>10} | {'库内误拒率':>10}")
        for threshold, result in evaluation.simulate_thresholds(
            samples, [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
        ):
            print(f"{threshold:>6.2f} | {result.out_of_kb_refusal_rate:>10.3f} | "
                  f"{result.in_kb_false_refusal_rate:>10.3f}")

        print("\n=== 相似度分布（用于判断 τ 的可分性） ===")
        for item, similarity in samples:
            tag = "库内" if item.in_kb else "库外"
            value = f"{similarity:.3f}" if similarity is not None else "无候选"
            print(f"  [{tag}] {value}  {item.question}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
