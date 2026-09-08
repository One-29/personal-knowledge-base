"""KnowBase API 入口：组装应用（路由随各模块 PR 挂入）。

运行：uvicorn app.main:app --reload
文档：http://127.0.0.1:8000/docs
"""

from fastapi import FastAPI

from app.core.config import settings  # noqa: F401  （供后续装配读取配置）
from app.routers import documents, kbs

API_PREFIX = "/api/v1"

app = FastAPI(
    title="KnowBase API",
    description="个人知识库问答系统：文档管理 / RAG 问答（溯源 + 拒答）/ Agent 工作流",
    version="0.1.0",
)

# 版本前缀在装配层统一管理（单点修改）；各 router 只声明业务前缀
app.include_router(kbs.router, prefix=API_PREFIX)
app.include_router(documents.kb_documents_router, prefix=API_PREFIX)
app.include_router(documents.documents_router, prefix=API_PREFIX)


@app.get("/health")
def health() -> dict:
    """存活探针：容器化/CI 用。"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
