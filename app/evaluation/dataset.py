"""评估清单、固定语料与标注完整性校验。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

EVAL_SET_PATH = Path("eval/eval_set.json")
MIN_BASELINE_LIBRARIES = 5
MIN_BASELINE_ITEMS = 40
_LIBRARY_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")
EvalDifficulty = Literal["smoke", "basic", "regular", "hard"]
DIFFICULTIES: tuple[EvalDifficulty, ...] = ("smoke", "basic", "regular", "hard")


class EvalDatasetError(ValueError):
    """评估清单或固定语料不一致。"""


@dataclass(frozen=True)
class EvalLibrary:
    """一组独立检索范围及其固定 Markdown 语料。"""

    key: str
    name: str
    notes_dir: Path

    def note_paths(self) -> list[Path]:
        return sorted(
            self.notes_dir.glob("*.md"),
            key=lambda path: path.name.casefold(),
        )


@dataclass(frozen=True)
class EvalItem:
    """一条问题标注；库外问题也必须指定被测试的知识库。"""

    question: str
    expected_doc: str | None
    expected_keywords: list[str]
    in_kb: bool
    library: str = "default"
    difficulty: EvalDifficulty = "regular"


@dataclass(frozen=True)
class EvalDataset:
    """经过结构校验的评估清单。"""

    version: int
    description: str
    libraries: dict[str, EvalLibrary]
    items: list[EvalItem]

    @property
    def document_count(self) -> int:
        return sum(len(library.note_paths()) for library in self.libraries.values())


def _record(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EvalDatasetError(f"{label} 必须是 JSON 对象。")
    return value


def _text(value: object, label: str, *, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalDatasetError(f"{label} 必须是非空字符串。")
    result = value.strip()
    if len(result) > max_length:
        raise EvalDatasetError(f"{label} 不能超过 {max_length} 个字符。")
    return result


def _resolve_notes_dir(manifest_dir: Path, raw_path: object, key: str) -> Path:
    relative = Path(_text(raw_path, f"知识库 {key} 的 notes_dir", max_length=240))
    if relative.is_absolute():
        raise EvalDatasetError(f"知识库 {key} 的 notes_dir 必须是相对路径。")
    root = manifest_dir.resolve(strict=False)
    resolved = (root / relative).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise EvalDatasetError(f"知识库 {key} 的 notes_dir 不能越出评估目录。")
    return resolved


def load_eval_set(path: Path = EVAL_SET_PATH) -> EvalDataset:
    """读取并完整校验 v2 多知识库评估清单。"""
    try:
        payload = _record(json.loads(path.read_text(encoding="utf-8")), "评估清单")
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalDatasetError(f"无法读取评估清单 {path}：{exc}") from exc

    version = payload.get("version")
    if version != 2:
        raise EvalDatasetError(f"只支持评估清单 version=2，当前为 {version!r}。")
    description = _text(payload.get("description"), "description", max_length=1000)
    raw_libraries = payload.get("libraries")
    if not isinstance(raw_libraries, list) or not raw_libraries:
        raise EvalDatasetError("libraries 必须是非空数组。")

    libraries: dict[str, EvalLibrary] = {}
    library_names: set[str] = set()
    library_paths: set[Path] = set()
    for index, raw_library in enumerate(raw_libraries, start=1):
        item = _record(raw_library, f"libraries[{index}]")
        key = _text(item.get("key"), f"libraries[{index}].key", max_length=50)
        if _LIBRARY_KEY.fullmatch(key) is None:
            raise EvalDatasetError(
                f"知识库 key {key!r} 只能使用小写字母、数字、下划线和连字符。"
            )
        if key in libraries:
            raise EvalDatasetError(f"知识库 key 重复：{key}。")
        name = _text(item.get("name"), f"知识库 {key} 的 name", max_length=100)
        folded_name = name.casefold()
        if folded_name in library_names:
            raise EvalDatasetError(f"知识库名称重复：{name}。")
        notes_dir = _resolve_notes_dir(path.parent, item.get("notes_dir"), key)
        if notes_dir in library_paths:
            raise EvalDatasetError(f"多个知识库不能共用语料目录：{notes_dir}。")
        libraries[key] = EvalLibrary(key=key, name=name, notes_dir=notes_dir)
        library_names.add(folded_name)
        library_paths.add(notes_dir)

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise EvalDatasetError("items 必须是非空数组。")
    items: list[EvalItem] = []
    seen_questions: set[tuple[str, str]] = set()
    for index, raw_item in enumerate(raw_items, start=1):
        item = _record(raw_item, f"items[{index}]")
        library = _text(item.get("library"), f"items[{index}].library", max_length=50)
        if library not in libraries:
            raise EvalDatasetError(f"items[{index}] 引用了未知知识库 {library!r}。")
        question = _text(item.get("question"), f"items[{index}].question")
        question_key = (library, question.casefold())
        if question_key in seen_questions:
            raise EvalDatasetError(f"知识库 {library} 中的问题重复：{question}。")
        seen_questions.add(question_key)

        difficulty_raw = _text(
            item.get("difficulty"), f"items[{index}].difficulty", max_length=20
        )
        if difficulty_raw not in DIFFICULTIES:
            raise EvalDatasetError(
                f"items[{index}].difficulty 必须是 {list(DIFFICULTIES)} 之一。"
            )

        in_kb = item.get("in_kb")
        if not isinstance(in_kb, bool):
            raise EvalDatasetError(f"items[{index}].in_kb 必须是布尔值。")
        expected_doc_raw = item.get("expected_doc")
        expected_doc = (
            _text(expected_doc_raw, f"items[{index}].expected_doc", max_length=255)
            if expected_doc_raw is not None
            else None
        )
        raw_keywords = item.get("expected_keywords")
        if not isinstance(raw_keywords, list):
            raise EvalDatasetError(f"items[{index}].expected_keywords 必须是数组。")
        keywords = [
            _text(keyword, f"items[{index}].expected_keywords", max_length=120)
            for keyword in raw_keywords
        ]
        if in_kb and (expected_doc is None or not keywords):
            raise EvalDatasetError(
                f"库内问题 {question!r} 必须声明 expected_doc 和 expected_keywords。"
            )
        if not in_kb and (expected_doc is not None or keywords):
            raise EvalDatasetError(
                f"库外问题 {question!r} 的 expected_doc 必须为 null 且关键词必须为空。"
            )
        items.append(
            EvalItem(
                question=question,
                expected_doc=expected_doc,
                expected_keywords=keywords,
                in_kb=in_kb,
                library=library,
                difficulty=difficulty_raw,
            )
        )

    dataset = EvalDataset(
        version=version,
        description=description,
        libraries=libraries,
        items=items,
    )
    validate_eval_sources(dataset)
    return dataset


def validate_eval_sources(dataset: EvalDataset) -> None:
    """证明每条库内标注都指向本库真实文档和真实关键词。"""
    items_by_library = {
        key: [item for item in dataset.items if item.library == key]
        for key in dataset.libraries
    }
    for key, library in dataset.libraries.items():
        if not library.notes_dir.is_dir():
            raise EvalDatasetError(f"知识库 {key} 的语料目录不存在：{library.notes_dir}。")
        note_paths = library.note_paths()
        if not note_paths:
            raise EvalDatasetError(f"知识库 {key} 没有 Markdown 语料。")
        note_by_name: dict[str, Path] = {}
        for note_path in note_paths:
            resolved = note_path.resolve(strict=True)
            if not resolved.is_relative_to(library.notes_dir.resolve(strict=True)):
                raise EvalDatasetError(f"知识库 {key} 的语料文件越出目录：{note_path}。")
            folded = note_path.name.casefold()
            if folded in note_by_name:
                raise EvalDatasetError(f"知识库 {key} 存在大小写冲突文档：{note_path.name}。")
            note_by_name[folded] = note_path

        library_items = items_by_library[key]
        in_items = [item for item in library_items if item.in_kb]
        out_items = [item for item in library_items if not item.in_kb]
        if not in_items or not out_items:
            raise EvalDatasetError(f"知识库 {key} 必须同时包含库内和库外问题。")
        referenced: set[str] = set()
        for item in in_items:
            assert item.expected_doc is not None
            expected_key = item.expected_doc.casefold()
            note_path = note_by_name.get(expected_key)
            if note_path is None:
                raise EvalDatasetError(
                    f"问题 {item.question!r} 的来源文档不在知识库 {key}：{item.expected_doc}。"
                )
            content = note_path.read_text(encoding="utf-8").casefold()
            missing = [
                word
                for word in item.expected_keywords
                if word.casefold() not in content
            ]
            if missing:
                raise EvalDatasetError(
                    f"问题 {item.question!r} 的标注关键词不在 {item.expected_doc}：{missing}。"
                )
            referenced.add(expected_key)
        unreferenced = sorted(set(note_by_name) - referenced)
        if unreferenced:
            raise EvalDatasetError(f"知识库 {key} 存在没有评估问题覆盖的文档：{unreferenced}。")


def assert_baseline_scale(dataset: EvalDataset) -> None:
    """正式基线的最低规模门槛；防止后续误删样本后 CI 仍然全绿。"""
    if len(dataset.libraries) < MIN_BASELINE_LIBRARIES:
        raise EvalDatasetError(
            f"正式基线至少需要 {MIN_BASELINE_LIBRARIES} 个知识库，当前 {len(dataset.libraries)} 个。"
        )
    if len(dataset.items) < MIN_BASELINE_ITEMS:
        raise EvalDatasetError(
            f"正式基线至少需要 {MIN_BASELINE_ITEMS} 条样本，当前 {len(dataset.items)} 条。"
        )
    missing_difficulties = sorted(
        set(DIFFICULTIES) - {item.difficulty for item in dataset.items}
    )
    if missing_difficulties:
        raise EvalDatasetError(f"正式基线缺少难度层级：{missing_difficulties}。")
