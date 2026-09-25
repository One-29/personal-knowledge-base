"""KnowBase API 入口：组装应用（路由随各模块 PR 挂入）。

运行：uvicorn app.main:app --reload
界面：http://127.0.0.1:8000/          （前端单页，M5）
文档：http://127.0.0.1:8000/docs      （OpenAPI，联调与答辩备用）
"""

from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.requests import Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings  # noqa: F401  （供后续装配读取配置）
from app.database import initialize_database
from app.db import engine
from app.diagnostics.context import current_request_id
from app.diagnostics.local_logging import configure_runtime_logging
from app.diagnostics.middleware import RequestDiagnosticsMiddleware
from app.http_client import close_http_client
from app.http_security import local_browser_write_guard
from app.ingest_tasks import recover_incomplete_tasks
from app.routers import (
    ask,
    diagnostics,
    document_content,
    documents,
    graph,
    kbs,
    workflow,
)

API_PREFIX = "/api/v1"
FRONTEND_SOURCE_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND_DIR = FRONTEND_SOURCE_DIR / "dist"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """准备嵌入式 schema；退出时统一释放 HTTP 连接池。"""
    configure_runtime_logging(settings)
    logger.info("KnowBase API 启动 backend=%s", engine.dialect.name)
    try:
        initialize_database(engine)
        with Session(engine, expire_on_commit=False) as db:
            recover_incomplete_tasks(db)
        yield
    finally:
        close_http_client()
        logger.info("KnowBase API 已停止")


app = FastAPI(
    title="KnowBase API",
    description="个人知识库问答系统：文档管理 / RAG 问答（溯源 + 拒答）/ Agent 工作流",
    version="0.1.0",
    lifespan=lifespan,
)
app.middleware("http")(local_browser_write_guard)
app.add_middleware(RequestDiagnosticsMiddleware)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
    """未处理异常只返回关联 ID，不把路径、SQL 或供应商响应暴露给界面。"""
    request_id = getattr(request.state, "request_id", current_request_id())
    logger.exception("未处理的请求异常", extra={"request_id": request_id})
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务内部错误，请复制诊断信息后重试",
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )

# 版本前缀在装配层统一管理（单点修改）；各 router 只声明业务前缀
app.include_router(kbs.router, prefix=API_PREFIX)
app.include_router(documents.kb_documents_router, prefix=API_PREFIX)
app.include_router(documents.documents_router, prefix=API_PREFIX)
app.include_router(document_content.router, prefix=API_PREFIX)
app.include_router(ask.router, prefix=API_PREFIX)
app.include_router(workflow.router, prefix=API_PREFIX)
app.include_router(graph.router, prefix=API_PREFIX)
app.include_router(diagnostics.router, prefix=API_PREFIX)


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


# FastAPI 只托管 Vite 的生产构建产物；TypeScript 源码不会暴露给运行时。
# 目录不存在时跳过挂载（例如只跑 API 的场景），不影响后端功能。
if FRONTEND_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
