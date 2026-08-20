"""底稿交流修订接口；保留既有交流历史兼容能力，不扩展资料请求流程。"""

from collections.abc import Callable

from api.errors import key_or_value_error
from api_models import (
    ExchangeCloseReq,
    ExchangeCommentReq,
    ExchangeRequestReq,
    ExchangeRequestUpdateReq,
    ExchangeRevisionDecisionReq,
    ExchangeRevisionReq,
)
from database import AuditProject
from fastapi import APIRouter, Depends


def build_router(get_project: Callable[..., AuditProject], get_operator: Callable[..., str]) -> APIRouter:
    """保持交流会话、修订、批注和历史兼容接口不变。"""
    router = APIRouter()

    @router.post("/api/issues/{issue_id}/exchange")
    def start_issue_exchange(issue_id: int, operator: str = Depends(get_operator)):
        """开始交流修订；正式底稿在此期间保持只读。"""
        try:
            return get_project().start_exchange_session(issue_id, operator)
        except KeyError as exc:
            raise key_or_value_error(exc) from exc

    @router.get("/api/exchanges/{session_uuid}")
    def get_issue_exchange(session_uuid: str, _: str = Depends(get_operator)):
        try:
            return get_project().get_exchange_session(session_uuid)
        except KeyError as exc:
            raise key_or_value_error(exc) from exc

    @router.post("/api/exchanges/{session_uuid}/revisions")
    def propose_exchange_revision(session_uuid: str, req: ExchangeRevisionReq,
                                  operator: str = Depends(get_operator)):
        try:
            return get_project().propose_exchange_revision(
                session_uuid, req.field_name, req.new_value, req.reason, operator,
            )
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.post("/api/exchanges/{session_uuid}/revisions/{revision_uuid}/decision")
    def decide_exchange_revision(session_uuid: str, revision_uuid: str,
                                 req: ExchangeRevisionDecisionReq, operator: str = Depends(get_operator)):
        try:
            return get_project().decide_exchange_revision(session_uuid, revision_uuid, req.decision, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.post("/api/exchanges/{session_uuid}/comments")
    def add_exchange_comment(session_uuid: str, req: ExchangeCommentReq,
                             operator: str = Depends(get_operator)):
        try:
            return get_project().add_exchange_comment(
                session_uuid, req.body, req.anchor_field, req.revision_uuid, operator,
            )
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.post("/api/exchanges/{session_uuid}/requests")
    def create_exchange_request(session_uuid: str, req: ExchangeRequestReq,
                                operator: str = Depends(get_operator)):
        try:
            return get_project().create_exchange_request(session_uuid, req.content, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.patch("/api/exchanges/{session_uuid}/requests/{request_uuid}")
    def update_exchange_request(session_uuid: str, request_uuid: str, req: ExchangeRequestUpdateReq,
                                operator: str = Depends(get_operator)):
        try:
            return get_project().update_exchange_request(
                session_uuid, request_uuid, req.status, req.provided_file_id, req.note, operator,
            )
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.post("/api/exchanges/{session_uuid}/apply")
    def apply_exchange_revisions(session_uuid: str, operator: str = Depends(get_operator)):
        try:
            return get_project().apply_exchange_revisions(session_uuid, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    @router.post("/api/exchanges/{session_uuid}/close")
    def close_issue_exchange(session_uuid: str, req: ExchangeCloseReq,
                             operator: str = Depends(get_operator)):
        try:
            return get_project().close_exchange_session(session_uuid, req.note, operator)
        except (KeyError, ValueError) as exc:
            raise key_or_value_error(exc) from exc

    return router
