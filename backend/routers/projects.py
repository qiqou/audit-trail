"""项目打开、新建、删除和最近项目接口。"""

import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from api.dependencies import SessionContext
from api_models import CreateReq, NameReq, OpenReq, ResetReq
from config import PROJECT_EXT
from database import AuditProject
from fastapi import APIRouter, Depends, HTTPException
from platform_adapter import forget_recent, harden_project, load_recent_all, remember_recent


def build_router(
    get_project: Callable[..., AuditProject],
    get_operator: Callable[..., str],
    project_factory: Callable[[Path], AuditProject],
    get_context: Callable[[], SessionContext | None],
    project_key: Callable[[Path], str],
    require_switch_idle: Callable[[SessionContext, str], None],
    reserve_project: Callable[[SessionContext, str], None],
    release_project: Callable[[SessionContext, str], None],
    close_current_project: Callable[..., None],
    bind_project_identity: Callable[[AuditProject, SessionContext], None],
    project_info: Callable[[], dict],
    require_project_idle: Callable[[AuditProject], None],
    active_project_owner: Callable[[str], SessionContext | None],
    lease_seconds: float,
) -> APIRouter:
    """保持项目会话抢占、目录伪装和危险删除防护的既有语义。"""
    router = APIRouter()

    @router.post("/api/project/open")
    def open_project(req: OpenReq, operator: str = Depends(get_operator)):
        raw_path = req.path.strip()
        if not raw_path:
            raise HTTPException(status_code=400, detail="项目路径不能为空")
        path = Path(raw_path).expanduser()
        if not path.is_dir() and path.name and not path.name.endswith(PROJECT_EXT):
            candidate = path.with_name(path.name + PROJECT_EXT)
            if candidate.is_dir():
                path = candidate
        if not path.is_dir():
            raise HTTPException(status_code=404, detail=f"项目文件夹不存在：{path}")
        context = get_context()
        assert context is not None
        key = project_key(path)
        require_switch_idle(context, key)
        reserve_project(context, key)
        try:
            opened_project = project_factory(path)
        except ValueError as exc:
            if context.project_key != key:
                release_project(context, key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            if context.project_key != key:
                release_project(context, key)
            raise HTTPException(
                status_code=400,
                detail=f"无法打开项目「{path}」：{exc}。请确认该文件夹存在、当前账户具有读取和写入权限，且未被其他程序锁定后重试",
            ) from exc
        close_current_project(preserve_key=key)
        context.project = opened_project
        context.project_key = key
        context.preempted = False
        bind_project_identity(opened_project, context)
        opened_project.log(operator, "打开项目", str(path))
        remember_recent(operator, str(path), opened_project.project_name)
        return project_info()

    @router.post("/api/project/create")
    def create_project(req: CreateReq, operator: str = Depends(get_operator)):
        raw_path = req.path.strip()
        if not raw_path:
            raise HTTPException(status_code=400, detail="项目路径不能为空")
        path = Path(raw_path).expanduser()
        if path.name and not path.name.endswith(PROJECT_EXT):
            target = path.with_name(path.name + PROJECT_EXT)
            if path.is_dir():
                if not target.exists():
                    try:
                        path.rename(target)
                    except OSError:
                        target = path
                else:
                    try:
                        path.rmdir()
                    except OSError:
                        pass
            path = target
        context = get_context()
        assert context is not None
        key = project_key(path)
        require_switch_idle(context, key)
        reserve_project(context, key)
        try:
            opened_project = project_factory(path)
        except ValueError as exc:
            if context.project_key != key:
                release_project(context, key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            if context.project_key != key:
                release_project(context, key)
            raise HTTPException(
                status_code=400,
                detail=f"无法创建项目「{path}」：{exc}。请确认该目录可写，且未被其他程序锁定后重试",
            ) from exc
        close_current_project(preserve_key=key)
        context.project = opened_project
        context.project_key = key
        context.preempted = False
        bind_project_identity(opened_project, context)
        name = req.name.strip()
        if name:
            context.project.project_name = name
        harden_project(path)
        context.project.log(operator, "创建项目", name or path.name)
        remember_recent(operator, str(path), context.project.project_name)
        return project_info()

    @router.post("/api/project/delete")
    def delete_project(req: OpenReq, operator: str = Depends(get_operator)):
        """删除项目目录（仅限 .auditproj 伪装项目，防误删其他文件夹）。

        危险操作：数据库/附件/输出一并删除，不可恢复，前端必须二次确认。
        """
        raw_path = req.path.strip()
        if not raw_path:
            raise HTTPException(status_code=400, detail="项目路径不能为空")
        path = Path(raw_path).expanduser()
        if not path.name.endswith(PROJECT_EXT):
            raise HTTPException(status_code=400, detail="只能删除审迹创建的项目（目录名以 .auditproj 结尾）")
        if not path.is_dir():
            raise HTTPException(status_code=404, detail=f"项目不存在：{path}")
        context = get_context()
        key = project_key(path)
        owner = active_project_owner(key)
        if (context is not None and owner is not None and owner.token != context.token
                and time.monotonic() - owner.last_seen < lease_seconds):
            raise HTTPException(status_code=409, detail="该项目正在另一个工作台标签页中使用，不能删除")
        if context is not None and context.project is not None and project_key(Path(context.project.root)) == key:
            require_project_idle(context.project)
            close_current_project()
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"删除失败：{exc}。请关闭占用该项目的程序后重试") from exc
        forget_recent(operator, str(path))
        return {"deleted": str(path)}

    @router.post("/api/project/reset")
    def reset_project(req: ResetReq, operator: str = Depends(get_operator)):
        """重置项目：清空全部业务数据并完全初始化。

        危险操作，前端必须让用户输入项目名称二次确认；后端再次校验，
        与当前项目名不一致直接拒绝（防误触/跨会话误操作）。
        """
        project = get_project()
        if req.confirm_text.strip() != project.project_name:
            raise HTTPException(status_code=400, detail="确认文字与项目名称不一致，已取消重置")
        project.reset_all(operator)
        return {"ok": True}

    @router.get("/api/project/current")
    def current_project(_: str = Depends(get_operator)):
        return project_info()

    @router.post("/api/project/rename")
    def rename_project(req: NameReq, operator: str = Depends(get_operator)):
        project = get_project()
        old = project.project_name
        try:
            project.project_name = req.name
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        project.log(operator, "重命名项目", f"{old} → {project.project_name}")
        remember_recent(operator, str(project.root), project.project_name)
        return project_info()

    @router.get("/api/recent")
    def recent_projects(operator: str = Depends(get_operator)):
        """最近项目列表（按使用人隔离，存本机 ~/.shenji，与端口/浏览器无关）。"""
        return {"items": load_recent_all().get(operator, [])}

    @router.delete("/api/recent")
    def forget_recent_project(path: str, operator: str = Depends(get_operator)):
        """从最近列表移除一条记录（不移除磁盘项目）。"""
        forget_recent(operator, path)
        return {"ok": True}

    return router
