"""多步工作流测试（M4）：规划解析、逐步执行、缺料记录、汇总编号重映射。

M4 不自己检索——测试通过假 LLM 控制「规划输出」与「每步回答」，
以此验证编排逻辑（真检索由 M3 的测试覆盖）。
"""

import pytest

from app import ingest, storage, workflow
from app.core.config import settings
from app.generation import LLMError
from app.models import Document

DOC_TCP = "# TCP 三次握手\n\n客户端发送 SYN，服务端回复 SYN+ACK。\n"
DOC_OS = "# 操作系统调度\n\n时间片轮转与优先级调度。\n"


class _ScriptedLLM:
    """按调用顺序返回预设内容：先规划，再逐轮生成。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        return self.replies.pop(0) if self.replies else ""


@pytest.fixture()
def no_l1_threshold(monkeypatch) -> None:
    monkeypatch.setattr(settings, "refusal_similarity_threshold", -1.0)


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


# ── 规划解析（AW1） ─────────────────────────────────────────

def test_parse_plan_accepts_json_array():
    raw = '[{"goal": "查 TCP", "query": "TCP 三次握手"}, {"goal": "查调度", "query": "操作系统调度"}]'
    plans = workflow.parse_plan(raw, max_steps=5)
    assert [p.query for p in plans] == ["TCP 三次握手", "操作系统调度"]


def test_parse_plan_strips_code_fence():
    """模型常把 JSON 包进 ```json 代码块——要能剥离。"""
    raw = '```json\n[{"goal": "g", "query": "q"}]\n```'
    assert [p.query for p in workflow.parse_plan(raw, 5)] == ["q"]


def test_parse_plan_respects_max_steps():
    raw = '[{"goal": "1", "query": "1"}, {"goal": "2", "query": "2"}, {"goal": "3", "query": "3"}]'
    assert len(workflow.parse_plan(raw, max_steps=2)) == 2


def test_parse_plan_returns_empty_on_invalid_json():
    """解析失败返回空列表（由调用方退化，不抛异常）。"""
    assert workflow.parse_plan("这不是 JSON", 5) == []
    assert workflow.parse_plan('{"not": "a list"}', 5) == []


def test_plan_steps_falls_back_to_single_step():
    """规划失败 → 退化为「单步 = 原任务」（AW1，不阻断用户）。"""

    class _BrokenLLM(_ScriptedLLM):
        def complete(self, system: str, user: str) -> str:
            raise LLMError("规划失败")

    plans = workflow.plan_steps("总结 TCP 知识点", 5, provider=_BrokenLLM([]))
    assert len(plans) == 1
    assert plans[0].query == "总结 TCP 知识点"


# ── 汇总与编号重映射（AW4） ─────────────────────────────────

def test_remap_citations_rewrites_indexes():
    """步骤内编号 → 全局编号（纯规则，防前后端编号错位）。"""
    assert workflow.remap_citations("甲[1]乙[2]", {1: 3, 2: 4}) == "甲[3]乙[4]"


def test_remap_citations_keeps_unknown_index():
    """映射中不存在的编号原样保留（不静默篡改）。"""
    assert workflow.remap_citations("甲[9]", {1: 1}) == "甲[9]"


def test_synthesize_numbers_citations_globally():
    """跨步汇总：引用全局连续编号，正文同步重映射。"""
    from app.ask import CitationData

    def _cite(index: int, chunk_id: int) -> CitationData:
        return CitationData(index=index, chunk_id=chunk_id, doc_id=1, doc_title="t.md",
                            chunk_text="x", char_start=0, char_end=1)

    steps = [
        workflow.WorkflowStepData(index=1, goal="第一步", query="q1", status="answered",
                                  conclusion="结论甲[1]", citations=[_cite(1, 11)]),
        workflow.WorkflowStepData(index=2, goal="第二步", query="q2", status="answered",
                                  conclusion="结论乙[1]和[2]", citations=[_cite(1, 22), _cite(2, 23)]),
    ]
    answer, citations = workflow.synthesize("任务", steps)

    assert [c.index for c in citations] == [1, 2, 3]        # 全局连续
    assert "结论甲[1]" in answer
    assert "结论乙[2]和[3]" in answer                        # 第二步的 [1][2] 重映射为 [2][3]
    assert [c.chunk_id for c in citations] == [11, 22, 23]


# ── 端到端编排（真库 + 脚本化 LLM） ─────────────────────────

def test_run_workflow_multi_steps(db, client, no_l1_threshold):
    """两步任务：分别命中两个库的文档，汇总带全局编号引用。"""
    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)

    llm = _ScriptedLLM([
        '[{"goal": "查握手", "query": "三次握手是什么"}, {"goal": "查其它", "query": "无关内容"}]',
        "三次握手用于确认收发能力 [1]。",
        "资料不足，无法回答。",              # 第二步模型自述无料
    ])

    result = workflow.run_workflow(db, "总结 TCP 握手", kb_id, llm=llm)

    assert [s.status for s in result.steps] == ["answered", "insufficient"]
    assert result.steps[1].note is not None                  # 缺料说明可见（US-M4-02）
    assert "**1. 查握手**" in result.answer
    assert "知识库无相关内容" in result.answer
    assert len(result.citations) == 1


def test_run_workflow_continues_after_step_error(db, client, no_l1_threshold, monkeypatch):
    """单步故障不中断后续步骤（04 §3.2）。"""
    from app import ask as ask_module

    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)

    calls = {"n": 0}
    original = ask_module.answer_question

    def _flaky(db_, question, kb_id_, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟第一步故障")
        return original(db_, question, kb_id_, **kwargs)

    monkeypatch.setattr(workflow.ask_service, "answer_question", _flaky)
    llm = _ScriptedLLM([
        '[{"goal": "A", "query": "三次握手"}, {"goal": "B", "query": "三次握手"}]',
        "结论 [1]。",
    ])

    result = workflow.run_workflow(db, "任务", kb_id, llm=llm)
    assert result.steps[0].status == "error"
    assert result.steps[1].status == "answered"              # 后续步骤照常执行


def test_run_workflow_api_contract(client, db, no_l1_threshold, monkeypatch):
    """端点契约：200 + steps/answer/citations 结构。"""
    from app import generation

    kb_id = client.post("/api/v1/kbs", json={"name": "计算机网络"}).json()["id"]
    _add_doc(db, kb_id, "tcp.md", DOC_TCP)

    scripted = _ScriptedLLM([
        '[{"goal": "查握手", "query": "三次握手"}]',
        "三次握手确认收发能力 [1]。",
    ])
    # 单一替换点：workflow / ask 都经 generation 模块动态取 provider
    monkeypatch.setattr(generation, "get_llm_provider", lambda: scripted)

    resp = client.post("/api/v1/workflow", json={"task": "总结握手", "kb_id": kb_id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["task"] == "总结握手"
    assert len(body["steps"]) == 1
    assert body["steps"][0]["status"] == "answered"
    assert body["citations"][0]["index"] == 1
    assert body["citations"][0]["doc_title"] == "tcp.md"


def test_workflow_empty_task_422(client):
    assert client.post("/api/v1/workflow", json={"task": ""}).status_code == 422
