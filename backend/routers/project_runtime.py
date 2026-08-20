"""项目运行期查询、完整性扫描和项目数据概览接口。"""

from collections.abc import Callable

from api.dependencies import SessionContext
from database import SCHEMA_VERSION, AuditProject
from fastapi import APIRouter, Depends, HTTPException
from jobs import JobContext


def build_router(
    get_project: Callable[..., AuditProject],
    get_operator: Callable[..., str],
    get_context: Callable[[], SessionContext | None],
    submit_job: Callable[[AuditProject, str, Callable[[JobContext], dict]], None],
    cancel_job: Callable[[AuditProject, str], dict | None],
) -> APIRouter:
    """保持扫描任务持久化与项目级查询的 URL、响应结构不变。"""
    router = APIRouter()

    @router.get("/api/meta")
    def runtime_meta(_: str = Depends(get_operator)):
        """返回当前离线工作台的 schema 与可用能力，前端不得从错误文本推断功能。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "capabilities": {
                "draft_recovery": True,
                "review_notes": True,
                "unit_ordering": True,
                "issue_ordering": True,
                "rich_text_editor": False,
                "project_material_requests": False,
            },
        }

    @router.get("/api/project/health")
    def project_health(sample_size: int = 20, _: str = Depends(get_operator)):
        """项目健康检查：数据完整性 + 附件物理一致性。

        sample_size: 哈希抽查数量（<=0 = 全量）。检查结果含 counts 与 problems 明细。
        """
        return get_project().health_check(sample_size=sample_size)

    @router.post("/api/project/scan")
    def start_scan(_: str = Depends(get_operator)):
        """启动附件完整性扫描，任务和进度持久化到项目 SQLite。"""
        project = get_project()
        job = project.create_job("health_scan", {"sample_size": 0})

        def run_scan(context: JobContext) -> dict:
            def progress(done: int, total: int, phase: str) -> None:
                context.progress(done, total, phase)
                context.cancelled()

            return project.health_check(sample_size=0, progress=progress, cancel_event=context.cancel_event)

        submit_job(project, job["id"], run_scan)
        return {"scan_id": job["id"]}

    @router.get("/api/project/scan/{scan_id}")
    def scan_status(scan_id: str, _: str = Depends(get_operator)):
        """轮询扫描进度/结果；服务重启后仍可读取历史任务。"""
        context = get_context()
        if context is None or context.project is None:
            raise HTTPException(status_code=404, detail="扫描任务不存在")
        status = context.project.get_job(scan_id)
        if not status or status["type"] != "health_scan":
            raise HTTPException(status_code=404, detail="扫描任务不存在")
        progress = status.get("progress") or {}
        result = status.get("result") or {}
        return {
            "scan_id": status["id"], "status": status["status"],
            "phase": progress.get("phase", "db"), "done": progress.get("done", 0),
            "total": progress.get("total", 0), "problems": result.get("problems", []),
            "counts": result.get("counts", {}),
            "sample": result.get("sample", {"checked": 0, "total": 0}),
            "error": status.get("error", ""),
        }

    @router.post("/api/project/scan/{scan_id}/cancel")
    def cancel_scan(scan_id: str, _: str = Depends(get_operator)):
        """请求取消扫描；运行中的任务将在下一个安全检查点停止。"""
        status = cancel_job(get_project(), scan_id)
        if not status or status["type"] != "health_scan":
            raise HTTPException(status_code=404, detail="扫描任务不存在")
        return {"ok": True, "status": status["status"]}

    @router.get("/api/project/manifest")
    def project_manifest(_: str = Depends(get_operator)):
        """生成/刷新项目清单 manifest.json，返回清单内容。"""
        return get_project().write_manifest()

    @router.get("/api/project/summary")
    def project_summary(_: str = Depends(get_operator)):
        """项目汇总：底稿明细、分布统计与轻量项目数据概览。"""
        return get_project().summary()

    @router.get("/api/search")
    def global_search(q: str = "", _: str = Depends(get_operator)):
        """全局搜索：单位/底稿/附件按关键字模糊匹配（各类限 20 条）。"""
        return get_project().search(q)

    return router
