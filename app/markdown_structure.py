"""保留原文偏移的 Markdown 结构与语句边界；切分和证据补全共用。"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])[ \t]+")
DEPENDENT_START = re.compile(
    r"^(?:它[们]?|他[们]?|她[们]?|其(?:中|余)?|该|此|这(?:一|个|些|种|时|样)?|那(?:一|个|些|种)?"
    r"|上述|前述|前者|后者|因此|所以|但是|然而|不过|否则|那么|同时|从而|故|则"
    r"|(?:it|its|they|their|this|these|that|those|however|therefore|otherwise)\b)", re.I,
)


@dataclass(frozen=True)
class Unit:
    start: int
    end: int
    kind: str = "prose"


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def protected_ranges(text: str, extra: tuple[tuple[int, int], ...] = ()) -> list[tuple[int, int]]:
    """保护围栏代码、独立公式、表格以及行内代码、公式、图片/链接。"""
    ranges = list(extra)
    pos = 0
    opened: tuple[int, str, int] | None = None
    table_start: int | None = None
    lines = text.splitlines(keepends=True)
    table_lines: set[int] = set()
    separator = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
    for index, line in enumerate(lines):
        if index > 0 and separator.match(line) and "|" in lines[index - 1]:
            table_lines.update((index - 1, index))
            following = index + 1
            while following < len(lines) and "|" in lines[following] and lines[following].strip():
                table_lines.add(following)
                following += 1
    for index, line in enumerate(lines):
        stripped = line.strip()
        fence = FENCE.match(line)
        is_table = opened is None and not fence and index in table_lines
        if table_start is not None and not is_table:
            ranges.append((table_start, pos))
            table_start = None
        if opened is not None:
            start, marker, length = opened
            closed = (
                marker in ("`", "~") and re.fullmatch(re.escape(marker) + "{" + str(length) + r",}\s*", stripped)
            ) or (marker == "$$" and stripped == "$$") or (marker == "\\[" and stripped == "\\]")
            if closed:
                ranges.append((start, pos + len(line)))
                opened = None
        elif fence:
            opened = (pos, fence[1][0], len(fence[1]))
        elif stripped in {"$$", "\\["}:
            opened = (pos, stripped, 0)
        elif is_table and table_start is None:
            table_start = pos
        pos += len(line)
    if opened is not None:
        ranges.append((opened[0], len(text)))
    if table_start is not None:
        ranges.append((table_start, len(text)))
    for pattern in (r"(`+)[\s\S]*?\1", r"\$\$[\s\S]*?\$\$", r"(?<!\\)\$[^\n$]+\$", r"!?\[[^\]\n]*\]\([^\n]*?\)"):
        ranges.extend((m.start(), m.end()) for m in re.finditer(pattern, text))
    return _merge(ranges)


def sections(text: str, protected: list[tuple[int, int]]) -> list[tuple[int, int]]:
    starts = [m.start() for m in re.finditer(r"^ {0,3}#{1,6}[ \t]+", text, re.M)
              if not any(a <= m.start() < b for a, b in protected)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    if len(starts) > 1 and not text[:starts[1]].strip():
        starts.pop(1)
    return [(a, b) for a, b in zip(starts, starts[1:] + [len(text)]) if a < b]


def sentence_units(text: str, start: int, end: int, protected: list[tuple[int, int]]) -> list[Unit]:
    """软换行不结束句子；逗号和分号不分割条件。无句末标点的尾部完整保留。"""
    units: list[Unit] = []
    begin = pos = start
    spans = [(a, b) for a, b in protected if b > start and a < end]
    span_index = 0
    closers: list[str] = []
    pairs = {"（": "）", "(": ")", "[": "]", "【": "】", "“": "”", "‘": "’", "「": "」", "『": "』"}
    while pos < end:
        while span_index < len(spans) and spans[span_index][1] <= pos:
            span_index += 1
        if span_index < len(spans) and spans[span_index][0] <= pos < spans[span_index][1]:
            pos = min(end, spans[span_index][1])
            continue
        char = text[pos]
        if char == '"' and (pos == 0 or text[pos - 1] != "\\"):
            if closers and closers[-1] == '"':
                closers.pop()
            else:
                closers.append('"')
        elif char in pairs:
            closers.append(pairs[char])
        elif closers and char == closers[-1]:
            closers.pop()
        terminal = char in "。！？!?"
        if char == ".":
            token = re.search(r"[^\s]+$", text[begin:pos])
            word = token[0] if token else ""
            terminal = (pos + 1 == end or text[pos + 1].isspace()) and not (
                "://" in word or re.fullmatch(r"(?:[A-Za-z]\.)*[A-Za-z]{1,3}", word)
            )
        stop = pos + 1
        remaining_closers = list(closers)
        if terminal:
            while stop < end and text[stop] in "。！？!?\"”’」』）)]】" and not (
                span_index < len(spans) and spans[span_index][0] <= stop < spans[span_index][1]
            ):
                if remaining_closers and text[stop] == remaining_closers[-1]:
                    remaining_closers.pop()
                stop += 1
            if not remaining_closers:
                while stop < end and text[stop].isspace():
                    stop += 1
                units.append(Unit(begin, stop))
                begin = pos = stop
                closers = []
                continue
        pos += 1
    if begin < end:
        units.append(Unit(begin, end))
    return units


def blocks(text: str, start: int, end: int, protected: list[tuple[int, int]]) -> list[Unit]:
    """空行、标题和列表项构成块边界；保护区中的换行不参与切分。"""
    cuts = {start, end}
    pattern = r"\r?\n[ \t]*\r?\n|^ {0,3}#{1,6}[^\r\n]*\r?\n|^(?=[ \t]*(?:[-+*]|\d+[.)])[ \t]+)"
    for match in re.finditer(pattern, text[start:end], re.M):
        boundary = start + (match.start() if match.start() == match.end() else match.end())
        if start < boundary < end and not any(a < boundary < b for a, b in protected):
            cuts.add(boundary)
    for a, b in protected:
        if start <= a < b <= end and (a == 0 or text[a - 1] == "\n") and (b == len(text) or text[b - 1:b] == "\n"):
            cuts.update((a, b))
    result: list[Unit] = []
    ordered = sorted(cuts)
    for a, b in zip(ordered, ordered[1:]):
        kind = "structure" if any(x <= a and b <= y for x, y in protected) else "prose"
        result.append(Unit(a, b, kind))
    return result


def safe_units(text: str, start: int, end: int, protected: list[tuple[int, int]]) -> list[Unit]:
    result: list[Unit] = []
    for block in blocks(text, start, end, protected):
        if block.kind == "structure" or HEADING.match(text[block.start:block.end]) or LIST_ITEM.match(text[block.start:block.end]):
            result.append(block)
        else:
            result.extend(sentence_units(text, block.start, block.end, protected))
    return result


def depends_on_previous(text: str) -> bool:
    return bool(DEPENDENT_START.match(text.lstrip(" \t\r\n>*-0123456789.)")))
