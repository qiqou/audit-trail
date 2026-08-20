"""项目级预设与金额口径接口。

路由通过工厂接收会话依赖，避免 router 反向导入应用入口造成循环依赖。
"""

import json
from collections.abc import Callable

from api_models import AmountSettingsReq, CategoryReq, DeptReq, IssueNumberReq
from database import AuditProject
from fastapi import APIRouter, Depends, HTTPException


def build_router(
    get_project: Callable[..., AuditProject],
    get_operator: Callable[..., str],
) -> APIRouter:
    """创建设置路由，并保持 v1.2 的 URL、请求体和响应契约不变。"""
    router = APIRouter()

    @router.get("/api/settings/departments")
    def get_departments(_: str = Depends(get_operator)):
        raw = get_project().get_meta("departments", "[]")
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    @router.post("/api/settings/departments")
    def set_departments(req: DeptReq, operator: str = Depends(get_operator)):
        proj = get_project()
        departments = [item.strip() for item in req.departments if item.strip()]
        seen = set()
        unique = [item for item in departments if not (item in seen or seen.add(item))]
        proj.set_meta_with_log(
            "departments", json.dumps(unique, ensure_ascii=False), operator,
            "更新版块预设", f"{len(unique)} 个版块：{'、'.join(unique[:5])}",
        )
        return unique

    @router.get("/api/settings/issue-number")
    def get_issue_number(_: str = Depends(get_operator)):
        proj = get_project()
        return {
            "prefix": proj.get_meta("issue_number_prefix", ""),
            "suffix": proj.get_meta("issue_number_suffix", ""),
        }

    @router.post("/api/settings/issue-number")
    def set_issue_number(req: IssueNumberReq, operator: str = Depends(get_operator)):
        return get_project().save_issue_number_rule(operator, req.prefix.strip(), req.suffix.strip())

    @router.get("/api/settings/categories")
    def get_categories(_: str = Depends(get_operator)):
        raw = get_project().get_meta("categories", "[]")
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    @router.post("/api/settings/categories", operation_id="set_categories_post")
    @router.put("/api/settings/categories", operation_id="set_categories_put")
    def set_categories(req: CategoryReq, operator: str = Depends(get_operator)):
        proj = get_project()
        categories = [item.strip() for item in req.categories if item.strip()]
        seen = set()
        unique = [item for item in categories if not (item in seen or seen.add(item))]
        proj.set_meta_with_log(
            "categories", json.dumps(unique, ensure_ascii=False), operator,
            "更新问题分类预设", f"{len(unique)} 个分类：{'、'.join(unique[:5])}",
        )
        return unique

    @router.get("/api/settings/amount")
    def get_amount_settings(_: str = Depends(get_operator)):
        return get_project().get_amount_settings()

    @router.post("/api/settings/amount", operation_id="save_amount_settings_post")
    @router.put("/api/settings/amount", operation_id="save_amount_settings_put")
    def save_amount_settings(req: AmountSettingsReq, operator: str = Depends(get_operator)):
        try:
            return get_project().save_amount_settings(
                operator, currency=req.currency, amount_unit=req.amount_unit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
