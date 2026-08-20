"""底稿只读查询仓储。

保持 SQL 可审计；写入、排序、版本和事务仍由 ``AuditProject`` 编排，不能绕开
项目级锁与操作留痕。
"""

from typing import Any


class IssueRepository:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def get(self, issue_id: int, *, include_deleted: bool = False) -> dict | None:
        sql = "SELECT * FROM issues WHERE id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = self._connection.execute(sql, (issue_id,)).fetchone()
        return dict(row) if row else None

    def list_active_for_unit(self, unit_id: int) -> list[dict]:
        rows = self._connection.execute(
            """
            SELECT i.*, COALESCE(file_counts.file_count, 0) AS file_count
            FROM issues i
            LEFT JOIN (
                SELECT issue_id, COUNT(*) AS file_count FROM issue_files GROUP BY issue_id
            ) AS file_counts ON file_counts.issue_id=i.id
            WHERE i.unit_id=? AND i.deleted_at IS NULL
            ORDER BY i.sort_order, i.id
            """,
            (unit_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_active_grouped_by_unit(self) -> dict[int, list[dict]]:
        rows = self._connection.execute(
            """
            SELECT i.*, COALESCE(file_counts.file_count, 0) AS file_count
            FROM issues i
            JOIN units u ON u.id=i.unit_id
            LEFT JOIN (
                SELECT issue_id, COUNT(*) AS file_count FROM issue_files GROUP BY issue_id
            ) AS file_counts ON file_counts.issue_id=i.id
            WHERE i.deleted_at IS NULL AND u.deleted_at IS NULL
            ORDER BY u.sort_order, u.id, i.sort_order, i.id
            """
        ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            issue = dict(row)
            grouped.setdefault(issue["unit_id"], []).append(issue)
        return grouped
