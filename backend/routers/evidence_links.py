"""附件列表与底稿-附件关联接口。"""

from collections.abc import Callable

from database import AuditProject
from fastapi import APIRouter, Depends, HTTPException


def build_router(get_project: Callable[..., AuditProject], get_operator: Callable[..., str]) -> APIRouter:
    """保持附件归属、独占和关联关系的既有数据层语义。"""
    router = APIRouter()

    @router.get("/api/units/{unit_id}/files")
    def list_files(unit_id: int, _: str = Depends(get_operator)):
        return get_project().list_files(unit_id)

    @router.get("/api/units/{unit_id}/files/unlinked")
    def unlinked_files(unit_id: int, _: str = Depends(get_operator)):
        return get_project().unlinked_files(unit_id)

    @router.get("/api/files/{file_id}/issues")
    def issues_for_file(file_id: int, _: str = Depends(get_operator)):
        """反查：附件被哪些底稿引用。"""
        return get_project().issues_for_file(file_id)

    @router.get("/api/issues/{issue_id}/files")
    def files_for_issue(issue_id: int, _: str = Depends(get_operator)):
        if not get_project().get_issue(issue_id):
            raise HTTPException(status_code=404, detail="底稿不存在或已移入回收站")
        return get_project().files_for_issue(issue_id)

    @router.post("/api/issues/{issue_id}/files/{file_id}/link")
    def link_file(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
        try:
            get_project().link_file(issue_id, file_id, operator)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @router.delete("/api/issues/{issue_id}/files/{file_id}/link")
    def unlink_file(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
        try:
            get_project().unlink_file(issue_id, file_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @router.post("/api/issues/{issue_id}/files/{file_id}/link-exclusive")
    def link_exclusive(issue_id: int, file_id: int, operator: str = Depends(get_operator)):
        """仅关联到当前问题（独占）：附件移出资料库，其他底稿不可见。"""
        try:
            get_project().link_file_exclusive(issue_id, file_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @router.post("/api/files/{file_id}/shared")
    def clear_exclusive(file_id: int, operator: str = Depends(get_operator)):
        """恢复共享：附件回到资料库，其他底稿可继续使用。"""
        try:
            get_project().clear_file_exclusive(file_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    return router
