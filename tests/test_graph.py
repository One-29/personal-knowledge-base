"""关联图测试（M5+）：节点/边结构、过滤参数、边界情况。

向量由 conftest 的假 provider 生成（按文本 hash 的确定性伪向量）——
所以边的权重反映的是"结构正确性"，不是真实语义相似度（后者由 06 评估覆盖）。
"""

from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import graph, ingest, storage
from app.core.config import settings
from app.models import Chunk, Document, KnowledgeBase

TCP = "# TCP 三次握手\n\n客户端发 SYN，服务端回 SYN+ACK，客户端再回 ACK。\n\n## 拥塞控制\n\n慢启动与拥塞避免。\n"
OS = "# 操作系统调度\n\n时间片轮转与优先级调度。\n"
PY = "# Python 生成器\n\nyield 惰性求值，避免一次性占用内存。\n"


def _add_doc(db, kb_id: int, title: str, text: str, slot: int) -> Document:
    doc = Document(
        kb_id=kb_id,
        title=title,
        file_path=storage.save(kb_id, slot, text.encode("utf-8")),
        content_hash=f"graph-{title}",
        char_count=len(text),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    ingest.process_document(doc.id, db)
    return doc


def test_graph_requires_existing_kb(client):
    """知识库不存在 → 404。"""
    assert client.get("/api/v1/graph?kb_id=999999").status_code == 404


def test_graph_single_document_has_no_edges(client, db):
    """只有一篇文档时：有节点、无边（关联需要至少两篇）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", TCP, 1)

    body = client.get(f"/api/v1/graph?kb_id={kb_id}").json()
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["chunks"] >= 1
    assert body["edges"] == []


def test_graph_empty_kb(client):
    """空库：无节点无边，不报错。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "空库"}).json()["id"]
    body = client.get(f"/api/v1/graph?kb_id={kb_id}").json()
    assert body["nodes"] == []
    assert body["edges"] == []


def test_graph_multi_documents_structure(client, db, monkeypatch):
    """多篇文档：返回节点（含块数）与边（含权重/对数/相似度），边按权重降序。"""
    # 假向量彼此近似正交，相似度较低——为了验证"连边逻辑"，把阈值放低
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", TCP, 1)
    _add_doc(db, kb_id, "os.md", OS, 2)
    _add_doc(db, kb_id, "python.md", PY, 3)

    body = client.get(f"/api/v1/graph?kb_id={kb_id}&min_similarity=0.0").json()

    assert len(body["nodes"]) == 3
    assert {n["title"] for n in body["nodes"]} == {"tcp.md", "os.md", "python.md"}
    assert all(n["chunks"] >= 1 for n in body["nodes"])

    assert body["edges"], "阈值放到 0 时应当连边"
    first = body["edges"][0]
    assert {"source", "target", "weight", "links", "similarity"} <= set(first)
    assert first["source"] != first["target"]              # 不自连
    assert 0.0 <= first["weight"] <= 1.0
    assert first["links"] >= 1
    weights = [e["weight"] for e in body["edges"]]
    assert weights == sorted(weights, reverse=True)        # 权重降序
    assert body["truncated"] is False


def test_graph_threshold_filters_edges(client, db):
    """阈值提高后边变少或消失（不能凭空增加）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", TCP, 1)
    _add_doc(db, kb_id, "os.md", OS, 2)

    loose = client.get(f"/api/v1/graph?kb_id={kb_id}&min_similarity=0.0").json()["edges"]
    tight = client.get(f"/api/v1/graph?kb_id={kb_id}&min_similarity=0.99").json()["edges"]
    assert len(tight) <= len(loose)
    assert tight == []


def test_graph_invalid_params_422(client):
    """参数越界 → 422（top_k > 10 或相似度 > 1）。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    assert client.get(f"/api/v1/graph?kb_id={kb_id}&top_k=99").status_code == 422
    assert client.get(f"/api/v1/graph?kb_id={kb_id}&min_similarity=2").status_code == 422


@pytest.mark.parametrize(
    ("chunk_counts", "max_chunks", "truncated"),
    [
        ([], 400, False),
        ([401], 400, True),
        ([200, 201], 400, True),
        ([200, 200], 400, False),
        ([200, 201], 900, True),
    ],
)
def test_graph_without_edges_reports_truncation_from_nodes(chunk_counts, max_chunks, truncated):
    """空边和单文档早返回也必须诚实标注源块是否超出实际预算。"""
    node_rows = [
        SimpleNamespace(doc_id=i, title=f"文档{i}", chunks=count, char_count=count)
        for i, count in enumerate(chunk_counts)
    ]
    db = Mock()
    db.execute.side_effect = [Mock(all=lambda: node_rows), Mock(all=lambda: [])]

    result = graph.build_graph(db, kb_id=1, max_chunks=max_chunks)

    assert result.edges == []
    assert result.truncated is truncated
    assert sum(node.chunks for node in result.nodes) == sum(chunk_counts)


@pytest.mark.parametrize(
    ("chunk_counts", "max_chunks", "expected_counts"),
    [((410, 2, 1), 400, (397, 2, 1)), ((8, 3, 2), 5, (2, 2, 1))],
)
def test_graph_source_pool_samples_documents_in_rounds(db, chunk_counts, max_chunks, expected_counts):
    """实际执行近邻 SQL：早期长文不能挤掉后导入短文的源块配额。"""
    kb = KnowledgeBase(name="图采样测试")
    db.add(kb)
    db.flush()
    vector = [1.0] + [0.0] * (settings.embedding_dimension - 1)
    docs = []
    for slot, count in enumerate(chunk_counts):
        doc = Document(
            kb_id=kb.id,
            title=f"采样{slot}.md",
            file_path=f"unused-{slot}.md",
            content_hash=f"sampling-{slot}",
            char_count=count,
        )
        db.add(doc)
        db.flush()
        docs.append(doc)
        db.add_all([
            Chunk(
                kb_id=kb.id,
                doc_id=doc.id,
                chunk_index=index,
                content="块",
                char_start=index,
                char_end=index + 1,
                embedding=vector,
            )
            for index in range(count)
        ])
        db.flush()

    rows = db.execute(
        graph._PAIRS_SQL,
        {"kb_id": kb.id, "max_chunks": max_chunks, "top_k": 1, "min_similarity": 0.5},
    ).all()

    # 每个源块恰好连一个近邻，从源文档计数直接验证池的组成，而非仅验证有边。
    assert len(rows) == max_chunks
    assert Counter(row.source_doc for row in rows) == {
        doc.id: expected for doc, expected in zip(docs, expected_counts)
    }
