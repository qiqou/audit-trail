"""本地使用人会话接口。"""

from collections.abc import Callable
from typing import Any

from api_models import OperatorReq
from fastapi import APIRouter, Depends, Header


def build_router(
    get_operator: Callable[..., str],
    login_action: Callable[[OperatorReq], dict],
    current_session_action: Callable[[str], dict[str, Any]],
    logout_action: Callable[[str], dict],
) -> APIRouter:
    """会话状态由共享依赖管理；路由仅保持 v1.2 HTTP 形状。"""
    router = APIRouter()

    @router.post("/api/session")
    def login(req: OperatorReq):
        """建立本地会话：现场人员姓名为主留痕，OS 账户作为第二道核验。"""
        return login_action(req)

    @router.get("/api/session")
    def current_session(operator: str = Depends(get_operator)):
        """校验浏览器保存的本地会话；服务重启后前端据此重新要求输入使用人。"""
        return current_session_action(operator)

    @router.delete("/api/session")
    def logout(x_session: str = Header(default="")):
        """显式释放当前会话及其数据库连接，避免频繁切换使用人造成资源累积。"""
        return logout_action(x_session)

    return router
