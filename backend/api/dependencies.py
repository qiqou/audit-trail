"""会话请求上下文的共享定义。

会话生命周期和项目租约仍由启动入口编排；本模块只承载路由可复用的上下文
模型与 ContextVar，避免业务路由反向依赖 ``main`` 的全局变量。
"""

import threading
import time
from collections.abc import Callable
from contextvars import ContextVar

from database import AuditProject
from platform_adapter import OSIdentity


class SessionContext:
    """一个本地浏览器会话所持有的使用人与项目状态。"""

    def __init__(self, token: str, operator: str, identity: OSIdentity):
        self.token = token
        self.identity = identity
        self.operator = operator
        self.project: AuditProject | None = None
        self.project_key = ""
        self.last_seen = time.monotonic()
        self.archive_preflights: dict[str, dict] = {}
        self.merge_preflights: dict[str, dict] = {}
        self.batch_issue_preflights: dict[str, dict] = {}
        self.preempted = False


class SessionRegistry:
    """会话容器与单项目写入租约；关闭项目的具体方式由入口注入。"""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionContext] = {}
        self.project_leases: dict[str, str] = {}
        self.lock = threading.RLock()

    def expire(self, now: float, ttl_seconds: float, max_sessions: int,
               has_active_job: Callable[[AuditProject], bool],
               close_project: Callable[[SessionContext], None]) -> int:
        stale = [context for context in self.sessions.values() if now - context.last_seen >= ttl_seconds]
        survivors = [context for context in self.sessions.values() if context not in stale]
        overflow = max(0, len(survivors) - max_sessions)
        if overflow:
            stale.extend(sorted(survivors, key=lambda context: context.last_seen)[:overflow])
        for context in stale:
            if context.project is not None and has_active_job(context.project):
                context.last_seen = now
                continue
            if self.sessions.pop(context.token, None) is not None:
                close_project(context)
        return len(stale)

    def reserve(self, context: SessionContext, project_key: str,
                close_project: Callable[[SessionContext], None],
                on_close_failure: Callable[[Exception], None]) -> None:
        """接管已有租约；先关闭旧连接，后通知旧会话。"""
        with self.lock:
            owner = self.project_leases.get(project_key)
            if owner and owner != context.token:
                owner_context = self.sessions.get(owner)
                if owner_context is not None:
                    try:
                        close_project(owner_context)
                    except Exception as exc:
                        on_close_failure(exc)
                    owner_context.preempted = True
            self.project_leases[project_key] = context.token

    def release(self, context: SessionContext, project_key: str) -> None:
        if not project_key:
            return
        with self.lock:
            if self.project_leases.get(project_key) == context.token:
                self.project_leases.pop(project_key, None)

    def active_owner(self, project_key: str) -> SessionContext | None:
        with self.lock:
            token = self.project_leases.get(project_key)
            return self.sessions.get(token) if token else None


session_context: ContextVar[SessionContext | None] = ContextVar("audit_ctx", default=None)


def get_current_context() -> SessionContext | None:
    return session_context.get()
