"""被审单位生命周期接口。"""

from collections.abc import Callable

from api_models import NameReq, OrderReq
from database import AuditProject
from fastapi import APIRouter, Depends, HTTPException


def build_router(
    get_project: Callable[..., AuditProject],
    get_operator: Callable[..., str],
    require_project_idle: Callable[[AuditProject], None],
) -> APIRouter:
    """创建单位路由，保留 v1.2 的回收站和跨单位引用保护语义。"""
    router = APIRouter()

    @router.get("/api/units")
    def list_units(_: str = Depends(get_operator)):
        return get_project().list_units()

    @router.post("/api/units")
    def add_unit(req: NameReq, operator: str = Depends(get_operator)):
        try:
            unit_id = get_project().add_unit(req.name, operator)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": unit_id}

    @router.put("/api/units/order")
    def reorder_units(req: OrderReq, operator: str = Depends(get_operator)):
        try:
            changed = get_project().reorder_units(req.ids, operator)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"changed": changed}

    @router.patch("/api/units/{unit_id}")
    def rename_unit(unit_id: int, req: NameReq, operator: str = Depends(get_operator)):
        try:
            get_project().rename_unit(unit_id, req.name, operator)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @router.delete("/api/units/{unit_id}")
    def delete_unit(unit_id: int, operator: str = Depends(get_operator)):
        try:
            project = get_project()
            require_project_idle(project)
            project.delete_unit(unit_id, operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    return router
