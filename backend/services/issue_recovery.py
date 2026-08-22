"""底稿异常恢复与内部复核的服务边界。

2026-08-22：内部复核意见下线（v1.4 预留）——review_notes 相关转发方法无路由调用，
保留供 v1.4 恢复；草稿（draft_*）方法仍由 v1.3 API 使用。
"""

from database import AuditProject


class IssueRecoveryService:
    """编排草稿/复核业务；持久化仍由 AuditProject 的同一事务完成。"""

    def __init__(self, project: AuditProject) -> None:
        self._project = project

    def draft_state(self, issue_id: int) -> dict:
        return self._project.get_issue_draft_state(issue_id)

    def save_draft(
        self, issue_id: int, payload: dict, base_version_id: int, base_updated_at: str, operator: str,
    ) -> dict:
        return self._project.save_issue_draft(
            issue_id, payload, base_version_id, base_updated_at, operator,
        )

    def discard_draft(self, issue_id: int) -> bool:
        return self._project.discard_issue_draft(issue_id)

    def review_notes(self, issue_id: int) -> list[dict]:
        return self._project.list_review_notes(issue_id)

    def create_review_note(
        self, issue_id: int, body: str, anchor_field: str, base_version_id: int, operator: str,
    ) -> dict:
        return self._project.create_review_note(
            issue_id, body, anchor_field, base_version_id, operator,
        )

    def append_review_note_event(self, note_uuid: str, event_type: str, body: str, operator: str) -> dict:
        return self._project.append_review_note_event(note_uuid, event_type, body, operator)
