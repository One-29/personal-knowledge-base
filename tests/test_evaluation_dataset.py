"""评估清单规模、路径边界与标注完整性回归。"""

from collections import Counter
import json
from pathlib import Path

import pytest

from app import evaluation
from tests.evaluation_helpers import _write_small_eval_set

def test_canonical_eval_set_has_required_scale_and_source_coverage():
    dataset = evaluation.load_eval_set(Path("eval/eval_set.json"))

    evaluation.assert_baseline_scale(dataset)
    assert dataset.version == 2
    assert len(dataset.libraries) == 5
    assert dataset.document_count == 20
    assert len(dataset.items) == 60
    assert sum(item.in_kb for item in dataset.items) == 50
    assert Counter(item.difficulty for item in dataset.items) == {
        "smoke": 5,
        "basic": 15,
        "regular": 20,
        "hard": 20,
    }
    assert Counter(item.library for item in dataset.items) == {
        "calculus": 12,
        "networks": 12,
        "operating-systems": 12,
        "python": 12,
        "databases": 12,
    }
    for key in dataset.libraries:
        scoped = [item for item in dataset.items if item.library == key]
        assert any(item.in_kb for item in scoped)
        assert any(not item.in_kb for item in scoped)


def test_eval_set_rejects_corpus_path_outside_manifest_directory(tmp_path):
    manifest_path, _ = _write_small_eval_set(tmp_path, ("alpha",))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["libraries"][0]["notes_dir"] = "../outside"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(evaluation.EvalDatasetError, match="不能越出评估目录"):
        evaluation.load_eval_set(manifest_path)


def test_eval_set_rejects_annotation_not_present_in_source(tmp_path):
    manifest_path, _ = _write_small_eval_set(tmp_path, ("alpha",))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["items"][0]["expected_keywords"] = ["源文档不存在的词"]
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(evaluation.EvalDatasetError, match="标注关键词不在"):
        evaluation.load_eval_set(manifest_path)


def test_eval_set_rejects_document_without_any_in_kb_question(tmp_path):
    manifest_path, _ = _write_small_eval_set(tmp_path, ("alpha",))
    extra = tmp_path / "notes" / "alpha" / "02-uncovered.md"
    extra.write_text("# 未覆盖文档", encoding="utf-8")

    with pytest.raises(evaluation.EvalDatasetError, match="没有评估问题覆盖"):
        evaluation.load_eval_set(manifest_path)
