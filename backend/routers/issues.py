"""底稿、版本与项目内模板接口。"""

import time
import uuid
from collections.abc import Callable

from api.dependencies import SessionContext
from api.errors import key_or_value_error
from api_models import (
    BatchIssueMetadataReq,
    DuplicateIssueReq,
    IssueReq,
    OrderReq,
    StatusReq,
    WorkpaperTemplateApplyReq,
    WorkpaperTemplateCreateReq,
)
from database import AuditProject
from fastapi import APIRouter, Depends, HTTPException


def build_router(
    get_project: Callable[..., AuditProject],
    get_operator: Callable[..., str],
    get_context: Callable[[], SessionContext | None],
) -> APIRouter:
    """保持 v1.2 URL、请求体、状态码与底稿版本语义不变。"""
    router = APIRouter()

    @router.get("/api/units/{unit_id}/issues")
    def list_issues(unit_id: int, _: str = Depends(get_operator)):
        return get_project().list_issues(unit_id)

    @router.post("/api/units/{unit_id}/issues")
    def add_issue(unit_id: int, req: IssueReq, operator: str = Depends(get_operator)):
        try:
            issue_id = get_project().add_issue(unit_id, operator, **req.model_dump())
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc
        return {"id": issue_id}

    @router.put("/api/units/{unit_id}/issues/order")
    def reorder_issues(unit_id: int, req: OrderReq, operator: str = Depends(get_operator)):
        """保存单位内底稿的完整拖放顺序；编号和版本链保持不变。"""
        try:
            changed = get_project().reorder_issues(unit_id, req.ids, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc
        return {"changed": changed}

    @router.get("/api/issues/tree")
    def issue_tree(_: str = Depends(get_operator)):
        """V3 问题树：全项目底稿按单位分组，一次请求返回，避免前端 N+1 查询。"""
        return get_project().list_issues_by_unit()

    @router.get("/api/issues/{issue_id}")
    def get_issue(issue_id: int, _: str = Depends(get_operator)):
        issue = get_project().get_issue(issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="底稿不存在")
        return issue

    @router.post("/api/issues/{issue_id}/duplicate")
    def duplicate_issue(issue_id: int, req: DuplicateIssueReq, operator: str = Depends(get_operator)):
        """从当前正文快速新建草稿；不复制附件、版本、状态或交流记录。"""
        try:
            return get_project().duplicate_issue(issue_id, operator, req.unit_id)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.get("/api/workpaper-templates")
    def list_workpaper_templates(_: str = Depends(get_operator)):
        return get_project().list_workpaper_templates()

    @router.post("/api/workpaper-templates")
    def create_workpaper_template(req: WorkpaperTemplateCreateReq, operator: str = Depends(get_operator)):
        try:
            return get_project().create_workpaper_template(req.name, req.issue_id, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.post("/api/workpaper-templates/{template_id}/apply")
    def apply_workpaper_template(template_id: int, req: WorkpaperTemplateApplyReq,
                                 operator: str = Depends(get_operator)):
        try:
            return get_project().create_issue_from_template(template_id, req.unit_id, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.delete("/api/workpaper-templates/{template_id}")
    def delete_workpaper_template(template_id: int, operator: str = Depends(get_operator)):
        try:
            get_project().delete_workpaper_template(template_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @router.patch("/api/issues/{issue_id}")
    def update_issue(issue_id: int, req: IssueReq, operator: str = Depends(get_operator)):
        try:
            changed = get_project().update_issue(issue_id, operator, **req.model_dump(exclude_unset=True))
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc
        return {"changed": changed, "issue": get_project().get_issue(issue_id)}

    @router.post("/api/issues/batch-metadata/preflight")
    def batch_issue_metadata_preflight(req: BatchIssueMetadataReq, _: str = Depends(get_operator)):
        """批量元数据只读预检：明确影响范围后生成一次性确认令牌。"""
        try:
            result = get_project().preflight_batch_issue_metadata(req.issue_ids, req.changes)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc
        context = get_context()
        assert context is not None
        token = uuid.uuid4().hex
        context.batch_issue_preflights[token] = {
            "project_uuid": get_project().project_uuid,
            "issue_ids": result["issue_ids"],
            "changes": result["changes"],
            "fingerprint": result.pop("fingerprint"),
            "expires_at": time.monotonic() + 10 * 60,
        }
        result["confirmation_token"] = token
        return result

    @router.post("/api/issues/batch-metadata")
    def batch_issue_metadata_update(req: BatchIssueMetadataReq, operator: str = Depends(get_operator)):
        """令牌、项目和底稿快照一致时，事务内批量维护白名单元数据。"""
        context = get_context()
        assert context is not None
        approved = context.batch_issue_preflights.pop(req.confirmation_token.strip(), None)
        if not approved or approved["expires_at"] < time.monotonic():
            raise HTTPException(status_code=409, detail="批量维护预检已失效，请重新预检")
        project = get_project()
        if (approved["project_uuid"] != project.project_uuid or approved["issue_ids"] != req.issue_ids
                or approved["changes"] != req.changes):
            raise HTTPException(status_code=409, detail="批量维护内容已变化，请重新预检")
        try:
            current = project.preflight_batch_issue_metadata(req.issue_ids, req.changes)
            if current["fingerprint"] != approved["fingerprint"]:
                raise HTTPException(status_code=409, detail="所选底稿已变化，请重新预检后再提交")
            return project.batch_update_issue_metadata(req.issue_ids, req.changes, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.post("/api/issues/{issue_id}/status")
    def change_issue_status(issue_id: int, req: StatusReq, operator: str = Depends(get_operator)):
        """状态流转（T3）：矩阵校验 + 必填规则 + 留痕。非法迁移 400 且提示可走路径。"""
        try:
            get_project().change_status(issue_id, req.status, operator, req.comment)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc
        return get_project().get_issue(issue_id)

    @router.get("/api/issues/{issue_id}/versions")
    def list_versions(issue_id: int, _: str = Depends(get_operator)):
        if not get_project().get_issue(issue_id):
            raise HTTPException(status_code=404, detail="底稿不存在或已移入回收站")
        return get_project().list_versions(issue_id)

    @router.post("/api/issues/{issue_id}/versions/{version_id}/restore")
    def restore_version(issue_id: int, version_id: int, operator: str = Depends(get_operator)):
        try:
            get_project().restore_version(issue_id, version_id, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc
        return {"ok": True}

    return router
