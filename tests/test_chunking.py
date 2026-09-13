"""切分器单元测试（04 §2 DR1 的可测契约）。

不依赖数据库与外部服务——纯文本函数的快速回归网。
"""

import pytest

from app.chunking import split_markdown

MD = """# TCP 三次握手

客户端发送 SYN，服务端回复 SYN+ACK，客户端再回 ACK。

## 为什么是三次

两次无法确认客户端的接收能力。

## 四次挥手

FIN、ACK、FIN、ACK 四步。
"""


def test_empty_text_returns_no_chunks():
    """空文本 / 纯空白 → 无块。"""
    assert split_markdown("") == []
    assert split_markdown("   \n\t ") == []


def test_short_text_single_chunk_with_full_span():
    """短文本 → 单块，偏移覆盖全文。"""
    chunks = split_markdown("# 标题\n正文")
    assert len(chunks) == 1
    assert chunks[0].text == "# 标题\n正文"
    assert (chunks[0].char_start, chunks[0].char_end) == (0, len("# 标题\n正文"))
    assert chunks[0].index == 0


def test_headings_start_new_chunks_and_keep_title():
    """每个标题开启新块，且标题保留在块首（语义标签）。"""
    chunks = split_markdown(MD)
    assert len(chunks) == 3
    assert chunks[0].text.startswith("# TCP 三次握手")
    assert chunks[1].text.startswith("## 为什么是三次")
    assert chunks[2].text.startswith("## 四次挥手")


def test_offsets_reconstruct_original_text():
    """偏移可切回原文（溯源锚点的核心契约）。"""
    chunks = split_markdown(MD)
    for chunk in chunks:
        assert MD[chunk.char_start : chunk.char_end] == chunk.text


def test_long_segment_split_with_overlap():
    """超长段落 → 多块，相邻块存在重叠，且偏移仍可还原原文。"""
    text = "# 长文\n" + "".join(f"第{i}段内容。\n" for i in range(200))
    chunks = split_markdown(text, max_chars=300, overlap_chars=50)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 300
        assert text[chunk.char_start : chunk.char_end] == chunk.text
    # 相邻块重叠：前块尾部与后块头部有交集
    assert chunks[0].char_end > chunks[1].char_start


def test_no_heading_plain_text_single_or_windowed():
    """无标题纯文本：短则一块，长则按窗口切。"""
    assert len(split_markdown("这是一段没有标题的纯文本。")) == 1
    long_text = "句子。" * 500
    chunks = split_markdown(long_text, max_chars=200, overlap_chars=20)
    assert len(chunks) > 1
    assert all(len(c.text) <= 200 for c in chunks)


@pytest.mark.parametrize(
    ("text", "max_chars", "overlap"),
    [
        ("# 标题\n" + "中" * 900 + "\n" + "文" * 900, 800, 80),
        ("甲" * 150 + "\n" + "乙" * 300 + "\n" + "丙" * 300, 300, 80),
        ("一\n" + "二" * 50, 10, 9),
        ("一\n二\n三\n四\n五\n", 4, 0),
        ("逐字切块", 1, 0),
        ("# 前言\n短段\n# 长段\n" + "长" * 100, 20, 5),
    ],
)
def test_window_spans_cover_every_character_without_gaps(text, max_chars, overlap):
    """含长行、标题与极端重叠的原文都可由块区间无损拼回。"""
    chunks = split_markdown(text, max_chars=max_chars, overlap_chars=overlap)
    covered_end = 0
    reconstructed = ""
    previous_start = -1
    for chunk in chunks:
        assert previous_start < chunk.char_start <= covered_end
        assert chunk.char_start < chunk.char_end <= len(text)
        assert len(chunk.text) <= max_chars
        assert chunk.text == text[chunk.char_start:chunk.char_end]
        assert chunk.char_end > covered_end
        reconstructed += chunk.text[covered_end - chunk.char_start:]
        covered_end = chunk.char_end
        previous_start = chunk.char_start
    assert covered_end == len(text)
    assert reconstructed == text


def test_window_overlap_uses_actual_newline_boundary():
    """换行导致块尾提前时仍保留指定重叠，不跳过其后的长行。"""
    text = "甲" * 150 + "\n" + "乙" * 500
    chunks = split_markdown(text, max_chars=300, overlap_chars=80)
    assert chunks[0].char_end == 151
    assert chunks[1].char_start == 71


@pytest.mark.parametrize(
    ("max_chars", "overlap"), [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)]
)
def test_invalid_window_parameters_raise(max_chars, overlap):
    """非法窗口参数应立即失败，避免死循环或静默漏字。"""
    with pytest.raises(ValueError):
        split_markdown("需要切分的文本", max_chars=max_chars, overlap_chars=overlap)
