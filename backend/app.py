"""FastAPI 应用组装：保留本地回环服务的安全与静态托管约束。"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from runtime_log import install_unhandled_error_handler
from starlette.middleware.trustedhost import TrustedHostMiddleware


def create_app() -> FastAPI:
    """创建尚未挂载 SPA 的 API 应用。

    路由仍由启动入口注册；SPA 必须最后挂载，避免根路径吞掉 API 路由。
    """
    app = FastAPI(title="审迹", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.exception_handler(InterruptedError)
    async def handle_interrupted(_request: Request, exc: InterruptedError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc) or "任务已取消"})

    # 仅监听回环地址不足以阻断 DNS rebinding：浏览器仍可能将 evil.example 的 Host
    # 请求送到本地端口。testserver 仅供 FastAPI TestClient。
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    install_unhandled_error_handler(app)
    return app


def mount_frontend(app: FastAPI, directory: Path) -> None:
    """在所有 API 路由注册完成后挂载正式前端资源。"""
    directory.mkdir(exist_ok=True)
    app.mount("/", StaticFiles(directory=directory, html=True), name="frontend")
