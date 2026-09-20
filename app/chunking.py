"""Markdown 结构感知切分（04 §2 DR1）。

切分是检索质量的第一变量：块是检索与溯源的原子单元。
策略：
1. 标题边界优先——标题与其下正文同块（标题是块的语义标签）；
2. 超长段按换行边界二次切分，块间保留重叠（防结论被截断）；
3. 记录块在原文中的字符区间 [char_start, char_end)，作为溯源高亮锚点
   （与切分策略解耦：调参只改块边界，锚点机制不变）。

纯函数、无 IO，参数由配置注入（06 评估阶段用网格扫描回调）。
"""

import re
from dataclasses import dataclass

# ATX 标题：行首 1-6 个 # 后跟空格
_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """一个切块：文本 + 原文绝对字符区间（溯源锚点）。"""

    index: int
    text: str
    char_start: int
    char_end: int


def _split_by_headings(text: str) -> list[tuple[int, int]]:
    """按标题切分为段落区间（每个区间从标题开始，含标题行）。"""
    starts = [m.start() for m in _HEADING_RE.finditer(text)]
    if not starts:
        return [(0, len(text))] if text.strip() else []
    if starts[0] != 0:
        # 有内容的引言独立成段；纯前导空白并入首个标题，避免块区间从中途开始。
        if text[:starts[0]].strip():
            starts.insert(0, 0)
        else:
            starts[0] = 0
    bounds: list[tuple[int, int]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        if text[start:end].strip():
            bounds.append((start, end))
    return bounds


def _containing_span(
    boundary: int,
    protected_spans: tuple[tuple[int, int], ...],
) -> tuple[int, int] | None:
    """边界严格落在不可切断区间内部时返回该区间。"""
    return next(
        ((start, end) for start, end in protected_spans if start < boundary < end),
        None,
    )


def _window(
    segment: str,
    offset: int,
    max_chars: int,
    overlap: int,
    protected_spans: tuple[tuple[int, int], ...] = (),
) -> list[tuple[int, int]]:
    """超长段落按窗口切分；Markdown 图片语法等保护区间不会被边界截断。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < len(segment):
        previous_end = spans[-1][1] - offset if spans else -1
        end = min(pos + max_chars, len(segment))
        if end < len(segment):                       # 尽量在换行处断开
            newline = segment.rfind("\n", pos, end)
            # 回缩后的块必须长于重叠区，否则下一窗口无法前进。
            # 回退保护区起点后，不能再次选到已被前块完全覆盖的换行边界。
            if (
                newline > pos
                and newline + 1 - pos > overlap
                and newline + 1 > previous_end
            ):
                end = newline + 1
            protected = _containing_span(offset + end, protected_spans)
            if protected is not None:
                # 图片语法通常很短；必要时允许本块略超 max_chars，以换取位置原子性。
                end = min(len(segment), protected[1] - offset)
        spans.append((offset + pos, offset + end))
        if end >= len(segment):
            break
        next_pos = end - overlap
        protected = _containing_span(offset + next_pos, protected_spans)
        if protected is not None:
            adjusted = protected[0] - offset
            # 回退到保护区开头后，下个窗口仍须越过当前块尾；否则会生成一个
            # 完全被当前块覆盖的冗余块。极长保护区间从其末尾继续。
            next_pos = (
                adjusted
                if adjusted > pos and adjusted + max_chars > end
                else protected[1] - offset
            )
        pos = max(pos + 1, next_pos)
    return spans


def split_markdown(
    text: str,
    max_chars: int = 800,
    overlap_chars: int = 80,
    *,
    protected_spans: tuple[tuple[int, int], ...] = (),
) -> list[Chunk]:
    """把 Markdown 原文切成块（含原文偏移）。

    :param text: 原文（UTF-8 解码后的字符串）
    :param max_chars: 单块字符上限（≈512 token 量级，见 04 §2）
    :param overlap_chars: 相邻块重叠字符数（≈10%）
    :param protected_spans: 不允许切块边界穿过的原文区间（一期用于图片语法）
    :return: 按原文顺序排列的块列表；空文本返回空列表
    :raises ValueError: 上限非正，或重叠不在 [0, max_chars) 内
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap_chars must be between zero and max_chars - 1")
    if not text.strip():
        return []

    protected_spans = tuple(sorted(protected_spans))
    if any(start < 0 or end <= start or end > len(text) for start, end in protected_spans):
        raise ValueError("protected spans must be valid source ranges")
    if any(left[1] > right[0] for left, right in zip(protected_spans, protected_spans[1:])):
        raise ValueError("protected spans must not overlap")

    spans: list[tuple[int, int]] = []
    segments = _split_by_headings(text)
    # 极少数多行图片语法可能跨过看似标题的行；先合并该标题边界。
    merged_segments: list[tuple[int, int]] = []
    for seg_start, seg_end in segments:
        if merged_segments and _containing_span(seg_start, protected_spans) is not None:
            merged_segments[-1] = (merged_segments[-1][0], seg_end)
        else:
            merged_segments.append((seg_start, seg_end))
    for seg_start, seg_end in merged_segments:
        segment = text[seg_start:seg_end]
        if len(segment) <= max_chars:
            spans.append((seg_start, seg_end))
        else:
            spans.extend(
                _window(segment, seg_start, max_chars, overlap_chars, protected_spans)
            )

    return [
        Chunk(index=i, text=text[start:end], char_start=start, char_end=end)
        for i, (start, end) in enumerate(spans)
    ]
