"""FastAPI 入口 — 本地服务 + 静态托管 + 启动即开浏览器。

设计：
- 所有接口强制使用人（X-Operator 请求头），缺失即拒绝 —— 与前端启动弹窗双保险
- 项目按会话隔离：每个会话 token 独立持有自己的项目（审查 F-03 修复），
  不同浏览器会话可各自打开不同项目，互不干扰
- 查看类接口不写日志，变更类接口由数据层自动留痕
- 前端静态资源在 frontend-v3/dist，根路径挂载。
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

# 兼容两种启动方式：`python backend/main.py` 与 `uvicorn main:app`（根目录转发）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api.dependencies import SessionContext, SessionRegistry, get_current_context, session_context
from api_models import (
    BackupSettingsReq,
    BatchRenameReq,
    CreateReq,
    ExportReq,
    FolderReq,
    LocalBackupRestoreReq,
    LocalMergeReq,
    MoveFileReq,
    NameReq,
    OpenReq,
    OperatorReq,
    PackageReq,
    RecoveryPointRestoreReq,
    ResetReq,
)
from app import create_app, mount_frontend
from app_launcher import launch_service, serve_app
from config import PROJECT_EXT, RuntimeSettings
from database import OUT_DIR, SYSTEM_METADATA_NAMES, AuditProject
from jobs import JobContext, job_runner
from platform_adapter import (
    PlatformError,
    current_os_identity,
    forget_recent,
    harden_project,
    load_recent_all,
    open_path,
    remember_recent,
    spawn_detached,
)
from platform_adapter import (
    choose_folder as platform_choose_folder,
)
from routers.evidence_links import build_router as build_evidence_links_router
from routers.evidence_operations import build_router as build_evidence_operations_router
from routers.exchanges import build_router as build_exchanges_router
from routers.issues import build_router as build_issues_router
from routers.operations import build_router as build_operations_router
from routers.project_runtime import build_router as build_project_runtime_router
from routers.projects import build_router as build_projects_router
from routers.recycle import build_router as build_recycle_router
from routers.sessions import build_router as build_sessions_router
from routers.settings import build_router as build_settings_router
from routers.units import build_router as build_units_router
from runtime_log import log_runtime_event

SETTINGS = RuntimeSettings.from_environment()
HOST = SETTINGS.host
# 改造版独立于原审迹 v1.1 的单实例锁和端点记录。仅改端口还不够：若共用
# shenji.lock，启动器会把原版实例误认为当前版本并直接打开它的页面。
# v1.3 必须与 v1.2 使用不同单实例标识；端口可由运行配置独立指定，
# 但同一 v1.3 进程只允许一个实例，避免同一项目双写。
INSTANCE_LOCK_NAME = "audit-trail-v13.lock"
# 打包环境（PyInstaller）与开发环境均只加载 V3 正式前端。
if getattr(sys, "_MEIPASS", None):
    V3_FRONTEND_DIR = Path(sys._MEIPASS) / "frontend-v3" / "dist"
else:
    PROJECT_DIR = Path(__file__).resolve().parent.parent
    V3_FRONTEND_DIR = PROJECT_DIR / "frontend-v3" / "dist"

FRONTEND_DIR = V3_FRONTEND_DIR
if not FRONTEND_DIR.is_dir():
    raise RuntimeError(
        "V3 前端构建产物不存在。请先在 frontend-v3 执行 pnpm build。"
    )


def _sha256_of(path) -> str:
    """计算文件 sha256（上传查重用）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _folder_fingerprint(folder_files: list) -> str:
    """文件夹内容指纹：每个成员「相对路径 + 文件内容哈希」排序后整体摘要。

    目录结构或任一文件内容不同 → 指纹不同；目录结构相同且文件内容一致 → 判重。
    """
    parts = []
    for item in folder_files:
        rel, tmp = item[:2]
        relative = rel.replace("\\", "/")
        # 与数据层的目录摘要保持同一口径。Finder/浏览器偶尔会把 .DS_Store
        # 一并交给粘贴或拖入流程；该元数据会被保存，但不属于审计证据摘要。
        # 此处若仍计入预先指纹，会与 add_folder 落盘后的摘要不一致并产生误报。
        if any(part in SYSTEM_METADATA_NAMES for part in PurePosixPath(relative).parts):
            continue
        # 上传流已得到成员摘要时直接复用；历史/导入调用保留落盘后计算的兼容路径。
        member_sha = item[2] if len(item) > 2 else _sha256_of(tmp)
        parts.append(f"{relative}\t{member_sha}")
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

app = create_app()

# 会话表：token → 会话上下文（使用人 + 该会话打开的项目）。
# 每个会话独立持有项目；同一项目同时只允许一个会话写入，避免第二标签页
# 或第二浏览器连接造成 SQLite 并发写入。
_session_registry = SessionRegistry()
# 兼容既有测试与尚未迁移的启动逻辑：容器所有权已转入 SessionRegistry。
_sessions = _session_registry.sessions
_project_leases = _session_registry.project_leases
_project_leases_lock = _session_registry.lock
PROJECT_LEASE_SECONDS = 45
SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 20


def _expire_sessions(now: float | None = None) -> int:
    """回收过期或超出容量的浏览器会话，并释放其项目连接与单写租约。"""
    current = time.monotonic() if now is None else now
    return _session_registry.expire(
        current, SESSION_TTL_SECONDS, MAX_SESSIONS, job_runner.has_active, _close_session_project,
    )

# 当前请求的会话上下文（HTTP 中间件设置，请求上下文链内传播）。保留别名，
# 使尚未迁移的处理器能行为保持地使用共享会话上下文。
_ctx_var = session_context


@app.middleware("http")
async def _audit_session_middleware(request, call_next):
    """把请求的会话上下文挂到当前请求（async 层，保证依赖/接口都能读到）。

    同步依赖与同步接口可能在不同线程池线程执行，直接在线程内 set contextvar
    不可靠；在中间件（请求主协程）设置后，随请求上下文传播（审查 F-03 修复）。
    """
    token = request.headers.get("x-session", "").strip()
    ctx = _sessions.get(token)
    context_token = _ctx_var.set(ctx)
    try:
        return await call_next(request)
    finally:
        # 显式复位，防止 ASGI 任务复用或异常路径把上一请求的项目上下文带入下一请求。
        _ctx_var.reset(context_token)


def login(req: OperatorReq):
    """建立本地会话：现场人员姓名为主留痕，OS 账户作为第二道核验。"""
    operator = req.operator.strip()
    if not operator:
        raise HTTPException(status_code=400, detail="使用人姓名不能为空")
    _expire_sessions()
    identity = current_os_identity()
    token = uuid.uuid4().hex
    _sessions[token] = SessionContext(token, operator, identity)
    return {"token": token, "operator": operator,
            "account_id": identity.account_id, "device_id": identity.device_id}


def get_operator(x_session: str = Header(default="")) -> str:
    """强制使用人：会话 token 缺失或无效直接拒绝；同时把会话上下文挂到当前请求。"""
    _expire_sessions()
    ctx = _sessions.get(x_session.strip())
    if not ctx:
        raise HTTPException(status_code=400, detail="使用人会话无效，请重新启动程序并输入使用人")
    ctx.last_seen = time.monotonic()
    _ctx_var.set(ctx)
    return ctx.operator


def current_session(operator: str = Depends(get_operator)):
    """校验浏览器保存的本地会话；服务重启后前端据此重新要求输入使用人。"""
    ctx = _ctx_var.get()
    assert ctx is not None
    if ctx.project is not None:
        _maybe_schedule_auto_backup(ctx.project, operator)
    result: dict[str, object] = {"operator": operator, "account_id": ctx.identity.account_id,
                                 "device_id": ctx.identity.device_id}
    # 项目租约被其他会话接管：一次性通知前端（体验优化：强制切换而非拒绝）
    if ctx.preempted:
        result["project_preempted"] = True
        ctx.preempted = False
    return result


def logout(x_session: str = Header(default="")):
    """显式释放当前会话及其数据库连接，避免频繁切换使用人造成资源累积。"""
    ctx = _sessions.get(x_session.strip())
    if ctx is None:
        raise HTTPException(status_code=400, detail="使用人会话无效，请重新进入工作台")
    if ctx.project is not None and job_runner.has_active(ctx.project):
        raise HTTPException(status_code=409, detail="项目仍在执行后台任务，请等待完成或先取消任务后退出")
    _sessions.pop(x_session.strip(), None)
    _close_session_project(ctx)
    return {"ok": True}


app.include_router(build_sessions_router(get_operator, login, current_session, logout))


def get_project() -> AuditProject:
    """当前会话的项目（审查 F-03 修复：从会话上下文取，不再全局共享）。"""
    ctx = _ctx_var.get()
    if ctx is None or ctx.project is None:
        raise HTTPException(status_code=400, detail="请先打开或创建项目")
    proj = ctx.project
    # I2 修复：合并/导入换库交换窗口内短暂等待，避免读请求命中已关闭连接
    if getattr(proj, "_swapping", False):
        for _ in range(60):  # 最长约 3 秒
            if not getattr(proj, "_swapping", False):
                break
            time.sleep(0.05)
        else:
            raise HTTPException(status_code=409, detail="项目正在合并或导入中，请稍后重试")
    return proj


def _require_project_idle(proj: AuditProject) -> None:
    """删除/恢复不能与当前项目的扫描、备份、合并或归档同时发生。"""
    if job_runner.has_active(proj):
        raise HTTPException(status_code=409, detail="项目正在执行扫描、备份、合并或归档任务，请完成后再操作回收站")


def _require_current_project_idle_before_switch(ctx: SessionContext, next_key: str) -> None:
    """切换项目不得关闭仍有后台任务的当前连接。"""
    if ctx.project is not None and ctx.project_key and ctx.project_key != next_key and job_runner.has_active(ctx.project):
        raise HTTPException(status_code=409, detail="当前项目仍在执行后台任务，请完成或取消后再切换项目")


def _bind_project_identity(project: AuditProject, ctx: SessionContext) -> None:
    """把当前会话的 OS 账户元数据绑定到项目，后续日志无需逐接口传参。"""
    project.set_audit_identity(ctx.identity.account_id, ctx.identity.device_id)
    ctx.archive_preflights.clear()
    ctx.merge_preflights.clear()
    ctx.batch_issue_preflights.clear()


def _project_info() -> dict:
    proj = get_project()
    return {
        "path": str(proj.root),
        "project_name": proj.project_name,
        "units": proj.list_units(),
    }


def _auto_backup_due(settings: dict, interval_minutes: int) -> bool:
    """自动备份是否到期（I4 修复：失败也参与冷却）。

    以最近一次成功或失败时间为基准，间隔未到不重试——持久性故障
    （磁盘满/上限过小）下避免每次刷新工作台都触发完整备份并占用写锁。
    """
    last_success = str(settings.get("last_success_at") or "")
    last_error_at = str(settings.get("last_error_at") or "")
    candidates = [value for value in (last_success, last_error_at) if value]
    if not candidates:
        return True
    # 以最近一次成功或失败（较新者）为基准计算冷却
    base = max(candidates)
    try:
        base_epoch = time.mktime(time.strptime(base, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return True
    return time.time() - base_epoch >= interval_minutes * 60


def _maybe_schedule_auto_backup(proj: AuditProject, operator: str, *, force: bool = False) -> dict | None:
    """由工作台心跳触发到期检查；同一项目同时只允许一个自动备份任务。"""
    settings = proj.get_backup_settings()
    if not settings["enabled"]:
        return None
    if not force and not _auto_backup_due(settings, settings["interval_minutes"]):
        return None
    active = any(
        job["type"] == "auto_backup" and job["status"] in {AuditProject.JOB_QUEUED, AuditProject.JOB_RUNNING}
        for job in proj.list_jobs(limit=100)
    )
    if active:
        return None
    job = proj.create_job("auto_backup", {"target_dir": settings["target_dir"]})

    def run_backup(job_ctx: JobContext) -> dict:
        from export import create_incremental_recovery_point

        try:
            result = create_incremental_recovery_point(
                proj,
                target_dir=settings["target_dir"],
                retention_days=settings["retention_days"],
                max_bytes=settings["max_bytes"],
                progress=job_ctx.progress,
                cancelled=job_ctx.cancelled,
            )
            detail = (
                f"新增对象 {result['copied_objects']} 个、复用对象 {result['reused_objects']} 个、"
                f"新增 {result['copied_bytes']} 字节"
            )
            proj.record_auto_backup_result(
                success=True, operator=operator, target=result["recovery_point"], message=detail,
            )
            return result
        except Exception as e:
            proj.record_auto_backup_result(success=False, message=str(e), operator=operator)
            raise

    return job_runner.submit(proj, job["id"], run_backup)


def _project_key(path: Path) -> str:
    """项目锁的规范化路径；不存在的创建目标也能得到稳定键。"""
    return str(path.resolve())


def _reserve_project(ctx: SessionContext, key: str) -> None:
    """抢占同项目单写会话锁。

    体验优化（接管语义）：其他会话已打开同一项目时不再 409 拒绝，而是
    强制接管——吊销对方项目连接（防止双写同一 SQLite），对方通过心跳
    感知 project_preempted 后提示并返回项目列表，实现"新窗口强制切换"。
    """
    def close_previous(owner_ctx: SessionContext) -> None:
        _close_session_project(owner_ctx, preserve_key=key)

    def record_failure(exc: Exception) -> None:
        log_runtime_event(
            "warning", "lease_preempt_close_failed",
            message="接管项目租约时关闭旧会话项目失败",
            error_type=type(exc).__name__, detail=str(exc),
        )

    _session_registry.reserve(ctx, key, close_previous, record_failure)


def _release_project_lease(ctx: SessionContext, key: str = "") -> None:
    target = key or ctx.project_key
    _session_registry.release(ctx, target)


# ───────────────────────── 项目 ─────────────────────────

def open_project(req: OpenReq, operator: str = Depends(get_operator)):
    raw_path = req.path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="项目路径不能为空")
    p = Path(raw_path).expanduser()
    # 目录伪装后实际目录名带 .auditproj：用户手动输入不带后缀的路径时自动补上
    if not p.is_dir() and p.name and not p.name.endswith(PROJECT_EXT):
        candidate = p.with_name(p.name + PROJECT_EXT)
        if candidate.is_dir():
            p = candidate
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"项目文件夹不存在：{p}")
    ctx = _ctx_var.get()
    assert ctx is not None  # get_operator 依赖已确保会话存在
    key = _project_key(p)
    _require_current_project_idle_before_switch(ctx, key)
    _reserve_project(ctx, key)
    try:
        # 先成功打开候选项目，再关闭当前项目。目录无权限、文件被占用等失败时，
        # 当前正在编辑的项目仍可继续使用，不能因一次“最近项目”点击而丢失会话。
        opened_project = AuditProject(p)
    except ValueError as e:
        if ctx.project_key != key:
            _release_project_lease(ctx, key)
        # 版本兼容检查（T12）：更新版本创建的项目拒绝打开，给可执行提示
        raise HTTPException(status_code=400, detail=str(e))
    except (OSError, sqlite3.Error) as e:
        if ctx.project_key != key:
            _release_project_lease(ctx, key)
        raise HTTPException(
            status_code=400,
            detail=f"无法打开项目「{p}」：{e}。请确认该文件夹存在、当前账户具有读取和写入权限，且未被其他程序锁定后重试",
        ) from e
    _close_current_project(preserve_key=key)
    ctx.project = opened_project
    ctx.project_key = key
    ctx.preempted = False
    _bind_project_identity(opened_project, ctx)
    opened_project.log(operator, "打开项目", str(p))
    remember_recent(operator, str(p), opened_project.project_name)
    return _project_info()


def create_project(req: CreateReq, operator: str = Depends(get_operator)):
    raw_path = req.path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="项目路径不能为空")
    p = Path(raw_path).expanduser()
    # 目录伪装：新建项目目录名自动追加 .auditproj 后缀（已带则不重复加）。
    # 系统选择器只能选已存在的目录——用户先建的空文件夹直接就地改名，
    # 不留无用空文件夹；目标已存在或改名失败（占用等）则用目标路径。
    if p.name and not p.name.endswith(PROJECT_EXT):
        target = p.with_name(p.name + PROJECT_EXT)
        if p.is_dir():
            if not target.exists():
                try:
                    p.rename(target)   # 消费用户先建的空文件夹，不留残留
                except OSError:
                    target = p
            else:
                # 同名 .auditproj 已存在（可能上次创建过）：用户刚建的空文件夹
                # 不再需要，删掉避免残留；非空目录（老项目）rmdir 失败则保留。
                try:
                    p.rmdir()
                except OSError:
                    pass
        p = target
    ctx = _ctx_var.get()
    assert ctx is not None  # get_operator 依赖已确保会话存在
    key = _project_key(p)
    _require_current_project_idle_before_switch(ctx, key)
    _reserve_project(ctx, key)
    try:
        opened_project = AuditProject(p)
    except ValueError as exc:
        if ctx.project_key != key:
            _release_project_lease(ctx, key)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        if ctx.project_key != key:
            _release_project_lease(ctx, key)
        raise HTTPException(
            status_code=400,
            detail=f"无法创建项目「{p}」：{exc}。请确认该目录可写，且未被其他程序锁定后重试",
        ) from exc
    _close_current_project(preserve_key=key)
    ctx.project = opened_project
    ctx.project_key = key
    ctx.preempted = False
    _bind_project_identity(ctx.project, ctx)
    name = req.name.strip()
    if name:
        ctx.project.project_name = name
    harden_project(p)  # 隐藏目录：默认 Finder/资源管理器不可见，防人员误删改
    ctx.project.log(operator, "创建项目", name or p.name)
    remember_recent(operator, str(p), ctx.project.project_name)
    return _project_info()


def delete_project(req: OpenReq, operator: str = Depends(get_operator)):
    """删除项目目录（仅限 .auditproj 伪装项目，防误删其他文件夹）。

    危险操作：数据库/附件/输出一并删除，不可恢复，前端必须二次确认。
    """
    raw_path = req.path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="项目路径不能为空")
    p = Path(raw_path).expanduser()
    if not p.name.endswith(PROJECT_EXT):
        raise HTTPException(status_code=400,
                            detail="只能删除审迹创建的项目（目录名以 .auditproj 结尾）")
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"项目不存在：{p}")
    # 其他会话打开时不能删除，避免跨标签页把正在编辑的数据移除。
    ctx = _ctx_var.get()
    key = _project_key(p)
    if ctx is not None:
        with _project_leases_lock:
            owner = _project_leases.get(key)
        if owner and owner != ctx.token:
            owner_ctx = _sessions.get(owner)
            if owner_ctx and time.monotonic() - owner_ctx.last_seen < PROJECT_LEASE_SECONDS:
                raise HTTPException(status_code=409, detail="该项目正在另一个工作台标签页中使用，不能删除")
    # 删除的是当前会话打开的项目时，先关闭连接再删目录。
    if ctx is not None and ctx.project is not None and _project_key(Path(ctx.project.root)) == key:
        _require_project_idle(ctx.project)
        _close_current_project()
    try:
        shutil.rmtree(p)
    except OSError as e:
        raise HTTPException(status_code=400,
                            detail=f"删除失败：{e}。请关闭占用该项目的程序后重试") from e
    forget_recent(operator, str(p))
    return {"deleted": str(p)}


def reset_project(req: ResetReq, operator: str = Depends(get_operator)):
    """重置项目：清空全部业务数据并完全初始化。

    危险操作，前端必须让用户输入项目名称二次确认；后端再次校验，
    与当前项目名不一致直接拒绝（防误触/跨会话误操作）。
    """
    proj = get_project()
    if req.confirm_text.strip() != proj.project_name:
        raise HTTPException(status_code=400,
                            detail="确认文字与项目名称不一致，已取消重置")
    proj.reset_all(operator)
    return {"ok": True}


def current_project(_: str = Depends(get_operator)):
    return _project_info()


def rename_project(req: NameReq, operator: str = Depends(get_operator)):
    proj = get_project()
    old = proj.project_name
    try:
        proj.project_name = req.name
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    proj.log(operator, "重命名项目", f"{old} → {proj.project_name}")
    remember_recent(operator, str(proj.root), proj.project_name)
    return _project_info()


def recent_projects(operator: str = Depends(get_operator)):
    """最近项目列表（按使用人隔离，存本机 ~/.shenji，与端口/浏览器无关）。"""
    return {"items": load_recent_all().get(operator, [])}


def forget_recent_project(path: str, operator: str = Depends(get_operator)):
    """从最近列表移除一条记录（不移除磁盘项目）。"""
    forget_recent(operator, path)
    return {"ok": True}


def _close_session_project(ctx: SessionContext, preserve_key: str = "") -> None:
    """关闭指定会话的项目，并释放其项目写入锁。"""
    proj = ctx.project
    old_key = ctx.project_key
    if proj is not None:
        try:
            proj.close()
        except sqlite3.Error as exc:
            # 关闭失败不能悄然吞掉；会话仍释放，但本机运行日志保留诊断线索。
            log_runtime_event(
                "warning", "project_close_failed", message="关闭项目连接失败",
                error_type=type(exc).__name__, detail=str(exc),
            )
    ctx.project = None
    if old_key != preserve_key:
        _release_project_lease(ctx, old_key)
    ctx.project_key = preserve_key if old_key == preserve_key else ""


def _close_current_project(preserve_key: str = ""):
    """关闭当前会话持有的项目（如有）。"""
    ctx = _ctx_var.get()
    if ctx is not None:
        _close_session_project(ctx, preserve_key)


# D01：先拆出无副作用的项目预设路由；路由工厂复用现有会话依赖，保持 v1.2
# 的 URL、方法和响应不变，后续按相同模式继续拆分项目、单位、底稿等模块。
app.include_router(build_settings_router(get_project, get_operator))
app.include_router(build_units_router(get_project, get_operator, _require_project_idle))
app.include_router(build_issues_router(get_project, get_operator, get_current_context))
app.include_router(build_exchanges_router(get_project, get_operator))
app.include_router(build_recycle_router(get_project, get_operator, _require_project_idle))
app.include_router(build_evidence_links_router(get_project, get_operator))
app.include_router(build_project_runtime_router(
    get_project, get_operator, get_current_context, job_runner.submit, job_runner.cancel,
))
app.include_router(build_projects_router(
    get_project, get_operator, lambda path: AuditProject(path), get_current_context, _project_key,
    _require_current_project_idle_before_switch, _reserve_project, _release_project_lease,
    _close_current_project, _bind_project_identity, _project_info, _require_project_idle,
    _session_registry.active_owner, PROJECT_LEASE_SECONDS,
))


# ───────────────────────── 附件 ─────────────────────────

def list_files(unit_id: int, _: str = Depends(get_operator)):
    return get_project().list_files(unit_id)


def unlinked_files(unit_id: int, _: str = Depends(get_operator)):
    return get_project().unlinked_files(unit_id)


def open_unit_attachment_directory(unit_id: int, operator: str = Depends(get_operator)):
    """在系统文件管理器中打开单位附件库。

    路径只能由项目和 unit_id 在服务端解析，避免前端按显示名称拼接目录，
    也避免把任意本地路径暴露给工作台操作。
    """
    proj = get_project()
    try:
        directory = proj.unit_attachment_dir(unit_id)
        open_path(directory)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PlatformError as e:
        raise HTTPException(status_code=404, detail=str(e))
    unit = proj.get_unit(unit_id)
    proj.log(operator, "打开附件目录", unit["name"] if unit else f"单位{unit_id}")
    return {"ok": True}


def open_evidence_folder(file_id: int, operator: str = Depends(get_operator)):
    """打开“文件夹证据”自身目录，而不是错误地下载或跳到单位根目录。"""
    proj = get_project()
    evidence = proj.get_file(file_id)
    if not evidence:
        raise HTTPException(status_code=404, detail="附件不存在")
    if evidence.get("mime") != "folder":
        raise HTTPException(status_code=400, detail="只有文件夹证据可查看目录")
    try:
        directory = proj.attachment_path(evidence["rel_path"])
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        open_path(directory)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (FileNotFoundError, PlatformError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    proj.log(operator, "查看附件目录", evidence["orig_name"])
    return {"ok": True}


async def upload_folder(unit_id: int, folder_name: str = Form(...),
                        files: list[UploadFile] = File(...),
                        operator: str = Depends(get_operator)):
    """文件夹上传：内容打包 zip 存为单个附件实体（按单文件规则处理）。

    files 的 filename 携带 zip 内相对路径（前端递归展开后传入）。
    """
    from limits import MAX_BATCH_FILES, MAX_EXTRACT_TOTAL, MAX_FILE_SIZE, human_size

    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400,
                            detail=f"单批最多上传 {MAX_BATCH_FILES} 个文件，当前 {len(files)} 个")
    proj = get_project()
    tmp_items = []
    batch_size = 0
    try:
        for f in files:
            rel = (f.filename or f.name or "文件").replace("\\", "/")
            try:
                AuditProject._folder_member_path(Path(tempfile.gettempdir()), rel)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            suffix = Path(rel).suffix or ".bin"
            size = 0
            hasher = hashlib.sha256()
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                while True:
                    chunk = await f.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    batch_size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        # 先关闭再删（Windows 删除打开的文件会 WinError 32）；不 append 进 tmp_items，finally 不会重复删
                        tf.close()
                        Path(tf.name).unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=400,
                            detail=f"文件「{rel}」超过单文件上限 {human_size(MAX_FILE_SIZE)}")
                    if batch_size > MAX_EXTRACT_TOTAL:
                        tf.close()
                        Path(tf.name).unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=400,
                            detail=f"文件夹总量超过上限 {human_size(MAX_EXTRACT_TOTAL)}，请拆分后再导入")
                    await run_in_threadpool(tf.write, chunk)
                    hasher.update(chunk)
                tmp_items.append((rel, tf.name, hasher.hexdigest()))
        if not tmp_items:
            raise ValueError("文件夹为空")
        # 文件夹内容指纹：相对路径 + 文件内容哈希，排序后整体摘要（同目录同内容才判重）
        fingerprint = await run_in_threadpool(_folder_fingerprint, tmp_items)
        proj = get_project()
        dup = await run_in_threadpool(proj.find_folder_by_fingerprint, fingerprint)
        if dup:
            return {
                "duplicated": True,
                "file": dup,
                "message": f"工作区已存在相同文件夹「{dup['orig_name']}」（单位：{dup['unit_name']}），已复用，不重复存储",
            }
        rec = await run_in_threadpool(proj.add_folder, unit_id, tmp_items, folder_name, operator, fingerprint)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        for _rel, tmp, *_digest in tmp_items:
            Path(tmp).unlink(missing_ok=True)
    return rec


async def upload_file(unit_id: int, file: UploadFile = File(...), folder_path: str = Form(""),
                       operator: str = Depends(get_operator)):
    """附件上传：项目级重复检测（同内容只存一份）→ 入库 → 可关联。

    folder_path 可选：所属文件夹相对路径（文件夹上传时由前端递归展开传入）。
    """
    from limits import MAX_FILE_SIZE, human_size

    orig = file.filename or "未命名文件"
    suffix = Path(orig).suffix or ".bin"
    size = 0
    hasher = hashlib.sha256()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
        # 流式写入、计数并计算摘要：避免落盘后再次完整读取临时文件。
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                # 先关闭再删临时文件（Windows 上删除打开的文件会 WinError 32；macOS 无此限制）
                tf.close()
                Path(tf.name).unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail=f"文件超过单文件上限 {human_size(MAX_FILE_SIZE)}，请拆分后再导入")
            await run_in_threadpool(tf.write, chunk)
            hasher.update(chunk)
        tmp_path = tf.name
    try:
        sha = hasher.hexdigest()
        # 项目级查重：同一实体文件只保存一份
        proj = get_project()
        dup = await run_in_threadpool(proj.find_file_by_sha, sha)
        if dup:
            return {
                "duplicated": True,
                "file": dup,
                "message": f"工作区已存在相同文件「{dup['orig_name']}」（单位：{dup['unit_name']}），已复用，不重复存储",
            }
        f = await run_in_threadpool(
            proj.add_file, unit_id, tmp_path, operator, orig_name=orig, folder_path=folder_path,
            verified_sha256=sha, verified_size=size,
        )
    except (KeyError, FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return f


def download_file(file_id: int, _: str = Depends(get_operator)):
    proj = get_project()
    f = proj.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="附件不存在")
    try:
        path = proj.attachment_path(f["rel_path"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not path.exists():
        raise HTTPException(status_code=404, detail="附件文件已丢失，请检查附件库目录")
    return FileResponse(path, filename=f["orig_name"])


def open_file(file_id: int, operator: str = Depends(get_operator)):
    """用系统默认程序打开附件文件（macOS open / Windows 默认关联程序）。"""
    proj = get_project()
    f = proj.get_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="附件不存在")
    try:
        path = proj.attachment_path(f["rel_path"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not path.exists():
        raise HTTPException(status_code=404, detail="附件文件已丢失，请检查附件库目录")
    try:
        open_path(path)
    except PlatformError as e:
        raise HTTPException(status_code=400, detail=str(e))
    proj.log(operator, "打开附件", f["orig_name"])
    return {"ok": True}


def rename_file(file_id: int, req: NameReq, operator: str = Depends(get_operator)):
    try:
        get_project().rename_file(file_id, req.name, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


def batch_rename_files(req: BatchRenameReq, operator: str = Depends(get_operator)):
    """批量重命名附件：事务内冲突检测，冲突条目跳过并返回原因（审查 F-06 补齐）。"""
    return get_project().batch_rename_files(
        [{"id": r.id, "name": r.name} for r in req.renames], operator)


def move_file(file_id: int, req: MoveFileReq, operator: str = Depends(get_operator)):
    """移动附件到其他单位：物理移动 + 事务更新归属（审查 F-06 补齐）。"""
    try:
        return get_project().move_file_to_unit(file_id, req.unit_id, operator)
    except (KeyError, ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))


def remove_file(file_id: int, operator: str = Depends(get_operator)):
    try:
        proj = get_project()
        _require_project_idle(proj)
        proj.remove_file(file_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        # 删除保护：附件仍被底稿引用
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


app.include_router(build_evidence_operations_router(
    get_operator,
    open_unit_attachment_directory,
    open_evidence_folder,
    upload_folder,
    upload_file,
    download_file,
    open_file,
    rename_file,
    batch_rename_files,
    move_file,
    remove_file,
))


def issues_for_file(file_id: int, _: str = Depends(get_operator)):
    """反查：附件被哪些底稿引用。"""
    return get_project().issues_for_file(file_id)


# ───────────────────────── 底稿↔附件 关联 ─────────────────────────

def files_for_issue(issue_id: int, _: str = Depends(get_operator)):
    if not get_project().get_issue(issue_id):
        raise HTTPException(status_code=404, detail="底稿不存在或已移入回收站")
    return get_project().files_for_issue(issue_id)


def link_file(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
    try:
        get_project().link_file(issue_id, file_id, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


def unlink_file(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
    try:
        get_project().unlink_file(issue_id, file_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


def link_exclusive(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
    """仅关联到当前问题（独占）：附件移出资料库，其他底稿不可见。"""
    try:
        get_project().link_file_exclusive(issue_id, file_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


def clear_exclusive(file_id: int, operator: str = Depends(get_operator)):
    """恢复共享：附件回到资料库，其他底稿可继续使用。"""
    try:
        get_project().clear_file_exclusive(file_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


# ───────────────────────── 操作日志 ─────────────────────────

def list_logs(limit: int = 500, _: str = Depends(get_operator)):
    return get_project().list_logs(max(1, min(limit, 5000)))


# ───────────────────────── 导入问题汇总 ─────────────────────────

def import_template(_: str = Depends(get_operator)):
    """下载导入模板 xlsx。"""
    import tempfile
    from uuid import uuid4

    from export import build_import_template

    proj = get_project()
    tmp = Path(tempfile.gettempdir()) / f"audit_template_{uuid4().hex[:8]}.xlsx"
    build_import_template(tmp)
    return FileResponse(
        tmp,
        filename=f"问题导入模板_{proj.project_name}.xlsx",
        background=BackgroundTask(tmp.unlink, missing_ok=True),
    )


async def import_excel(file: UploadFile = File(...), operator: str = Depends(get_operator)):
    """上传整理好的 xlsx，一键导入底稿（单位不存在自动创建）。"""
    import tempfile
    from uuid import uuid4

    from export import import_from_excel as do_import
    from limits import MAX_FILE_SIZE, human_size

    proj = get_project()
    suffix = Path(file.filename or "import.xlsx").suffix or ".xlsx"
    tmp = Path(tempfile.gettempdir()) / f"audit_import_{uuid4().hex[:8]}{suffix}"
    size = 0
    with open(tmp, "wb") as fh:
        # 流式写入并计数，超限提前拒绝（审查 F-07 修复）
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                tmp.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail=f"导入文件超过上限 {human_size(MAX_FILE_SIZE)}，请拆分后再导入")
            fh.write(chunk)
    try:
        result = await run_in_threadpool(do_import, proj, tmp, operator)
        result["filename"] = file.filename or ""
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp.unlink(missing_ok=True)


async def import_merge(files: list[UploadFile] = File(...),
                       operator: str = Depends(get_operator)):
    """旧上传入口已停用：正式合并需本机路径预检，避免大包限制和绕过冲突确认。"""
    from limits import MAX_BATCH_FILES

    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"单批最多合并 {MAX_BATCH_FILES} 个备份，当前 {len(files)} 个",
        )
    raise HTTPException(
        status_code=409,
        detail="请在合并窗口逐行输入本机 .auditbak 完整路径，先完成预检并确认冲突后再合并",
    )


async def import_merge_local(req: LocalMergeReq, operator: str = Depends(get_operator)):
    """通过预检确认后从本机路径合并，适用于 50GB 场景。"""
    from export import merge_backups, merge_preflight
    from limits import MAX_BATCH_FILES

    paths = [Path(raw.strip()).expanduser() for raw in req.backup_paths if raw.strip()]
    if not paths:
        raise HTTPException(status_code=400, detail="请至少输入一个 .auditbak 备份完整路径")
    if len(paths) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"单批最多合并 {MAX_BATCH_FILES} 个备份，当前 {len(paths)} 个")
    for path in paths:
        if path.suffix.lower() != ".auditbak" or not path.is_file():
            raise HTTPException(status_code=400, detail=f"备份文件不存在或不是 .auditbak：{path}")
    ctx = _ctx_var.get()
    assert ctx is not None
    approved = ctx.merge_preflights.pop(req.confirmation_token.strip(), None)
    if not approved:
        raise HTTPException(status_code=409, detail="请先完成合并预检并确认冲突处理方式")
    if approved["expires_at"] < time.monotonic():
        raise HTTPException(status_code=409, detail="合并预检已过期，请重新预检")
    canonical_paths = [str(path.resolve()) for path in paths]
    if approved["project_uuid"] != get_project().project_uuid or approved["paths"] != canonical_paths:
        raise HTTPException(status_code=409, detail="待合并来源或当前项目已变化，请重新预检")
    try:
        current = await run_in_threadpool(merge_preflight, get_project(), paths)
        if not current["ok"]:
            raise HTTPException(status_code=409, detail="合并预检发现阻断项，请修复来源后重新预检")
        if (
            current["fingerprint"] != approved["fingerprint"]
            or current["target_fingerprint"] != approved["target_fingerprint"]
        ):
            raise HTTPException(status_code=409, detail="来源备份或当前项目已变化，请重新预检")
        proj = get_project()
        result = await run_in_threadpool(
            job_runner.run_and_wait,
            proj,
            "merge_backups",
            {"sources": canonical_paths},
            lambda _ctx: merge_backups(proj, paths, operator),
        )
        proj.log(
            operator, "确认合并备份", f"{len(paths)} 个来源",
            f"预检冲突 {len(current['conflicts'])} 项；默认并存且已由负责人确认",
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


async def import_merge_local_preflight(req: LocalMergeReq, _: str = Depends(get_operator)):
    """对本机备份来源做只读预检，发现冲突先展示，确认后才允许写入。"""
    from export import merge_preflight
    from limits import MAX_BATCH_FILES

    paths = [Path(raw.strip()).expanduser() for raw in req.backup_paths if raw.strip()]
    if not paths:
        raise HTTPException(status_code=400, detail="请至少输入一个 .auditbak 备份完整路径")
    if len(paths) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"单批最多合并 {MAX_BATCH_FILES} 个备份，当前 {len(paths)} 个")
    result = await run_in_threadpool(merge_preflight, get_project(), paths)
    if result["ok"]:
        ctx = _ctx_var.get()
        assert ctx is not None
        token = uuid.uuid4().hex
        ctx.merge_preflights[token] = {
            "project_uuid": get_project().project_uuid,
            "paths": [str(path.resolve()) for path in paths],
            "fingerprint": result.pop("fingerprint"),
            "target_fingerprint": result.pop("target_fingerprint"),
            "expires_at": time.monotonic() + 10 * 60,
        }
        result["confirmation_token"] = token
    else:
        result.pop("fingerprint", None)
        result.pop("target_fingerprint", None)
        result["confirmation_token"] = ""
    return result


# ───────────────────────── 导出 / 打包 / 备份 ─────────────────────────

def export_excel(req: ExportReq, operator: str = Depends(get_operator)):
    """导出问题汇总表 Excel（unit=当前单位 / project=全部单位）。"""
    from export import export_excel as do_export
    proj = get_project()
    try:
        info = do_export(proj, scope=req.scope, operator=operator, unit_id=req.unit_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scope_name = "当前单位" if req.scope == "unit" else "全部单位"
    proj.log(operator, "导出Excel", info["filename"], f"范围：{scope_name}，{info['count']} 条")
    return {"filename": info["filename"], "abs_path": info["abs_path"],
            "count": info["count"], "download_url": f"/api/export/file/{quote(info['filename'])}"}


def package_project(req: PackageReq, operator: str = Depends(get_operator)):
    """通过归档核对令牌后打包 ZIP；项目变化或核对过期必须重新确认。"""
    from export import archive_preflight
    from export import package_project as do_package
    proj = get_project()
    ctx = _ctx_var.get()
    assert ctx is not None
    token = req.confirmation_token.strip()
    approved = ctx.archive_preflights.pop(token, None) if token else None
    if not approved:
        raise HTTPException(status_code=409, detail="请先完成归档核对清单，再确认生成归档包")
    if approved["expires_at"] < time.monotonic():
        raise HTTPException(status_code=409, detail="归档核对已过期，请重新核对")
    requested_ids = list(dict.fromkeys(req.unit_ids or []))
    if (
        approved["project_uuid"] != proj.project_uuid
        or approved["scope"] != req.scope
        or approved["unit_ids"] != requested_ids
        or approved["group_by_dept"] != bool(req.group_by_dept)
    ):
        raise HTTPException(status_code=409, detail="归档范围已变化，请重新核对")
    try:
        current = archive_preflight(
            proj, scope=req.scope, unit_ids=requested_ids, group_by_dept=req.group_by_dept,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not current["ok"]:
        raise HTTPException(status_code=409, detail="归档核对发现阻断项，请修复后重新核对")
    if current["fingerprint"] != approved["fingerprint"]:
        raise HTTPException(status_code=409, detail="核对后项目数据或附件已变化，请重新核对")
    try:
        info = job_runner.run_and_wait(
            proj,
            "archive_package",
            {"scope": req.scope, "unit_ids": requested_ids, "group_by_dept": bool(req.group_by_dept)},
            lambda _ctx: do_package(
                proj, scope=req.scope, unit_ids=requested_ids, group_by_dept=req.group_by_dept,
                operator=operator,
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scope_name = "全部单位" if req.scope == "all" else f"勾选单位 {len(req.unit_ids)} 个"
    proj.log(operator, "归档核对并打包", info["filename"],
             f"{scope_name}，{info['units']} 个单位、{info['issues']} 条底稿；"
             f"核对警告 {len(current['warnings'])} 项"
             + ("，按版块分类" if req.group_by_dept else ""))
    return {"filename": info["filename"], "abs_path": info["abs_path"],
            "units": info["units"], "issues": info["issues"],
            "download_url": f"/api/export/file/{quote(info['filename'])}"}


def package_preflight(req: PackageReq, _: str = Depends(get_operator)):
    """生成归档核对清单。无阻断项时发放一次性确认令牌，有效期 10 分钟。"""
    from export import archive_preflight

    proj = get_project()
    ctx = _ctx_var.get()
    assert ctx is not None
    selected_ids = list(dict.fromkeys(req.unit_ids or []))
    try:
        result = archive_preflight(
            proj, scope=req.scope, unit_ids=selected_ids, group_by_dept=req.group_by_dept,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if result["ok"]:
        token = uuid.uuid4().hex
        ctx.archive_preflights[token] = {
            "project_uuid": proj.project_uuid,
            "scope": req.scope,
            "unit_ids": selected_ids,
            "group_by_dept": bool(req.group_by_dept),
            "fingerprint": result.pop("fingerprint"),
            "expires_at": time.monotonic() + 10 * 60,
        }
        result["confirmation_token"] = token
    else:
        result.pop("fingerprint", None)
        result["confirmation_token"] = ""
    return result


def create_backup(operator: str = Depends(get_operator)):
    """备份项目（audit.db + 附件库）到上级目录 .auditbak。"""
    from export import create_backup as do_backup
    proj = get_project()
    try:
        info = job_runner.run_and_wait(
            proj,
            "manual_backup",
            {},
            lambda _ctx: do_backup(proj),
        )
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"创建备份失败：{e}") from e
    proj.log(operator, "备份", info["filename"], f"{info['db_size']} 字节")
    return {"filename": info["filename"], "abs_path": info["abs_path"],
            "download_url": f"/api/backup/download/{quote(info['filename'])}"}


def get_backup_settings(_: str = Depends(get_operator)):
    return get_project().get_backup_settings()


def save_backup_settings(req: BackupSettingsReq, operator: str = Depends(get_operator)):
    try:
        return get_project().save_backup_settings(
            operator, enabled=req.enabled, target_dir=req.target_dir,
            interval_minutes=req.interval_minutes, retention_days=req.retention_days,
            max_bytes=req.max_bytes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def create_auto_recovery_point(operator: str = Depends(get_operator)):
    """手工立即创建一份增量恢复点；仍使用已保存的自动备份目标和空间策略。"""
    proj = get_project()
    settings = proj.get_backup_settings()
    if not settings["enabled"]:
        raise HTTPException(status_code=400, detail="请先在设置中开启自动备份并指定目标目录")
    job = _maybe_schedule_auto_backup(proj, operator, force=True)
    if job is None:
        raise HTTPException(status_code=409, detail="自动备份正在执行，请稍后查看任务结果")
    return {"job_id": job["id"], "status": job["status"]}


def list_auto_recovery_points(_: str = Depends(get_operator)):
    """列出当前项目自动备份目标中的可用恢复点。"""
    from export import list_incremental_recovery_points

    proj = get_project()
    settings = proj.get_backup_settings()
    if not settings["target_dir"]:
        return []
    try:
        return list_incremental_recovery_points(proj.project_uuid, settings["target_dir"])
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"读取自动备份恢复点失败：{e}") from e


def _log_restored_project(
    path: str, operator: str, account_id: str, device_id: str, action: str, detail: str = "",
) -> None:
    """恢复成功后，在新项目自己的永久日志中记录来源。"""
    restored = AuditProject(path)
    try:
        restored.set_audit_identity(account_id, device_id)
        restored.log(operator, action, path, detail)
        # P10：恢复出的项目直接补写 manifest.json，避免恢复后清单缺失
        restored.write_manifest()
    finally:
        restored.close()


async def restore_auto_recovery_point(req: RecoveryPointRestoreReq, operator: str = Depends(get_operator)):
    """从内容寻址自动备份恢复点恢复；始终写入一个新项目目录。"""
    from export import restore_incremental_recovery_point

    proj = get_project()
    ctx = _ctx_var.get()
    assert ctx is not None
    settings = proj.get_backup_settings()
    if not settings["target_dir"]:
        raise HTTPException(status_code=400, detail="当前项目未设置自动备份目标目录")
    target = req.target_dir.strip()
    if not target:
        raise HTTPException(status_code=400, detail="恢复目标目录不能为空")
    # 重要6 修复：恢复目标不得位于当前项目内，防止覆盖正在编辑的项目或产生嵌套项目
    target_resolved = Path(target).expanduser().resolve()
    project_root = Path(proj.root).resolve()
    if target_resolved == project_root or target_resolved.is_relative_to(project_root):
        raise HTTPException(
            status_code=400,
            detail="恢复目标不能位于当前项目内，请选择项目外的其他目录",
        )
    try:
        info = await run_in_threadpool(
            job_runner.run_and_wait,
            proj,
            "restore_recovery_point",
            {"recovery_point_id": req.recovery_point_id, "target_dir": target},
            lambda _ctx: restore_incremental_recovery_point(
                project_uuid=proj.project_uuid,
                backup_target_dir=settings["target_dir"],
                recovery_point_id=req.recovery_point_id,
                target_dir=target,
            ),
        )
        await run_in_threadpool(
            _log_restored_project, info["path"], operator,
            ctx.identity.account_id, ctx.identity.device_id,
            "恢复自动备份", f"恢复点：{req.recovery_point_id}"
        )
        return {"path": info["path"]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def download_backup(filename: str, _: str = Depends(get_operator)):
    """下载备份 .auditbak（存放于项目上级目录，不走输出目录端点）。

    防目录穿越：解析后的路径必须落在项目上级目录内。
    """
    from urllib.parse import unquote
    proj = get_project()
    parent_resolved = proj.root.parent.resolve()
    p = (parent_resolved / unquote(filename)).resolve()
    if p.parent != parent_resolved or p.suffix.lower() != ".auditbak":
        raise HTTPException(status_code=400, detail="非法文件名")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="备份文件不存在（可能已被移动或清理）")
    return FileResponse(p, filename=p.name)


async def restore_backup(file: UploadFile = File(...), target_dir: str = Form(...),
                         operator: str = Depends(get_operator)):
    """恢复备份：上传 .auditbak + 目标目录（须为空）。"""
    from export import restore_backup as do_restore
    from limits import MAX_FILE_SIZE, human_size
    target = target_dir.strip()
    if not target:
        raise HTTPException(status_code=400, detail="目标目录不能为空")
    suffix = Path(file.filename or "restore.auditbak").suffix or ".auditbak"
    size = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
        tmp_bak = tf.name
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                Path(tmp_bak).unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail=f"备份文件超过上传上限 {human_size(MAX_FILE_SIZE)}，请拆分或从本机项目上级目录直接使用备份",
                )
            tf.write(chunk)
    try:
        ctx = _ctx_var.get()
        if ctx is not None and ctx.project is not None:
            info = await run_in_threadpool(
                job_runner.run_and_wait,
                ctx.project,
                "restore_backup",
                {"target_dir": target, "source": file.filename or ""},
                lambda _ctx: do_restore(tmp_bak, target),
            )
        else:
            # 首次启动时可先恢复项目再打开；没有当前项目就不存在并发项目任务。
            info = await run_in_threadpool(do_restore, tmp_bak, target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        Path(tmp_bak).unlink(missing_ok=True)
    ctx = _ctx_var.get()
    assert ctx is not None
    await run_in_threadpool(
        _log_restored_project, info["path"], operator,
        ctx.identity.account_id, ctx.identity.device_id, "恢复备份",
    )
    return {"path": info["path"]}


async def restore_local_backup(req: LocalBackupRestoreReq, operator: str = Depends(get_operator)):
    """从本机路径恢复完整 .auditbak，避免 50GB 文件经浏览器上传的大小限制。"""
    from export import restore_backup as do_restore

    backup_path = req.backup_path.strip()
    target = req.target_dir.strip()
    if not backup_path or not target:
        raise HTTPException(status_code=400, detail="备份文件路径和恢复目标目录均不能为空")
    source = Path(backup_path).expanduser()
    if source.suffix.lower() != ".auditbak":
        raise HTTPException(status_code=400, detail="请选择 .auditbak 备份文件")
    ctx = _ctx_var.get()
    assert ctx is not None
    try:
        ctx = _ctx_var.get()
        if ctx is not None and ctx.project is not None:
            info = await run_in_threadpool(
                job_runner.run_and_wait,
                ctx.project,
                "restore_local_backup",
                {"target_dir": target, "source": str(source)},
                lambda _ctx: do_restore(source, target),
            )
        else:
            info = await run_in_threadpool(do_restore, source, target)
        await run_in_threadpool(
            _log_restored_project, info["path"], operator,
            ctx.identity.account_id, ctx.identity.device_id, "恢复本地备份", str(source),
        )
        return {"path": info["path"]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def download_export(filename: str, _: str = Depends(get_operator)):
    """下载输出目录中的导出文件（Excel/ZIP/备份）。"""
    from urllib.parse import unquote
    proj = get_project()
    out_resolved = (proj.root / OUT_DIR).resolve()
    p = (out_resolved / unquote(filename)).resolve()
    # 防目录穿越：必须落在输出目录内
    if p.parent != out_resolved or p.suffix.lower() not in {".xlsx", ".zip", ".txt"}:
        raise HTTPException(status_code=400, detail="非法文件名")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在（可能已被移动）")
    return FileResponse(p, filename=p.name)


# ───────────────────────── 系统辅助（平台适配层） ─────────────────────────

def restart_program(_: str = Depends(get_operator)):
    """重启程序：结束当前服务进程并重新拉起（页面卡死/数据异常时自救）。

    返回响应后延迟 0.8s 执行：先关闭所有项目连接（SQLite 干净落盘），
    再后台拉起新进程（开发态同命令同目录；打包态直接重启可执行文件），
    最后退出当前进程。新进程会重新分配端口并自动打开浏览器。
    """
    _schedule_restart()
    return {"ok": True, "message": "程序正在重启，请稍候…"}


def _schedule_restart():
    """延迟执行重启（先让当前请求返回，前端才能收到确认消息）。"""
    threading.Timer(0.8, _do_restart).start()


def _do_restart():
    """实际重启动作（Timer 线程执行，测试可 monkeypatch _schedule_restart 拦下）。"""
    # 关闭所有会话持有的项目连接，保证数据干净落盘
    for ctx in list(_sessions.values()):
        _close_session_project(ctx)
    try:
        if getattr(sys, "frozen", False):
            # 打包版：sys.executable 就是程序本体，直接重启
            cmd = [sys.executable]
            cwd = None
        else:
            script = Path(sys.argv[0]).resolve() if sys.argv else Path("main.py").resolve()
            cmd = [sys.executable, str(script)]
            # 保持原进程工作目录重启（从任意目录启动都原样恢复）
            cwd = os.getcwd()
        spawn_detached(cmd, cwd=cwd)
    except PlatformError:
        pass
    finally:
        # 立即退出当前进程（不跑 atexit/finally 清理；flock 随 fd 关闭自动释放）
        os._exit(0)


def quit_program(_: str = Depends(get_operator)):
    """退出程序：关闭所有项目连接（SQLite 干净落盘）后退出进程。

    打包版为 LSUIElement（无 Dock 图标），页面内退出是唯一优雅关闭入口。
    """
    threading.Timer(0.8, _do_quit).start()
    return {"ok": True, "message": "程序正在退出…"}


def _do_quit():
    """实际退出动作（Timer 线程执行，测试可 monkeypatch _schedule_restart 拦下）。"""
    for ctx in list(_sessions.values()):
        _close_session_project(ctx)
    # 立即退出当前进程（不跑 atexit/finally 清理；flock 随 fd 关闭自动释放）
    os._exit(0)


def choose_folder(_: str = Depends(get_operator)):
    """弹系统原生文件夹选择器（平台适配层 choose_folder）。

    浏览器安全限制无法直接选文件夹路径，由后端弹原生对话框返回路径。
    用户取消时返回空路径。
    """
    try:
        return {"path": platform_choose_folder()}
    except PlatformError as e:
        # 选择器依赖系统图形会话；受限开发进程或自动化权限关闭时不能显示。
        # 这不应抹掉用户已输入的路径，也不该以“请求失败”掩盖手输路径这一可行替代。
        return {"path": "", "warning": f"{e}。未修改已输入路径，请直接粘贴项目文件夹完整路径。"}


def open_folder(req: FolderReq, _: str = Depends(get_operator)):
    """在系统文件管理器中打开指定文件夹（平台适配层 open_path）。"""
    try:
        open_path(req.path)
    except PlatformError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


app.include_router(build_operations_router(
    get_operator,
    list_logs,
    import_template,
    import_excel,
    import_merge,
    import_merge_local,
    import_merge_local_preflight,
    export_excel,
    package_project,
    package_preflight,
    create_backup,
    get_backup_settings,
    save_backup_settings,
    create_auto_recovery_point,
    list_auto_recovery_points,
    restore_auto_recovery_point,
    download_backup,
    restore_backup,
    restore_local_backup,
    download_export,
    restart_program,
    quit_program,
    choose_folder,
    open_folder,
))


# ───────────────────────── 静态资源（必须最后挂载） ─────────────────────────

mount_frontend(app, FRONTEND_DIR)


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """SPA 入口不缓存（修复"初次进入空白，刷新后恢复"）。

    前端每次构建会删除旧 hash 资源；若浏览器缓存了旧 index.html，
    会引用已删除的旧 JS → 404 白屏。入口 HTML 每次重新验证（ETag 304），
    hash 资源正常缓存。
    """
    response = await call_next(request)
    path = request.url.path
    if not path or path.endswith(("/", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _install_crash_hook() -> Path:
    """安装全局异常钩子：未捕获异常写崩溃日志（T10 崩溃日志位置）。

    打包后（PyInstaller windowed）无终端可见 traceback，异常落盘到
    ~/.shenji/logs/crash_*.log 才能排查。同时打印日志位置提示。
    返回日志目录。
    """
    from platform_adapter import CONFIG_DIR

    log_dir = CONFIG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    def _hook(exc_type, exc, tb):
        from datetime import datetime as _dt

        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"crash_{ts}.log"
        try:
            body = "".join(traceback.format_exception(exc_type, exc, tb))
            path.write_text(
                f"审迹崩溃日志\n时间：{_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n{body}\n",
                encoding="utf-8",
            )
            print(f"[崩溃] 详情已写入：{path}", file=sys.stderr)
        except Exception:  # 崩溃处理自身失败不阻断退出
            pass

    sys.excepthook = _hook
    # 线程异常也落盘（PyInstaller windowed 模式下线程崩溃无终端可见）
    def _thread_hook(args):
        _hook(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = _thread_hook
    return log_dir


def main():
    """程序入口：--serve = 服务常驻（detached 子进程）；默认 = 启动器。

    启动器模式（每次双击执行的路径）：
      服务已在跑 → 打开页面后立即退出；未在跑 → 拉起服务子进程（--serve），
      等端口就绪后打开页面再退出。
    这样 LaunchServices 始终认为应用“未在运行”——否则 macOS 会把第二次双击
    当成“激活已有实例”，而 LSUIElement 应用没有可激活的 UI，报“没有响应”。
    """
    # Windows 打包版（console=False / windowed）没有控制台，sys.stdout/stderr 为 None，
    # uvicorn 的 ColourizedFormatter 初始化调 sys.stdout.isatty() → AttributeError 崩溃。
    # 兜底为 devnull 流：isatty()=False 关闭颜色输出，日志本就走崩溃文件/丢弃，不影响功能。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 替换全局流，句柄须存活到进程退出
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 替换全局流，句柄须存活到进程退出
    _install_crash_hook()
    if "--serve" in sys.argv:
        serve_app(app, instance_lock_name=INSTANCE_LOCK_NAME, host=HOST, port=SETTINGS.port)
    else:
        launch_service(instance_lock_name=INSTANCE_LOCK_NAME, host=HOST, port=SETTINGS.port)


if __name__ == "__main__":
    main()
