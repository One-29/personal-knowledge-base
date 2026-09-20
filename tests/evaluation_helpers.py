"""评估测试共享的最小语料与候选构造器。"""

from __future__ import annotations

import json
from pathlib import Path

from app import evaluation
from app.retrieval import RetrievedChunk
from eval import baseline as eval_baseline


def _candidate(chunk_id: int, doc_id: int, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        content=content,
        char_start=0,
        char_end=len(content),
        rrf_score=0.1,
        vector_similarity=0.9,
        vector_rank=chunk_id,
        keyword_rank=None,
    )


def _write_small_eval_set(
    tmp_path: Path,
    library_keys: tuple[str, ...] = ("alpha", "beta"),
) -> tuple[Path, evaluation.EvalDataset]:
    libraries: list[dict[str, object]] = []
    items: list[dict[str, object]] = []
    for key in library_keys:
        notes_dir = tmp_path / "notes" / key
        notes_dir.mkdir(parents=True)
        filename = f"01-{key}.md"
        (notes_dir / filename).write_text(
            f"# {key}\n\nsource keyword {key}\n",
            encoding="utf-8",
        )
        libraries.append(
            {
                "key": key,
                "name": f"评估·{key}",
                "notes_dir": f"notes/{key}",
            }
        )
        items.extend(
            [
                {
                    "library": key,
                    "difficulty": "smoke",
                    "question": f"{key} 的库内问题",
                    "expected_doc": filename,
                    "expected_keywords": [f"keyword {key}"],
                    "in_kb": True,
                },
                {
                    "library": key,
                    "difficulty": "hard",
                    "question": f"{key} 的库外问题",
                    "expected_doc": None,
                    "expected_keywords": [],
                    "in_kb": False,
                },
            ]
        )
    manifest = {
        "version": 2,
        "description": "单元测试评估集",
        "libraries": libraries,
        "items": items,
    }
    manifest_path = tmp_path / "eval_set.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest_path, evaluation.load_eval_set(manifest_path)


def _set_eval_environment(tmp_path: Path, monkeypatch) -> Path:
    eval_storage = tmp_path / "data" / "eval-storage"
    monkeypatch.setattr(eval_baseline, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        eval_baseline.settings,
        "database_url",
        "postgresql+psycopg://postgres@127.0.0.1:5432/knowbase_eval",
    )
    monkeypatch.setattr(eval_baseline.settings, "storage_dir", eval_storage)
    return eval_storage
