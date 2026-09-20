"""KnowBase API 入口：组装应用（路由随各模块 PR 挂入）。

运行：uvicorn app.main:app --reload
界面：http://127.0.0.1:8000/          （前端单页，M5）
文档：http://127.0.0.1:8000/docs      （OpenAPI，联调与答辩备用）
"""

from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings  # noqa: F401  （供后续装配读取配置）
from app.db import engine
from app.http_client import close_http_client
from app.routers import ask, document_content, documents, graph, kbs, workflow

API_PREFIX = "/api/v1"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """HTTP 池按需创建，服务完成在途请求和后台任务后统一释放。"""
    try:
        yield
    finally:
        close_http_client()


app = FastAPI(
    title="KnowBase API",
    description="个人知识库问答系统：文档管理 / RAG 问答（溯源 + 拒答）/ Agent 工作流",
    version="0.1.0",
    lifespan=lifespan,
)

# 版本前缀在装配层统一管理（单点修改）；各 router 只声明业务前缀
app.include_router(kbs.router, prefix=API_PREFIX)
app.include_router(documents.kb_documents_router, prefix=API_PREFIX)
app.include_router(documents.documents_router, prefix=API_PREFIX)
app.include_router(document_content.router, prefix=API_PREFIX)
app.include_router(ask.router, prefix=API_PREFIX)
app.include_router(workflow.router, prefix=API_PREFIX)
app.include_router(graph.router, prefix=API_PREFIX)


@app.get("/health")
def health() -> dict:
    """存活探针：只表示 API 进程能响应，不访问外部依赖。"""
    return {"status": "ok"}


def database_is_ready() -> bool:
    """用独立短连接检查数据库；失败不向客户端暴露连接信息。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        logger.warning("数据库就绪检查失败", exc_info=True)
        return False


@app.get("/ready")
def ready():
    """就绪探针：API 与数据库都可用时才返回 200。"""
    if not database_is_ready():
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unavailable"},
        )
    return {"status": "ok", "database": "ok"}


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
