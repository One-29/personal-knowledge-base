"""评估入口：建评估语料库 → 跑检索与拒答评估 → 输出报告。

用法（项目根、venv 激活、数据库容器运行中）：
    python -m eval.run_eval                # 完整评估（检索 + 拒答 + τ 扫描）
    python -m eval.run_eval --retrieval    # 只跑检索评估（不消耗 LLM）
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import delete, select

from app import crud, evaluation, ingest, storage
from app.db import SessionLocal
from app.models import Chunk, Document, KnowledgeBase
from app.schemas import KnowledgeBaseCreate

KB_NAME = "评估语料库"
NOTES_DIR = Path("eval/notes")


def build_eval_kb(db) -> int:
    """重建评估语料库：固定语料保证结果可复现。"""
    old = crud.get_kb_by_name(db, KB_NAME)
    if old is not None:
        db.execute(delete(Chunk).where(Chunk.kb_id == old.id))
        db.execute(delete(Document).where(Document.kb_id == old.id))
        db.delete(old)
        db.commit()

    kb = crud.create_kb(db, KnowledgeBaseCreate(name=KB_NAME, description="检索质量评估专用"))
    for path in sorted(NOTES_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        doc = Document(kb_id=kb.id, title=path.name, file_path="",
                       content_hash=f"eval-{path.name}", char_count=len(text))
        db.add(doc)
        db.flush()
        doc.file_path = storage.save(kb.id, doc.id, text.encode("utf-8"))
        db.commit()
        ingest.process_document(doc.id, db)
    return kb.id


def main() -> int:
    parser = argparse.ArgumentParser(description="KnowBase 检索质量评估")
    parser.add_argument("--retrieval", action="store_true", help="只跑检索评估（不调 LLM）")
    parser.add_argument("--top-k", type=int, default=None, help="检索评估的候选数")
    args = parser.parse_args()

    items = evaluation.load_eval_set()
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
