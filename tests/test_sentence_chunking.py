"""切分不截掉条件、原文位置和 Markdown 原子结构的回归。"""

import pytest

from app.chunking import UnsplittableContent, split_markdown


def assert_literal_coverage(text, chunks):
    end = 0
    for chunk in chunks:
        assert chunk.text == text[chunk.char_start:chunk.char_end]
        assert chunk.char_start <= end < chunk.char_end
        end = chunk.char_end
    assert end == len(text)


def test_negation_cannot_be_lost_at_overlap_start():
    sentence = "如果不开启缓存，性能会下降。"
    text = "背景。" * 239 + sentence + "后续说明。" * 50
    chunks = split_markdown(text)
    for chunk in chunks:
        if "开启缓存，性能会下降" in chunk.text:
            assert sentence in chunk.text
    assert_literal_coverage(text, chunks)


@pytest.mark.parametrize("sentence", [
    "如果不\n开启缓存，性能会下降。",
    "只有满足这些条件，才能启用缓存。",
    "并非开启缓存会导致变慢，而是缓存失效时需要重新查询。",
    "他说：“如果不开启缓存，性能会下降。”",
    "版本为 3.14，地址是 https://example.com/a.b，不能按句点拆开。",
    "若 f(x)>0（其中 x∈(0,1)），则结论成立。",
])
def test_complete_conditional_is_atomic(sentence):
    text = "前文。" * 8 + sentence + "后文。" * 8
    start = text.index(sentence)
    end = start + len(sentence)
    chunks = split_markdown(text, max_chars=32, overlap_chars=8)
    assert all(not start < point < end for c in chunks for point in (c.char_start, c.char_end))
    assert_literal_coverage(text, chunks)


@pytest.mark.parametrize("structure", [
    "```python\n# 不是标题\nprint('条件。结果。')\n```\n",
    "~~~text\n## 也不是标题\n若未开启，不能继续。\n~~~\n",
    "$$\nF(x)=\n\\begin{cases}0 & x<0\\\\1 & x>0\\end{cases}\n$$\n",
    "| 条件 | 结果 |\n| --- | --- |\n| 不开启 | 性能下降。 |\n",
    "条件 | 结果\n--- | ---\n不开启 | 性能下降。\n",
    "- 如果不开启缓存，性能会下降。\n  这一点适用于重复查询。\n",
])
def test_markdown_structure_is_kept_whole(structure):
    text = "# 标题\r\n\r\n前文。\n\n" + structure + "\n后文。\n# 下一节\n其他内容。"
    start = text.index(structure)
    end = start + len(structure.rstrip())
    chunks = split_markdown(text, max_chars=20, overlap_chars=3)
    assert all(not start < point < end for c in chunks for point in (c.char_start, c.char_end))
    assert_literal_coverage(text, chunks)


def test_oversized_sentence_stays_whole_or_fails_explicitly():
    text = "如果不" + "执行必要检查，" * 140 + "将无法继续。"
    chunks = split_markdown(text)
    assert len(chunks) == 1 and len(chunks[0].text) > 800
    with pytest.raises(UnsplittableContent, match="未截断原文"):
        split_markdown(text * 2, max_unit_chars=850)


def test_chinese_math_bars_are_not_a_markdown_table():
    text = "当 n>N 时 |a_n-A|<ε。函数极限讨论 x 趋近某点。"
    chunks = split_markdown(text, max_chars=24, overlap_chars=0)
    assert len(chunks) == 2


def test_image_offsets_and_crlf_are_preserved():
    text = "# 图片\r\n\r\n说明。" + "![图。](a(1).png)" + "说明。![另一图](b.png)\r\n"
    spans = tuple((text.index(s), text.index(s) + len(s)) for s in ("![图。](a(1).png)", "![另一图](b.png)"))
    chunks = split_markdown(text, max_chars=22, overlap_chars=5, protected_spans=spans)
    assert_literal_coverage(text, chunks)
    assert all(not a < point < b for a, b in spans for c in chunks for point in (c.char_start, c.char_end))
