"""KnowBase API 入口：组装应用（路由随各模块 PR 挂入）。

运行：uvicorn app.main:app --reload
界面：http://127.0.0.1:8000/          （前端单页，M5）
文档：http://127.0.0.1:8000/docs      （OpenAPI，联调与答辩备用）
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings  # noqa: F401  （供后续装配读取配置）
from app.routers import ask, documents, graph, kbs, workflow

API_PREFIX = "/api/v1"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="KnowBase API",
    description="个人知识库问答系统：文档管理 / RAG 问答（溯源 + 拒答）/ Agent 工作流",
    version="0.1.0",
)

# 版本前缀在装配层统一管理（单点修改）；各 router 只声明业务前缀
app.include_router(kbs.router, prefix=API_PREFIX)
app.include_router(documents.kb_documents_router, prefix=API_PREFIX)
app.include_router(documents.documents_router, prefix=API_PREFIX)
app.include_router(ask.router, prefix=API_PREFIX)
app.include_router(workflow.router, prefix=API_PREFIX)
app.include_router(graph.router, prefix=API_PREFIX)


@app.get("/health")
def health() -> dict:
    """存活探针：容器化/CI 用。"""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """根路径跳到前端单页（M5）。"""
    return RedirectResponse(url="/ui/")


# 前端为纯静态文件（无构建步骤，02 §3 M5 边界：只渲染后端契约，不做业务判定）。
# 目录不存在时跳过挂载（例如只跑 API 的场景），不影响后端功能。
if FRONTEND_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
