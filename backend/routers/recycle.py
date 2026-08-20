"""底稿、单位和附件的回收站接口。"""

from collections.abc import Callable

from database import AuditProject
from fastapi import APIRouter, Depends, HTTPException


def build_router(
    get_project: Callable[..., AuditProject],
    get_operator: Callable[..., str],
    require_project_idle: Callable[[AuditProject], None],
) -> APIRouter:
    """保留显式删除、恢复与物理清空的既有安全边界。"""
    router = APIRouter()

    @router.delete("/api/issues/{issue_id}")
    def delete_issue(issue_id: int, operator: str = Depends(get_operator)):
        try:
            project = get_project()
            require_project_idle(project)
            project.delete_issue(issue_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @router.get("/api/recycle/issues")
    def list_recycled_issues(_: str = Depends(get_operator)):
        """底稿回收站：默认永不自动清空。"""
        return get_project().list_recycled_issues()

    @router.get("/api/recycle/issues/{recycle_id}")
    def get_recycled_issue_detail(recycle_id: int, _: str = Depends(get_operator)):
        """只读查看已移入回收站的底稿，便于确认后恢复或物理删除。"""
        try:
            return get_project().get_recycled_issue_detail(recycle_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/recycle/issues/{recycle_id}/restore")
    def restore_recycled_issue(recycle_id: int, operator: str = Depends(get_operator)):
        try:
            project = get_project()
            require_project_idle(project)
            return project.restore_recycled_issue(recycle_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/recycle/issues/{recycle_id}")
    def purge_recycled_issue(recycle_id: int, operator: str = Depends(get_operator)):
        """物理清空单条底稿，仅用户在回收站内明确操作时调用。"""
        try:
            project = get_project()
            require_project_idle(project)
            project.purge_recycled_issue(recycle_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @router.get("/api/recycle/units")
    def list_recycled_units(_: str = Depends(get_operator)):
        return get_project().list_recycled_units()

    @router.post("/api/recycle/units/{recycle_id}/restore")
    def restore_recycled_unit(recycle_id: int, operator: str = Depends(get_operator)):
        try:
            project = get_project()
            require_project_idle(project)
            return project.restore_recycled_unit(recycle_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/recycle/units/{recycle_id}")
    def purge_recycled_unit(recycle_id: int, operator: str = Depends(get_operator)):
        try:
            project = get_project()
            require_project_idle(project)
            project.purge_recycled_unit(recycle_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @router.get("/api/recycle/files")
    def list_recycled_files(_: str = Depends(get_operator)):
        return get_project().list_recycled_files()

    @router.post("/api/recycle/files/{recycle_id}/restore")
    def restore_recycled_file(recycle_id: int, operator: str = Depends(get_operator)):
        try:
            project = get_project()
            require_project_idle(project)
            return project.restore_recycled_file(recycle_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/recycle/files/{recycle_id}")
    def purge_recycled_file(recycle_id: int, operator: str = Depends(get_operator)):
        try:
            project = get_project()
            require_project_idle(project)
            project.purge_recycled_file(recycle_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    return router
