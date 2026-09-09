"""切分器单元测试（04 §2 DR1 的可测契约）。

不依赖数据库与外部服务——纯文本函数的快速回归网。
"""

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
