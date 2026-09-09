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
    if starts[0] != 0:                       # 首个标题前的引言部分单独成段
        starts.insert(0, 0)
    bounds: list[tuple[int, int]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        if text[start:end].strip():
            bounds.append((start, end))
    return bounds


def _window(segment: str, offset: int, max_chars: int, overlap: int) -> list[tuple[int, int]]:
    """超长段落按窗口切分：窗口 max_chars，步长 max_chars - overlap，边界优先对齐换行。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    step = max(max_chars - overlap, 1)
    while pos < len(segment):
        end = min(pos + max_chars, len(segment))
        if end < len(segment):                       # 尽量在换行处断开
            newline = segment.rfind("\n", pos, end)
            if newline > pos:
                end = newline + 1
        spans.append((offset + pos, offset + end))
        if end >= len(segment):
            break
        pos += step
    return spans


def split_markdown(text: str, max_chars: int = 800, overlap_chars: int = 80) -> list[Chunk]:
    """把 Markdown 原文切成块（含原文偏移）。

    :param text: 原文（UTF-8 解码后的字符串）
    :param max_chars: 单块字符上限（≈512 token 量级，见 04 §2）
    :param overlap_chars: 相邻块重叠字符数（≈10%）
    :return: 按原文顺序排列的块列表；空文本返回空列表
    """
    if not text.strip():
        return []

    spans: list[tuple[int, int]] = []
    for seg_start, seg_end in _split_by_headings(text):
        segment = text[seg_start:seg_end]
        if len(segment) <= max_chars:
            spans.append((seg_start, seg_end))
        else:
            spans.extend(_window(segment, seg_start, max_chars, overlap_chars))

    return [
        Chunk(index=i, text=text[start:end], char_start=start, char_end=end)
        for i, (start, end) in enumerate(spans)
    ]
