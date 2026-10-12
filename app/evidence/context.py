"""在同一份已发布索引中恢复完整语句和必要前文，不改变检索证据分数。"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..markdown_structure import HEADING, depends_on_previous, protected_ranges, safe_units, sections
from ..models import Chunk, Document
from ..retrieval import RetrievedChunk


class ContextIncomplete(ValueError):
    """索引缺字、片段过期或必要上下文超出预算，不能形成完整证据。"""


def _source_text(rows, expected_length: int) -> str:
    end = 0
    pieces: list[str] = []
    for row in rows:
        if row.char_start < 0 or row.char_start > end or row.char_end - row.char_start != len(row.content):
            raise ContextIncomplete("原文索引区间不完整")
        if row.char_end > end:
            pieces.append(row.content[max(0, end - row.char_start):])
            end = row.char_end
    text = "".join(pieces)
    if len(text) != expected_length or any(text[row.char_start:row.char_end] != row.content for row in rows):
        raise ContextIncomplete("索引片段无法还原同一份原文")
    return text


def expand_evidence(
    db: Session,
    anchors: list[RetrievedChunk],
    *,
    max_chars: int,
    neighbor_chars: int,
) -> tuple[list[RetrievedChunk], dict[int, tuple[int, ...]]]:
    """保留命中顺序，追加无分数的必要原始块，返回各块的上下文依赖。

    全文由当前索引区间还原，避免磁盘上尚未发布的修改混入本次问答；不读取
    embedding 列。任何覆盖缺口、冲突和预算不足都显式拒绝，不硬截上下文。
    """
    doc_ids = {c.doc_id for c in anchors}
    documents = dict(db.execute(select(Document.id, Document.char_count).where(Document.id.in_(doc_ids))).all())
    rows_by_doc = defaultdict(list)
    for row in db.execute(
        select(Chunk.id, Chunk.doc_id, Chunk.content, Chunk.char_start, Chunk.char_end)
        .join(Document, Document.id == Chunk.doc_id)
        .where(Chunk.doc_id.in_(doc_ids), Chunk.kb_id == Document.kb_id)
        .order_by(Chunk.doc_id, Chunk.char_start, Chunk.char_end)
    ):
        rows_by_doc[row.doc_id].append(row)
    analyses = {}
    for doc_id, rows in rows_by_doc.items():
        text = _source_text(rows, documents[doc_id])
        protected = protected_ranges(text)
        analyses[doc_id] = (text, [(a, b, safe_units(text, a, b, protected)) for a, b in sections(text, protected)])
    selected = {c.chunk_id: c for c in anchors}
    groups: dict[int, set[int]] = defaultdict(set)
    pending = list(anchors)
    for anchor in pending:
        if anchor.doc_id not in analyses:
            raise ContextIncomplete("命中来源已失效")
        text, regions = analyses[anchor.doc_id]
        rows = rows_by_doc[anchor.doc_id]
        if not any(r.id == anchor.chunk_id and r.content == anchor.content and r.char_start == anchor.char_start and r.char_end == anchor.char_end for r in rows):
            raise ContextIncomplete("命中片段与已发布原文不一致")
        region = next(((a, b, units) for a, b, units in regions if a <= anchor.char_start < anchor.char_end <= b), None)
        if region is None:
            raise ContextIncomplete("片段跨越章节，请更新索引")
        section_start, section_end, units = region
        first = next(i for i, u in enumerate(units) if u.start <= anchor.char_start < u.end)
        last = next(i for i, u in enumerate(units) if u.start < anchor.char_end <= u.end)
        while depends_on_previous(text[units[first].start:units[first].end]):
            if first == 0 or HEADING.match(text[units[first - 1].start:units[first - 1].end]):
                raise ContextIncomplete("片段缺少可确认的前置主语或条件")
            first -= 1
        while last + 1 < len(units) and depends_on_previous(text[units[last + 1].start:units[last + 1].end]):
            last += 1
        start, end = units[first].start, units[last].end
        required = {anchor.chunk_id}
        cursor = start
        while cursor < end:
            covering = [r for r in rows if section_start <= r.char_start <= cursor < r.char_end <= section_end]
            if not covering:
                raise ContextIncomplete("必要上下文无法在同一章节内补齐")
            # 已选块优先，避免因重叠而重复加入被完全覆盖的片段。
            row = max(covering, key=lambda r: (r.id in selected, r.char_end))
            required.add(row.id)
            if row.id not in selected:
                selected[row.id] = RetrievedChunk(
                    chunk_id=row.id, doc_id=row.doc_id, content=row.content,
                    char_start=row.char_start, char_end=row.char_end, rrf_score=0.0,
                    vector_similarity=None, vector_rank=None, keyword_rank=None,
                )
                pending.append(selected[row.id])
            cursor = row.char_end
        added_chars = sum(len(selected[cid].content) for cid in required if cid != anchor.chunk_id)
        if added_chars > neighbor_chars:
            raise ContextIncomplete("必要前后文超过单处补充预算")
        for cid in required:
            groups[cid].update(required - {cid})
    if sum(len(c.content) for c in selected.values()) > max_chars:
        raise ContextIncomplete("完整证据超过本次回答上下文预算")
    return list(selected.values()), {cid: tuple(sorted(ids)) for cid, ids in groups.items()}
