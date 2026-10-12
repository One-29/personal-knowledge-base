"""Markdown 结构感知的完整语句组块，原文偏移供引用和上下文补全共用。"""

from dataclasses import dataclass

from .markdown_structure import HEADING, LIST_ITEM, blocks, protected_ranges, sections, sentence_units


class UnsplittableContent(ValueError):
    """一个完整语句/结构超过配置上限；保留原文，禁止静默硬切。"""


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    char_start: int
    char_end: int


def split_markdown(
    text: str,
    max_chars: int = 800,
    overlap_chars: int = 80,
    *,
    protected_spans: tuple[tuple[int, int], ...] = (),
    max_unit_chars: int = 1600,
) -> list[Chunk]:
    """按章节、段落和完整语句组块。

    max_chars 为目标大小；单个不可分割单元允许超出目标但不超过
    max_unit_chars。overlap_chars 是重叠预算，只保留预算内最后一个完整语句；
    不能容纳新内容时取消重叠。返回文本严格等于原文切片，覆盖全部字符。
    """
    if max_chars <= 0 or max_unit_chars < max_chars:
        raise ValueError("max_unit_chars must be at least max_chars > 0")
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap_chars must be between zero and max_chars - 1")
    if not text.strip():
        return []
    protected_spans = tuple(sorted(protected_spans))
    if any(a < 0 or b <= a or b > len(text) for a, b in protected_spans) or any(
        left[1] > right[0] for left, right in zip(protected_spans, protected_spans[1:])
    ):
        raise ValueError("protected spans must be valid, non-overlapping source ranges")
    protected = protected_ranges(text, protected_spans)
    spans: list[tuple[int, int]] = []
    for start, end in sections(text, protected):
        units = []
        for block in blocks(text, start, end, protected):
            if block.end - block.start <= max_chars or block.kind == "structure" or LIST_ITEM.match(text[block.start:block.end]):
                units.append(block)
            else:
                units.extend(sentence_units(text, block.start, block.end, protected))
        for unit in units:
            if unit.end - unit.start > max_unit_chars:
                raise UnsplittableContent(
                    f"原文字符 {unit.start}–{unit.end} 的完整语句或结构超过 {max_unit_chars} 字符，"
                    "请按语义补充段落边界，或调整 CHUNK_MAX_UNIT_CHARS 后更新索引；未截断原文。"
                )
        i = 0
        overlap_start = None
        while i < len(units):
            first = units[i].start if overlap_start is None else overlap_start
            j = i + 1
            while j < len(units) and units[j].end - first <= max_chars:
                j += 1
            if j == i + 1 and j < len(units) and HEADING.match(text[first:units[i].end].lstrip()) and units[j].end - first <= max_unit_chars:
                j += 1
            spans.append((first, units[j - 1].end))
            overlap_start = None
            if j < len(units) and units[j - 1].kind == "prose":
                sentences = sentence_units(text, units[j - 1].start, units[j - 1].end, protected)
                last = sentences[-1]
                if last.end - last.start <= overlap_chars and units[j].end - last.start <= max_chars:
                    overlap_start = last.start
            i = j
    return [Chunk(i, text[a:b], a, b) for i, (a, b) in enumerate(spans)]
