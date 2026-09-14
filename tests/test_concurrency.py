"""问答与工作流并发能力的 API 回归测试。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock
from time import perf_counter

from fastapi.testclient import TestClient

from app import db as db_module
from app.ask import AnswerData
from app.main import app
from app.routers import ask as ask_router
from app.routers import workflow as workflow_router
from app.workflow import WorkflowResultData, WorkflowStepData


@dataclass
class _RequestSession:
    """用身份可观察的轻量对象代替数据库会话。"""

    token: int
    closed: bool = False

    def close(self):
        self.closed = True


def test_ask_and_workflow_overlap_with_request_scoped_sessions(monkeypatch):
    """两个同步端点在线程池中重叠执行，并分别持有请求级 Session。"""
    lock = Lock()
    entered = {"ask": Event(), "workflow": Event()}
    release = Event()
    created: list[_RequestSession] = []
    intervals: dict[str, tuple[float, float]] = {}
    service_sessions: dict[str, _RequestSession] = {}

    def session_factory():
        with lock:
            session = _RequestSession(token=len(created) + 1)
            created.append(session)
        return session

    def run_together(name, session, result):
        started = perf_counter()
        with lock:
            service_sessions[name] = session
        entered[name].set()
        if not release.wait(timeout=5):
            raise TimeoutError(f"{name} 没有等到并发测试释放信号")
        finished = perf_counter()
        with lock:
            intervals[name] = (started, finished)
        return result

    def answer_question(db, question, kb_id, **kwargs):
        return run_together(
            "ask",
            db,
            AnswerData(question=question, content="问答成功", search_query=question),
        )

    def run_workflow(db, task, kb_id, max_steps=None, **kwargs):
        return run_together(
            "workflow",
            db,
            WorkflowResultData(
                task=task,
                steps=[
                    WorkflowStepData(
                        index=1,
                        goal="验证并发",
                        query=task,
                        status="answered",
                        conclusion="工作流成功",
                    )
                ],
                answer="工作流成功",
            ),
        )

    monkeypatch.setattr(ask_router.ask_service, "answer_question", answer_question)
    monkeypatch.setattr(workflow_router.workflow_service, "run_workflow", run_workflow)
    # 保留生产 get_db 依赖，只替换它运行时读取的 SessionLocal 工厂：既不连接
    # PostgreSQL，又能覆盖“每个请求创建并关闭独立 Session”的生产生命周期。
    monkeypatch.setattr(db_module, "SessionLocal", session_factory)

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
        ask_future = executor.submit(
            client.post, "/api/v1/ask", json={"question": "并发问答"}
        )
        workflow_future = executor.submit(
            client.post, "/api/v1/workflow", json={"task": "并发工作流"}
        )
        try:
            assert entered["ask"].wait(timeout=3)
            assert entered["workflow"].wait(timeout=3)
            # 两个服务都已进入且被同一信号挂起，证明请求确实同时在途。
            assert not ask_future.done()
            assert not workflow_future.done()
        finally:
            # 即使断言失败也解除等待，避免测试线程滞留到各自的超时上限。
            release.set()
        ask_response = ask_future.result(timeout=5)
        workflow_response = workflow_future.result(timeout=5)

    assert ask_response.status_code == 200
    assert ask_response.json()["content"] == "问答成功"
    assert workflow_response.status_code == 200
    assert workflow_response.json()["answer"] == "工作流成功"
    assert workflow_response.json()["steps"][0]["status"] == "answered"

    assert len(created) == 2
    assert service_sessions["ask"] is not service_sessions["workflow"]
    assert {session.token for session in service_sessions.values()} == {1, 2}
    assert all(session.closed for session in created)

    ask_started, ask_finished = intervals["ask"]
    workflow_started, workflow_finished = intervals["workflow"]
    assert max(ask_started, workflow_started) < min(ask_finished, workflow_finished)
