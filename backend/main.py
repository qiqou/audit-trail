"""FastAPI 入口 — 本地服务 + 静态托管 + 启动即开浏览器。

设计：
- 所有接口强制使用人（X-Operator 请求头），缺失即拒绝 —— 与前端启动弹窗双保险
- 项目按会话隔离：每个会话 token 独立持有自己的项目（审查 F-03 修复），
  不同浏览器会话可各自打开不同项目，互不干扰
- 查看类接口不写日志，变更类接口由数据层自动留痕
- 前端静态资源在 frontend-v3/dist，根路径挂载。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

# 兼容两种启动方式：`python backend/main.py` 与 `uvicorn main:app`（根目录转发）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import PROJECT_EXT, RuntimeSettings
from database import OUT_DIR, AuditProject
from jobs import JobContext, job_runner
from platform_adapter import (
    PlatformError,
    acquire_single_instance,
    discover_instance_endpoint,
    harden_project,
    open_browser,
    open_path,
    release_single_instance,
    reserve_local_port,
    write_instance_endpoint,
)
from platform_adapter import (
    choose_folder as platform_choose_folder,
)

SETTINGS = RuntimeSettings.from_environment()
HOST = SETTINGS.host
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
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

app = FastAPI(title="审迹", docs_url="/api/docs", openapi_url="/api/openapi.json")

# 会话表：token → 会话上下文（使用人 + 该会话打开的项目）。
# 每个会话独立持有项目，互不干扰（审查 F-03 修复：项目不再全局共享）。
# 启动弹窗登录换取 token（HTTP header 只传 ASCII 安全值，中文直接放 header
# 会被 Latin-1 解码成乱码，浏览器 fetch 也会拒绝）。
class SessionContext:
    def __init__(self, operator: str):
        self.operator = operator
        self.project: AuditProject | None = None


_sessions: dict[str, SessionContext] = {}

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


# ───────────────────────── 依赖 ─────────────────────────

class OperatorReq(BaseModel):
    operator: str


class FolderReq(BaseModel):
    path: str


class ExportReq(BaseModel):
    scope: str = "project"   # unit / project
    unit_id: int = None


class PackageReq(BaseModel):
    scope: str = "all"            # all / selected
    unit_ids: list[int] = Field(default_factory=list)
    group_by_dept: bool = False   # 是否按版块分类（三级目录）


class DeptReq(BaseModel):
    departments: list[str] = Field(default_factory=list)


class CategoryReq(BaseModel):
    categories: list[str] = Field(default_factory=list)


@app.post("/api/session")
def login(req: OperatorReq):
    """登录：输入使用人，换取会话 token。不登录无法调用任何业务接口。"""
    op = req.operator.strip()
    if not op:
        raise HTTPException(status_code=400, detail="使用人不能为空")
    token = uuid.uuid4().hex
    _sessions[token] = SessionContext(op)
    return {"token": token, "operator": op}


def get_operator(x_session: str = Header(default="")) -> str:
    """强制使用人：会话 token 缺失或无效直接拒绝；同时把会话上下文挂到当前请求。"""
    ctx = _sessions.get(x_session.strip())
    if not ctx:
        raise HTTPException(status_code=400, detail="使用人会话无效，请重新启动程序并输入使用人")
    _ctx_var.set(ctx)
    return ctx.operator


@app.get("/api/session")
def current_session(operator: str = Depends(get_operator)):
    """校验浏览器保存的本地会话；服务重启后前端据此重新要求输入使用人。"""
    return {"operator": operator}


@app.delete("/api/session")
def logout(x_session: str = Header(default="")):
    """显式释放当前会话及其数据库连接，避免频繁切换使用人造成资源累积。"""
    ctx = _sessions.pop(x_session.strip(), None)
    if ctx is None:
        raise HTTPException(status_code=400, detail="使用人会话无效，请重新进入工作台")
    if ctx.project is not None:
        ctx.project.close()
        ctx.project = None
    return {"ok": True}


def get_project() -> AuditProject:
    """当前会话的项目（审查 F-03 修复：从会话上下文取，不再全局共享）。"""
    ctx = _ctx_var.get()
    if ctx is None or ctx.project is None:
        raise HTTPException(status_code=400, detail="请先打开或创建项目")
    return ctx.project


def _project_info() -> dict:
    proj = get_project()
    return {
        "path": str(proj.root),
        "project_name": proj.project_name,
        "units": proj.list_units(),
    }


# ───────────────────────── 请求模型 ─────────────────────────

class OpenReq(BaseModel):
    path: str


class CreateReq(BaseModel):
    path: str
    name: str = ""


class NameReq(BaseModel):
    name: str


class ResetReq(BaseModel):
    confirm_text: str


class IssueNumberReq(BaseModel):
    prefix: str = ""
    suffix: str = ""


class IssueReq(BaseModel):
    """底稿字段请求体。

    所有字段 Optional：新建（POST）时未传即空；更新（PATCH）时只更新
    显式提交的字段，绝不把未提交字段清空（审查 F-02 修复）。
    """
    department: str | None = None
    category: str | None = None
    defect_type: str | None = None
    defect_desc: str | None = None
    amount: str | None = None
    regulation_basis: str | None = None
    suggestion: str | None = None
    author: str | None = None
    reviewer: str | None = None
    status: str | None = None


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
    _close_current_project()
    ctx = _ctx_var.get()
    assert ctx is not None  # get_operator 依赖已确保会话存在
    try:
        ctx.project = AuditProject(p)
    except ValueError as e:
        # 版本兼容检查（T12）：更新版本创建的项目拒绝打开，给可执行提示
        raise HTTPException(status_code=400, detail=str(e))
    ctx.project.log(operator, "打开项目", str(p))
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
    _close_current_project()
    ctx = _ctx_var.get()
    assert ctx is not None  # get_operator 依赖已确保会话存在
    ctx.project = AuditProject(p)
    name = req.name.strip()
    if name:
        ctx.project.project_name = name
    harden_project(p)  # 隐藏目录：默认 Finder/资源管理器不可见，防人员误删改
    ctx.project.log(operator, "创建项目", name or p.name)
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
    # 删除的是当前会话打开的项目时，先关闭连接再删目录
    ctx = _ctx_var.get()
    if ctx is not None and ctx.project is not None and Path(ctx.project.root) == p:
        _close_current_project()
    try:
        shutil.rmtree(p)
    except OSError as e:
        raise HTTPException(status_code=400,
                            detail=f"删除失败：{e}。请关闭占用该项目的程序后重试") from e
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
    return _project_info()


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
    """三维汇总（T8）：按状态/版块/单位，数量与明细一致。"""
    return get_project().summary()


def _close_current_project():
    """关闭当前会话持有的项目（如有）。"""
    ctx = _ctx_var.get()
    if ctx is None:
        return
    proj = ctx.project
    if proj is not None:
        try:
            proj.close()
        except Exception:
            pass
    ctx.project = None


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
        get_project().delete_unit(unit_id, operator)
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
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
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


class StatusReq(BaseModel):
    """状态流转请求：status 必填；comment 在复核退回/归档后编辑时必填。"""
    status: str
    comment: str = ""


@app.post("/api/issues/{issue_id}/status")
def change_issue_status(issue_id: int, req: StatusReq, operator: str = Depends(get_operator)):
    """状态流转（T3）：矩阵校验 + 必填规则 + 留痕。非法迁移 400 且提示可走路径。"""
    try:
        get_project().change_status(issue_id, req.status, operator, req.comment)
    except (KeyError, ValueError) as e:
        status_code = 404 if isinstance(e, KeyError) else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    return get_project().get_issue(issue_id)


@app.delete("/api/issues/{issue_id}")
def delete_issue(issue_id: int, operator: str = Depends(get_operator)):
    try:
        get_project().delete_issue(issue_id, operator)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


# ───────────────────────── 版本历史 ─────────────────────────

@app.get("/api/issues/{issue_id}/versions")
def list_versions(issue_id: int, _: str = Depends(get_operator)):
    return get_project().list_versions(issue_id)


@app.post("/api/issues/{issue_id}/versions/{version_id}/restore")
def restore_version(issue_id: int, version_id: int, operator: str = Depends(get_operator)):
    try:
        get_project().restore_version(issue_id, version_id, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
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
                    tf.write(chunk)
                tmp_items.append((rel, tf.name))
        if not tmp_items:
            raise ValueError("文件夹为空")
        rec = await run_in_threadpool(proj.add_folder, unit_id, tmp_items, folder_name, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        for _rel, tmp in tmp_items:
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
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
        # 流式写入并计数，超限提前拒绝（审查 F-07 修复）
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
            tf.write(chunk)
        tmp_path = tf.name
    try:
        sha = await run_in_threadpool(_sha256_of, tmp_path)
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


@app.patch("/api/files/{file_id}")
def rename_file(file_id: int, req: NameReq, operator: str = Depends(get_operator)):
    try:
        get_project().rename_file(file_id, req.name, operator)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


class RenameItem(BaseModel):
    id: int
    name: str


class BatchRenameReq(BaseModel):
    renames: list[RenameItem]


@app.post("/api/files/batch-rename")
def batch_rename_files(req: BatchRenameReq, operator: str = Depends(get_operator)):
    """批量重命名附件：事务内冲突检测，冲突条目跳过并返回原因（审查 F-06 补齐）。"""
    return get_project().batch_rename_files(
        [{"id": r.id, "name": r.name} for r in req.renames], operator)


class MoveFileReq(BaseModel):
    unit_id: int


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
        get_project().remove_file(file_id, operator)
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
    except Exception:
        return []


@app.post("/api/settings/departments")
def set_departments(req: DeptReq, operator: str = Depends(get_operator)):
    """保存项目版块预设。"""
    proj = get_project()
    depts = [d.strip() for d in req.departments if d.strip()]
    # 去重保序
    seen = set()
    uniq = [d for d in depts if not (d in seen or seen.add(d))]
    proj.set_meta("departments", json.dumps(uniq, ensure_ascii=False))
    proj.log(operator, "更新版块预设", f"{len(uniq)} 个版块：{'、'.join(uniq[:5])}")
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
    计算，作为唯一识别码全程一致；默认前后缀为空 = 纯数字序号。
    """
    proj = get_project()
    proj.set_meta("issue_number_prefix", req.prefix.strip())
    proj.set_meta("issue_number_suffix", req.suffix.strip())
    proj.log(operator, "更新编号规则", f"前缀「{req.prefix}」后缀「{req.suffix}」")
    return {"prefix": req.prefix.strip(), "suffix": req.suffix.strip()}


@app.get("/api/settings/categories")
def get_categories(_: str = Depends(get_operator)):
    """读取项目问题分类预设（存 meta，随项目走）。"""
    raw = get_project().get_meta("categories", "[]")
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:
        return []


@app.api_route("/api/settings/categories", methods=["POST", "PUT"])
def set_categories(req: CategoryReq, operator: str = Depends(get_operator)):
    """保存项目问题分类预设。"""
    proj = get_project()
    categories = [item.strip() for item in req.categories if item.strip()]
    seen = set()
    unique = [item for item in categories if not (item in seen or seen.add(item))]
    proj.set_meta("categories", json.dumps(unique, ensure_ascii=False))
    proj.log(operator, "更新问题分类预设", f"{len(unique)} 个分类：{'、'.join(unique[:5])}")
    return unique


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
    """合并导入：审计经理汇总多个 .auditbak 备份到当前项目（单位/底稿/附件/版块预设）。"""
    from export import merge_backups
    from limits import MAX_BATCH_FILES, MAX_FILE_SIZE, human_size

    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400,
                            detail=f"单批最多合并 {MAX_BATCH_FILES} 个备份，当前 {len(files)} 个")
    proj = get_project()
    tmp_zips = []
    try:
        for f in files:
            if not (f.filename or "").lower().endswith(".auditbak"):
                raise ValueError(f"{f.filename} 不是备份文件（.auditbak）")
            size = 0
            with tempfile.NamedTemporaryFile(delete=False, suffix=".auditbak") as tf:
                while True:
                    chunk = await f.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        Path(tf.name).unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=400,
                            detail=f"备份文件「{f.filename}」超过上限 {human_size(MAX_FILE_SIZE)}")
                    tf.write(chunk)
                tmp_zips.append(tf.name)
        info = await run_in_threadpool(merge_backups, proj, tmp_zips, operator)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        for p in tmp_zips:
            Path(p).unlink(missing_ok=True)
    return info


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
    """按项目结构打包 ZIP（范围：全部/勾选单位；可按版块分类）。"""
    from export import package_project as do_package
    proj = get_project()
    try:
        info = do_package(proj, scope=req.scope, unit_ids=req.unit_ids,
                          group_by_dept=req.group_by_dept)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scope_name = "全部单位" if req.scope == "all" else f"勾选单位 {len(req.unit_ids)} 个"
    proj.log(operator, "打包ZIP", info["filename"],
             f"{scope_name}，{info['units']} 个单位、{info['issues']} 条底稿"
             + ("，按版块分类" if req.group_by_dept else ""))
    return {"filename": info["filename"], "abs_path": info["abs_path"],
            "units": info["units"], "issues": info["issues"],
            "download_url": f"/api/export/file/{quote(info['filename'])}"}


@app.post("/api/backup/create")
def create_backup(operator: str = Depends(get_operator)):
    """备份项目（audit.db + 附件库）到上级目录 .auditbak。"""
    from export import create_backup as do_backup
    proj = get_project()
    try:
        info = do_backup(proj)
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"创建备份失败：{e}") from e
    proj.log(operator, "备份", info["filename"], f"{info['db_size']} 字节")
    return {"filename": info["filename"], "abs_path": info["abs_path"],
            "download_url": f"/api/backup/download/{quote(info['filename'])}"}


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
        info = await run_in_threadpool(do_restore, tmp_bak, target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        Path(tmp_bak).unlink(missing_ok=True)
    # 恢复成功后在恢复的项目里留痕
    def log_restored_project() -> None:
        restored = AuditProject(info["path"])
        try:
            restored.log(operator, "恢复备份", info["path"])
        finally:
            restored.close()

    await run_in_threadpool(log_restored_project)
    return {"path": info["path"]}


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
        if ctx.project is not None:
            try:
                ctx.project.close()
            except Exception:
                pass
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
        subprocess.Popen(cmd, cwd=cwd, start_new_session=True)
    except Exception:
        pass
    finally:
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
        raise HTTPException(status_code=400, detail=str(e))


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
    """直接运行入口：单实例锁 → 预留动态端口 → 起服务 → 自动开浏览器。

    端口由操作系统分配（或由开发环境显式指定），并把已监听 socket 交给
    Uvicorn，避免固定 8000 端口及“检查后被占用”的竞态。
    """
    # Windows 打包版（console=False / windowed）没有控制台，sys.stdout/stderr 为 None，
    # uvicorn 的 ColourizedFormatter 初始化调 sys.stdout.isatty() → AttributeError 崩溃。
    # 兜底为 devnull 流：isatty()=False 关闭颜色输出，日志本就走崩溃文件/丢弃，不影响功能。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 替换全局流，句柄须存活到进程退出
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 替换全局流，句柄须存活到进程退出
    _install_crash_hook()
    lock_name = "shenji.lock"
    lock = acquire_single_instance(lock_name)
    if lock is None:
        endpoint = discover_instance_endpoint(lock_name)
        if endpoint:
            print(f"审迹已在运行，正在打开页面：{endpoint}")
            try:
                open_browser(endpoint)
            except PlatformError as e:
                print(str(e))
        else:
            print("审迹已在运行，但未找到页面地址。请稍后重试，或手动访问启动日志中的本地地址。")
        return
    listener = None
    try:
        try:
            listener, port = reserve_local_port(HOST, SETTINGS.port)
        except PlatformError as e:
            print(str(e))
            return
        print(f"审迹正在启动：http://{HOST}:{port}")
        write_instance_endpoint(HOST, port, lock_name)
        threading.Timer(1.0, _auto_open_browser, args=(port,)).start()
        import uvicorn

        if sys.platform == "win32":
            # uvicorn fd 模式在 Windows 上访问 socket.AF_UNIX（该常量 Windows 不存在）
            # → AttributeError 启动崩溃（CI Windows 实测）。改为 host+port 启动：
            # 端口已由 reserve_local_port 确定，close 后重新 bind 的竞态窗口极小。
            listener.close()
            listener = None
            uvicorn.run(app, host=HOST, port=port)
        else:
            uvicorn.run(app, fd=listener.fileno())
    finally:
        if listener is not None:
            listener.close()
        release_single_instance(lock)
        try:
            from platform_adapter import _endpoint_path
            _endpoint_path(lock_name).unlink(missing_ok=True)
        except OSError:
            pass


def _auto_open_browser(port: int):
    """延迟 1 秒自动开浏览器；失败不致命（打印提示，用户手动访问）。"""
    try:
        open_browser(f"http://{HOST}:{port}")
    except PlatformError as e:
        print(str(e))


if __name__ == "__main__":
    main()
