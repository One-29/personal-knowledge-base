"""检索层测试（M3）：RRF 融合（纯函数）+ 双通道与端到端检索（真库）。

embedding 由 conftest 的假 provider 提供（按文本 hash 的确定性伪向量）——
向量通道在此验证「能查到、过滤正确、排序规则正确」，
真实语义相似度由 06 评估阶段用真向量测。关键词通道用真 pg_trgm。
"""

from sqlalchemy import select

from app import ingest, retrieval, storage
from app.core.config import settings
from app.models import Chunk, Document

DOC_TCP = "# TCP 三次握手\n\n客户端发送 SYN，服务端回复 SYN+ACK。\n"
DOC_OS = "# 操作系统调度\n\n时间片轮转与优先级调度。\n"


def _query_vector() -> list[float]:
    """非零确定性查询向量（零向量会让 cosine 距离退化，见 conftest 注释）。"""
    return [0.01] * settings.embedding_dimension


def _add_doc(db, kb_id: int, title: str, text: str) -> Document:
    doc = Document(
        kb_id=kb_id,
        title=title,
        file_path=storage.save(kb_id, 9999, text.encode("utf-8")),
        content_hash=f"hash-{title}",
        char_count=len(text),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    ingest.process_document(doc.id, db)
    return doc


# ── RRF 纯函数（不依赖数据库） ────────────────────────────────

def test_rrf_single_channel_keeps_order():
    """只有一个通道有结果时，顺序与排名一致。"""
    fused = retrieval.fuse_rrf([10, 20, 30], [])
    assert [cid for cid, *_ in fused] == [10, 20, 30]


def test_rrf_boosts_chunks_hit_by_both_channels():
    """两通道都命中的块获得分数加成，排到最前。"""
    fused = retrieval.fuse_rrf([1, 2, 3], [3, 4])
    scores = {cid: score for cid, score, *_ in fused}
    assert fused[0][0] == 3                       # 块 3 被两通道命中
    assert scores[3] > scores[1]


def test_rrf_is_deterministic():
    """相同输入恒得相同输出（RRF 无随机性）。"""
    a = retrieval.fuse_rrf([1, 2], [3])
    b = retrieval.fuse_rrf([1, 2], [3])
    assert [(cid, round(s, 6)) for cid, s, *_ in a] == [(cid, round(s, 6)) for cid, s, *_ in b]


def test_rrf_records_channel_ranks():
    """融合结果保留两通道各自的排名（供调试与阈值分析）。"""
    fused = retrieval.fuse_rrf([7], [7, 8])
    cid, _score, v_rank, k_rank = fused[0]
    assert (cid, v_rank, k_rank) == (7, 1, 1)


# ── 双通道与整合（真库） ────────────────────────────────────

def test_vector_search_filters_by_kb(db, client):
    """向量通道按 kb_id 过滤：不返回其它库的块。"""
    kb1 = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    kb2 = client.post("/api/v1/kbs", json={"name": "操作系统"}).json()["id"]
    _add_doc(db, kb1, "tcp.md", DOC_TCP)
    _add_doc(db, kb2, "os.md", DOC_OS)

    hits_kb1 = retrieval.search_vector(db, _query_vector(), kb1)
    assert hits_kb1
    rows = db.scalars(select(Chunk).where(Chunk.id.in_(hits_kb1))).all()
    assert all(c.kb_id == kb1 for c in rows)


def test_keyword_search_finds_literal_term(db, client):
    """关键词通道按字面命中（pg_trgm）：搜「调度」命中操作系统文档的块。"""
    kb = client.post("/api/v1/kbs", json={"name": "操作系统"}).json()["id"]
    _add_doc(db, kb, "os.md", DOC_OS)

    hits = retrieval.search_keyword(db, "时间片轮转与优先级调度", kb)
    assert hits
    hit_texts = db.scalars(select(Chunk.content).where(Chunk.id.in_(hits))).all()
    assert any("调度" in text for text in hit_texts)


def test_retrieve_returns_ranked_candidates(db, client):
    """整合检索：返回候选块并带 RRF 分数与通道排名。"""
    kb = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb, "tcp.md", DOC_TCP)

    results = retrieval.retrieve(db, "TCP 三次握手", _query_vector(), kb, top_k=5)
    assert results
    assert results[0].chunk_id > 0
    assert results[0].rrf_score > 0
    assert "三次握手" in results[0].content
    assert results[0].vector_rank is not None


def test_retrieve_empty_kb_returns_empty(db, client):
    """空知识库：返回空列表（拒答判定的输入之一）。"""
    kb = client.post("/api/v1/kbs", json={"name": "空库"}).json()["id"]
    assert retrieval.retrieve(db, "任何问题", _query_vector(), kb) == []


def test_retrieve_all_kbs_without_filter(db, client):
    """kb_id=None 表示全库检索：跨库命中。"""
    kb1 = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    kb2 = client.post("/api/v1/kbs", json={"name": "操作系统"}).json()["id"]
    _add_doc(db, kb1, "tcp.md", DOC_TCP)
    _add_doc(db, kb2, "os.md", DOC_OS)

    results = retrieval.retrieve(db, "调度", _query_vector(), kb_id=None, top_k=10)
    assert len(results) >= 2                      # 两个库的块都在候选里
