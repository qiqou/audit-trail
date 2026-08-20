"""交流会话的只读查询仓储。"""

from typing import Any


class ExchangeRepository:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def get_session(self, session_uuid: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM exchange_sessions WHERE session_uuid=?", (session_uuid,)
        ).fetchone()
        return dict(row) if row else None

    def find_open_session_for_issue_uuid(self, issue_uuid: str) -> str | None:
        row = self._connection.execute(
            "SELECT session_uuid FROM exchange_sessions WHERE issue_uuid=? AND status='open' "
            "ORDER BY opened_at DESC LIMIT 1",
            (issue_uuid,),
        ).fetchone()
        return str(row["session_uuid"]) if row else None
