"""被审计单位的只读查询仓储。"""

from typing import Any


class UnitRepository:
    """保持 SQL 显式，连接和锁仍由 ``AuditProject`` 统一持有。"""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def get(self, unit_id: int, *, include_deleted: bool = False) -> dict | None:
        sql = "SELECT * FROM units WHERE id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = self._connection.execute(sql, (unit_id,)).fetchone()
        return dict(row) if row else None

    def list_active(self) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM units WHERE deleted_at IS NULL ORDER BY sort_order, id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_active_by_name(self, name: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM units WHERE name=? AND deleted_at IS NULL", (str(name).strip(),)
        ).fetchone()
        return dict(row) if row else None
