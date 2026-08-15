"""FastAPI 入口 — 本地服务 + 静态托管 + 启动即开浏览器。

设计：
- 所有接口强制使用人（X-Operator 请求头），缺失即拒绝 —— 与前端启动弹窗双保险
- 项目按会话隔离：每个会话 token 独立持有自己的项目（审查 F-03 修复），
  不同浏览器会话可各自打开不同项目，互不干扰
- 查看类接口不写日志，变更类接口由数据层自动留痕
- 前端静态资源在 frontend-v3/dist，根路径挂载。
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import uuid
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

# 兼容两种启动方式：`python backend/main.py` 与 `uvicorn main:app`（根目录转发）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_models import (
    AmountSettingsReq,
    BackupSettingsReq,
    BatchRenameReq,
    CategoryReq,
    CreateReq,
    DeptReq,
    ExchangeCloseReq,
    ExchangeCommentReq,
    ExchangeRequestReq,
    ExchangeRequestUpdateReq,
    ExchangeRevisionDecisionReq,
    ExchangeRevisionReq,
    ExportReq,
    FolderReq,
    IssueNumberReq,
    IssueReq,
    LocalBackupRestoreReq,
    LocalMergeReq,
    MoveFileReq,
    NameReq,
    OpenReq,
    OperatorReq,
    PackageReq,
    RecoveryPointRestoreReq,
    ResetReq,
    StatusReq,
)
from app_launcher import launch_service, serve_app
from config import PROJECT_EXT, RuntimeSettings
from database import OUT_DIR, AuditProject
from jobs import JobContext, job_runner
from platform_adapter import (
    OSIdentity,
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
from runtime_log import install_unhandled_error_handler, log_runtime_event

SETTINGS = RuntimeSettings.from_environment()
HOST = SETTINGS.host
# 改造版独立于原审迹 v1.1 的单实例锁和端点记录。仅改端口还不够：若共用
# shenji.lock，启动器会把原版实例误认为当前版本并直接打开它的页面。
INSTANCE_LOCK_NAME = "shenji-v11-upgrade.lock"
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
        # 上传流已得到成员摘要时直接复用；历史/导入调用保留落盘后计算的兼容路径。
        member_sha = item[2] if len(item) > 2 else _sha256_of(tmp)
        parts.append(f"{rel.replace(chr(92), '/')}\t{member_sha}")
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

app = FastAPI(title="审迹", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.exception_handler(InterruptedError)
async def _handle_interrupted(_request: Request, exc: InterruptedError) -> JSONResponse:
    """I5：同步任务被取消或等待超时统一转 409，不再泄漏为 500。"""
    return JSONResponse(status_code=409, content={"detail": str(exc) or "任务已取消"})
# 仅监听回环地址不足以阻断 DNS rebinding：浏览器仍可能将 evil.example 的 Host
# 请求送到本地端口。明确拒绝非本机 Host；testserver 仅供 FastAPI TestClient。
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
install_unhandled_error_handler(app)

# 会话表：token → 会话上下文（使用人 + 该会话打开的项目）。
# 每个会话独立持有项目；同一项目同时只允许一个会话写入，避免第二标签页
# 或第二浏览器连接造成 SQLite 并发写入。
# 启动弹窗登录换取 token（HTTP header 只传 ASCII 安全值，中文直接放 header
# 会被 Latin-1 解码成乱码，浏览器 fetch 也会拒绝）。
class SessionContext:
    def __init__(self, token: str, operator: str, identity: OSIdentity):
        self.token = token
        self.identity = identity
        # 使用人姓名是审计留痕的主字段，由现场人员明确输入；OS 账户仅作为
        # 第二道来源核验字段写入同一条日志，不能替代业务上的责任人署名。
        self.operator = operator
        self.project: AuditProject | None = None
        self.project_key = ""
        self.last_seen = time.monotonic()
        self.archive_preflights: dict[str, dict] = {}
        self.merge_preflights: dict[str, dict] = {}
        # 项目租约被其他会话接管（体验优化：强制切换而非拒绝打开）；
        # 心跳响应携带一次 project_preempted，前端据此提示并返回项目列表。
        self.preempted = False


_sessions: dict[str, SessionContext] = {}
_project_leases: dict[str, str] = {}
_project_leases_lock = threading.RLock()
PROJECT_LEASE_SECONDS = 45
SESSION_TTL_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 20


def _expire_sessions(now: float | None = None) -> int:
    """回收过期或超出容量的浏览器会话，并释放其项目连接与单写租约。"""
    current = time.monotonic() if now is None else now
    stale = [ctx for ctx in _sessions.values() if current - ctx.last_seen >= SESSION_TTL_SECONDS]
    survivors = [ctx for ctx in _sessions.values() if ctx not in stale]
    overflow = max(0, len(survivors) - MAX_SESSIONS)
    if overflow:
        stale.extend(sorted(survivors, key=lambda ctx: ctx.last_seen)[:overflow])
    for ctx in stale:
        # 后台任务仍使用此项目连接时不能直接 close；保留会话到任务结束，再由下一次
        # 清理回收，避免扫描/备份线程访问已关闭数据库。
        if ctx.project is not None and job_runner.has_active(ctx.project):
            ctx.last_seen = current
            continue
        if _sessions.pop(ctx.token, None) is not None:
            _close_session_project(ctx)
    return len(stale)

# 当前请求的会话上下文（HTTP 中间件设置，请求上下文链内传播）
_ctx_var: "ContextVar[SessionContext | None]" = ContextVar("audit_ctx", default=None)


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


@app.post("/api/session")
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


@app.get("/api/session")
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


@app.delete("/api/session")
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
    with _project_leases_lock:
        owner = _project_leases.get(key)
        if owner and owner != ctx.token:
            owner_ctx = _sessions.get(owner)
            if owner_ctx is not None:
                # 吊销旧会话项目连接；其后续写请求会因项目未打开而得到明确错误
                try:
                    _close_session_project(owner_ctx, preserve_key=key)
                except Exception as exc:
                    log_runtime_event(
                        "warning", "lease_preempt_close_failed",
                        message="接管项目租约时关闭旧会话项目失败",
                        error_type=type(exc).__name__, detail=str(exc),
                    )
                owner_ctx.preempted = True
        _project_leases[key] = ctx.token


def _release_project_lease(ctx: SessionContext, key: str = "") -> None:
    target = key or ctx.project_key
    if not target:
        return
    with _project_leases_lock:
        if _project_leases.get(target) == ctx.token:
            _project_leases.pop(target, None)


# ───────────────────────── 项目 ─────────────────────────

@app.post("/api/project/open")
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


@app.post("/api/project/create")
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
    except (OSError, sqlite3.Error, ValueError):
        if ctx.project_key != key:
            _release_project_lease(ctx, key)
        raise
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


@app.post("/api/project/delete")
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


@app.post("/api/project/reset")
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


@app.get("/api/project/current")
def current_project(_: str = Depends(get_operator)):
    return _project_info()


@app.post("/api/project/rename")
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


@app.get("/api/recent")
def recent_projects(operator: str = Depends(get_operator)):
    """最近项目列表（按使用人隔离，存本机 ~/.shenji，与端口/浏览器无关）。"""
    return {"items": load_recent_all().get(operator, [])}


@app.delete("/api/recent")
def forget_recent_project(path: str, operator: str = Depends(get_operator)):
    """从最近列表移除一条记录（不移除磁盘项目）。"""
    forget_recent(operator, path)
    return {"ok": True}


@app.get("/api/project/health")
def project_health(sample_size: int = 20, _: str = Depends(get_operator)):
    """项目健康检查：数据完整性 + 附件物理一致性。

    sample_size: 哈希抽查数量（<=0 = 全量）。检查结果含 counts 与 problems 明细。
    """
    proj = get_project()
    return proj.health_check(sample_size=sample_size)


@app.post("/api/project/scan")
def start_scan(_: str = Depends(get_operator)):
    """启动附件完整性扫描，任务和进度持久化到项目 SQLite。"""
    proj = get_project()
    job = proj.create_job("health_scan", {"sample_size": 0})

    def run_scan(ctx: JobContext) -> dict:
        def progress(done: int, total: int, phase: str) -> None:
            ctx.progress(done, total, phase)
            ctx.cancelled()  # 将数据库中的取消请求同步到 health_check 的安全检查点

        result = proj.health_check(sample_size=0, progress=progress, cancel_event=ctx.cancel_event)
        return result

    job_runner.submit(proj, job["id"], run_scan)
    return {"scan_id": job["id"]}


@app.get("/api/project/scan/{scan_id}")
def scan_status(scan_id: str, _: str = Depends(get_operator)):
    """轮询扫描进度/结果；服务重启后仍可读取历史任务。"""
    ctx = _ctx_var.get()
    if ctx is None or ctx.project is None:
        raise HTTPException(status_code=404, detail="扫描任务不存在")
    st = ctx.project.get_job(scan_id)
    if not st or st["type"] != "health_scan":
        raise HTTPException(status_code=404, detail="扫描任务不存在")
    progress = st.get("progress") or {}
    result = st.get("result") or {}
    return {
        "scan_id": st["id"],
        "status": st["status"],
        "phase": progress.get("phase", "db"),
        "done": progress.get("done", 0),
        "total": progress.get("total", 0),
        "problems": result.get("problems", []),
        "counts": result.get("counts", {}),
        "sample": result.get("sample", {"checked": 0, "total": 0}),
        "error": st.get("error", ""),
    }


@app.post("/api/project/scan/{scan_id}/cancel")
def cancel_scan(scan_id: str, _: str = Depends(get_operator)):
    """请求取消扫描；运行中的任务将在下一个安全检查点停止。"""
    st = job_runner.cancel(get_project(), scan_id)
    if not st or st["type"] != "health_scan":
        raise HTTPException(status_code=404, detail="扫描任务不存在")
    return {"ok": True, "status": st["status"]}


@app.get("/api/project/manifest")
def project_manifest(_: str = Depends(get_operator)):
    """生成/刷新项目清单 manifest.json，返回清单内容。"""
    return get_project().write_manifest()


@app.get("/api/project/summary")
def project_summary(_: str = Depends(get_operator)):
    """三维汇总（T8）：按状态/版块/单位 + 问题明细，数量与明细一致。"""
    return get_project().summary()


@app.get("/api/search")
def global_search(q: str = "", _: str = Depends(get_operator)):
    """全局搜索：单位/底稿/附件按关键字模糊匹配（各类限 20 条）。"""
    return get_project().search(q)


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


# ───────────────────────── 单位 ─────────────────────────

@app.get("/api/units")
def list_units(_: str = Depends(get_operator)):
    return get_project().list_units()


@app.post("/api/units")
def add_unit(req: NameReq, operator: str = Depends(get_operator)):
    try:
        uid = get_project().add_unit(req.name, operator)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": uid}


@app.patch("/api/units/{unit_id}")
def rename_unit(unit_id: int, req: NameReq, operator: str = Depends(get_operator)):
    try:
        get_project().rename_unit(unit_id, req.name, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/api/units/{unit_id}")
def delete_unit(unit_id: int, operator: str = Depends(get_operator)):
    try:
        proj = get_project()
        _require_project_idle(proj)
        proj.delete_unit(unit_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        # 跨单位引用保护：附件正被其他单位底稿引用
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# ───────────────────────── 底稿 ─────────────────────────

@app.get("/api/units/{unit_id}/issues")
def list_issues(unit_id: int, _: str = Depends(get_operator)):
    return get_project().list_issues(unit_id)


@app.post("/api/units/{unit_id}/issues")
def add_issue(unit_id: int, req: IssueReq, operator: str = Depends(get_operator)):
    try:
        iid = get_project().add_issue(unit_id, operator, **req.model_dump())
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=404 if isinstance(e, KeyError) else 400, detail=str(e))
    return {"id": iid}


@app.get("/api/issues/tree")
def issue_tree(_: str = Depends(get_operator)):
    """V3 问题树：全项目底稿按单位分组，一次请求返回，避免前端 N+1 查询。"""
    return get_project().list_issues_by_unit()


@app.get("/api/issues/{issue_id}")
def get_issue(issue_id: int, _: str = Depends(get_operator)):
    iss = get_project().get_issue(issue_id)
    if not iss:
        raise HTTPException(status_code=404, detail="底稿不存在")
    return iss


@app.patch("/api/issues/{issue_id}")
def update_issue(issue_id: int, req: IssueReq, operator: str = Depends(get_operator)):
    try:
        # 只更新显式提交的字段：未提交字段不写入（审查 F-02 修复）
        changed = get_project().update_issue(issue_id, operator,
                                             **req.model_dump(exclude_unset=True))
    except (KeyError, ValueError) as e:
        status_code = 404 if isinstance(e, KeyError) else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    return {"changed": changed, "issue": get_project().get_issue(issue_id)}


@app.post("/api/issues/{issue_id}/status")
def change_issue_status(issue_id: int, req: StatusReq, operator: str = Depends(get_operator)):
    """状态流转（T3）：矩阵校验 + 必填规则 + 留痕。非法迁移 400 且提示可走路径。"""
    try:
        get_project().change_status(issue_id, req.status, operator, req.comment)
    except (KeyError, ValueError) as e:
        status_code = 404 if isinstance(e, KeyError) else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    return get_project().get_issue(issue_id)


# ───────────────────────── 问题交流（P1-14） ─────────────────────────

@app.post("/api/issues/{issue_id}/exchange")
def start_issue_exchange(issue_id: int, operator: str = Depends(get_operator)):
    """开始交流修订；正式底稿在此期间保持只读。"""
    try:
        return get_project().start_exchange_session(issue_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/exchanges/{session_uuid}")
def get_issue_exchange(session_uuid: str, _: str = Depends(get_operator)):
    try:
        return get_project().get_exchange_session(session_uuid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/exchanges/{session_uuid}/revisions")
def propose_exchange_revision(session_uuid: str, req: ExchangeRevisionReq,
                              operator: str = Depends(get_operator)):
    try:
        return get_project().propose_exchange_revision(
            session_uuid, req.field_name, req.new_value, req.reason, operator,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/exchanges/{session_uuid}/revisions/{revision_uuid}/decision")
def decide_exchange_revision(session_uuid: str, revision_uuid: str,
                             req: ExchangeRevisionDecisionReq, operator: str = Depends(get_operator)):
    try:
        return get_project().decide_exchange_revision(session_uuid, revision_uuid, req.decision, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/exchanges/{session_uuid}/comments")
def add_exchange_comment(session_uuid: str, req: ExchangeCommentReq,
                         operator: str = Depends(get_operator)):
    try:
        return get_project().add_exchange_comment(
            session_uuid, req.body, req.anchor_field, req.revision_uuid, operator,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/exchanges/{session_uuid}/requests")
def create_exchange_request(session_uuid: str, req: ExchangeRequestReq,
                            operator: str = Depends(get_operator)):
    try:
        return get_project().create_exchange_request(session_uuid, req.content, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.patch("/api/exchanges/{session_uuid}/requests/{request_uuid}")
def update_exchange_request(session_uuid: str, request_uuid: str, req: ExchangeRequestUpdateReq,
                            operator: str = Depends(get_operator)):
    try:
        return get_project().update_exchange_request(
            session_uuid, request_uuid, req.status, req.provided_file_id, req.note, operator,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/exchanges/{session_uuid}/apply")
def apply_exchange_revisions(session_uuid: str, operator: str = Depends(get_operator)):
    try:
        return get_project().apply_exchange_revisions(session_uuid, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/exchanges/{session_uuid}/close")
def close_issue_exchange(session_uuid: str, req: ExchangeCloseReq,
                         operator: str = Depends(get_operator)):
    try:
        return get_project().close_exchange_session(session_uuid, req.note, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.delete("/api/issues/{issue_id}")
def delete_issue(issue_id: int, operator: str = Depends(get_operator)):
    try:
        proj = get_project()
        _require_project_idle(proj)
        proj.delete_issue(issue_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@app.get("/api/recycle/issues")
def list_recycled_issues(_: str = Depends(get_operator)):
    """底稿回收站：默认永不自动清空。"""
    return get_project().list_recycled_issues()


@app.get("/api/recycle/issues/{recycle_id}")
def get_recycled_issue_detail(recycle_id: int, _: str = Depends(get_operator)):
    """只读查看已移入回收站的底稿，便于确认后恢复或物理删除。"""
    try:
        return get_project().get_recycled_issue_detail(recycle_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/recycle/issues/{recycle_id}/restore")
def restore_recycled_issue(recycle_id: int, operator: str = Depends(get_operator)):
    try:
        proj = get_project()
        _require_project_idle(proj)
        return proj.restore_recycled_issue(recycle_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/recycle/issues/{recycle_id}")
def purge_recycled_issue(recycle_id: int, operator: str = Depends(get_operator)):
    """物理清空单条底稿，仅用户在回收站内明确操作时调用。"""
    try:
        proj = get_project()
        _require_project_idle(proj)
        proj.purge_recycled_issue(recycle_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@app.get("/api/recycle/units")
def list_recycled_units(_: str = Depends(get_operator)):
    return get_project().list_recycled_units()


@app.post("/api/recycle/units/{recycle_id}/restore")
def restore_recycled_unit(recycle_id: int, operator: str = Depends(get_operator)):
    try:
        proj = get_project()
        _require_project_idle(proj)
        return proj.restore_recycled_unit(recycle_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/recycle/units/{recycle_id}")
def purge_recycled_unit(recycle_id: int, operator: str = Depends(get_operator)):
    try:
        proj = get_project()
        _require_project_idle(proj)
        proj.purge_recycled_unit(recycle_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.get("/api/recycle/files")
def list_recycled_files(_: str = Depends(get_operator)):
    return get_project().list_recycled_files()


@app.post("/api/recycle/files/{recycle_id}/restore")
def restore_recycled_file(recycle_id: int, operator: str = Depends(get_operator)):
    try:
        proj = get_project()
        _require_project_idle(proj)
        return proj.restore_recycled_file(recycle_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/recycle/files/{recycle_id}")
def purge_recycled_file(recycle_id: int, operator: str = Depends(get_operator)):
    try:
        proj = get_project()
        _require_project_idle(proj)
        proj.purge_recycled_file(recycle_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# ───────────────────────── 版本历史 ─────────────────────────

@app.get("/api/issues/{issue_id}/versions")
def list_versions(issue_id: int, _: str = Depends(get_operator)):
    if not get_project().get_issue(issue_id):
        raise HTTPException(status_code=404, detail="底稿不存在或已移入回收站")
    return get_project().list_versions(issue_id)


@app.post("/api/issues/{issue_id}/versions/{version_id}/restore")
def restore_version(issue_id: int, version_id: int, operator: str = Depends(get_operator)):
    try:
        get_project().restore_version(issue_id, version_id, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=404 if isinstance(e, KeyError) else 400, detail=str(e))
    return {"ok": True}


# ───────────────────────── 附件 ─────────────────────────

@app.get("/api/units/{unit_id}/files")
def list_files(unit_id: int, _: str = Depends(get_operator)):
    return get_project().list_files(unit_id)


@app.get("/api/units/{unit_id}/files/unlinked")
def unlinked_files(unit_id: int, _: str = Depends(get_operator)):
    return get_project().unlinked_files(unit_id)


@app.api_route("/api/units/{unit_id}/attachments/open", methods=["GET", "POST"])
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


@app.api_route("/api/files/{file_id}/directory/open", methods=["GET", "POST"])
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


@app.post("/api/units/{unit_id}/folder-upload")
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


@app.post("/api/units/{unit_id}/files")
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


@app.get("/api/files/{file_id}/download")
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


@app.post("/api/files/{file_id}/open")
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


@app.patch("/api/files/{file_id}")
def rename_file(file_id: int, req: NameReq, operator: str = Depends(get_operator)):
    try:
        get_project().rename_file(file_id, req.name, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.post("/api/files/batch-rename")
def batch_rename_files(req: BatchRenameReq, operator: str = Depends(get_operator)):
    """批量重命名附件：事务内冲突检测，冲突条目跳过并返回原因（审查 F-06 补齐）。"""
    return get_project().batch_rename_files(
        [{"id": r.id, "name": r.name} for r in req.renames], operator)


@app.post("/api/files/{file_id}/move")
def move_file(file_id: int, req: MoveFileReq, operator: str = Depends(get_operator)):
    """移动附件到其他单位：物理移动 + 事务更新归属（审查 F-06 补齐）。"""
    try:
        return get_project().move_file_to_unit(file_id, req.unit_id, operator)
    except (KeyError, ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/files/{file_id}")
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


@app.get("/api/files/{file_id}/issues")
def issues_for_file(file_id: int, _: str = Depends(get_operator)):
    """反查：附件被哪些底稿引用。"""
    return get_project().issues_for_file(file_id)


# ───────────────────────── 底稿↔附件 关联 ─────────────────────────

@app.get("/api/issues/{issue_id}/files")
def files_for_issue(issue_id: int, _: str = Depends(get_operator)):
    if not get_project().get_issue(issue_id):
        raise HTTPException(status_code=404, detail="底稿不存在或已移入回收站")
    return get_project().files_for_issue(issue_id)


@app.post("/api/issues/{issue_id}/files/{file_id}/link")
def link_file(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
    try:
        get_project().link_file(issue_id, file_id, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/api/issues/{issue_id}/files/{file_id}/link")
def unlink_file(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
    try:
        get_project().unlink_file(issue_id, file_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@app.post("/api/issues/{issue_id}/files/{file_id}/link-exclusive")
def link_exclusive(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
    """仅关联到当前问题（独占）：附件移出资料库，其他底稿不可见。"""
    try:
        get_project().link_file_exclusive(issue_id, file_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@app.post("/api/files/{file_id}/shared")
def clear_exclusive(file_id: int, operator: str = Depends(get_operator)):
    """恢复共享：附件回到资料库，其他底稿可继续使用。"""
    try:
        get_project().clear_file_exclusive(file_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


# ───────────────────────── 操作日志 ─────────────────────────

@app.get("/api/logs")
def list_logs(limit: int = 500, _: str = Depends(get_operator)):
    return get_project().list_logs(max(1, min(limit, 5000)))


# ───────────────────────── 版块 / 问题分类预设 ─────────────────────────

@app.get("/api/settings/departments")
def get_departments(_: str = Depends(get_operator)):
    """读取项目版块预设（存 meta，随项目走）。"""
    proj = get_project()
    raw = proj.get_meta("departments", "[]")
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


@app.post("/api/settings/departments")
def set_departments(req: DeptReq, operator: str = Depends(get_operator)):
    """保存项目版块预设。"""
    proj = get_project()
    depts = [d.strip() for d in req.departments if d.strip()]
    # 去重保序
    seen = set()
    uniq = [d for d in depts if not (d in seen or seen.add(d))]
    proj.set_meta_with_log(
        "departments", json.dumps(uniq, ensure_ascii=False), operator,
        "更新版块预设", f"{len(uniq)} 个版块：{'、'.join(uniq[:5])}",
    )
    return uniq


@app.get("/api/settings/issue-number")
def get_issue_number(_: str = Depends(get_operator)):
    """读取底稿编号规则（前缀/后缀，默认空 = 纯数字序号）。"""
    proj = get_project()
    return {
        "prefix": proj.get_meta("issue_number_prefix", ""),
        "suffix": proj.get_meta("issue_number_suffix", ""),
    }


@app.post("/api/settings/issue-number")
def set_issue_number(req: IssueNumberReq, operator: str = Depends(get_operator)):
    """保存底稿编号规则：编号 = 前缀 + 数字序号 + 后缀。

    规则写入 meta（数据层），树/详情/导出台账/归档打包统一经 issue_no()
    计算当前展示编号；永久关联使用 issue_uuid。前后缀变更不追溯，
    删除后的数字可复用；默认前后缀为空 = 纯数字序号。
    """
    proj = get_project()
    return proj.save_issue_number_rule(operator, req.prefix.strip(), req.suffix.strip())


@app.get("/api/settings/categories")
def get_categories(_: str = Depends(get_operator)):
    """读取项目问题分类预设（存 meta，随项目走）。"""
    raw = get_project().get_meta("categories", "[]")
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


@app.api_route("/api/settings/categories", methods=["POST", "PUT"])
def set_categories(req: CategoryReq, operator: str = Depends(get_operator)):
    """保存项目问题分类预设。"""
    proj = get_project()
    categories = [item.strip() for item in req.categories if item.strip()]
    seen = set()
    unique = [item for item in categories if not (item in seen or seen.add(item))]
    proj.set_meta_with_log(
        "categories", json.dumps(unique, ensure_ascii=False), operator,
        "更新问题分类预设", f"{len(unique)} 个分类：{'、'.join(unique[:5])}",
    )
    return unique


@app.get("/api/settings/amount")
def get_amount_settings(_: str = Depends(get_operator)):
    return get_project().get_amount_settings()


@app.api_route("/api/settings/amount", methods=["POST", "PUT"])
def save_amount_settings(req: AmountSettingsReq, operator: str = Depends(get_operator)):
    try:
        return get_project().save_amount_settings(
            operator, currency=req.currency, amount_unit=req.amount_unit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ───────────────────────── 导入问题汇总 ─────────────────────────

@app.get("/api/import/template")
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


@app.post("/api/import/excel")
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


@app.post("/api/import/merge")
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


@app.post("/api/import/merge-local")
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


@app.post("/api/import/merge-local/preflight")
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

@app.post("/api/export/excel")
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


@app.post("/api/export/package")
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


@app.post("/api/export/package/preflight")
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


@app.post("/api/backup/create")
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


@app.get("/api/backup/settings")
def get_backup_settings(_: str = Depends(get_operator)):
    return get_project().get_backup_settings()


@app.post("/api/backup/settings")
def save_backup_settings(req: BackupSettingsReq, operator: str = Depends(get_operator)):
    try:
        return get_project().save_backup_settings(
            operator, enabled=req.enabled, target_dir=req.target_dir,
            interval_minutes=req.interval_minutes, retention_days=req.retention_days,
            max_bytes=req.max_bytes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/backup/recovery-point")
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


@app.get("/api/backup/recovery-points")
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


@app.post("/api/backup/recovery-points/restore")
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


@app.get("/api/backup/download/{filename}")
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


@app.post("/api/backup/restore")
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


@app.post("/api/backup/restore-local")
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


@app.get("/api/export/file/{filename}")
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

@app.post("/api/system/restart")
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


@app.post("/api/system/quit")
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


@app.post("/api/system/choose-folder")
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


@app.post("/api/system/open-folder")
def open_folder(req: FolderReq, _: str = Depends(get_operator)):
    """在系统文件管理器中打开指定文件夹（平台适配层 open_path）。"""
    try:
        open_path(req.path)
    except PlatformError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


# ───────────────────────── 静态资源（必须最后挂载） ─────────────────────────

FRONTEND_DIR.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


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
