"""附件与资料库的只读查询仓储。"""

from typing import Any


class EvidenceRepository:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def get(self, file_id: int, *, include_deleted: bool = False) -> dict | None:
        sql = "SELECT * FROM files WHERE id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = self._connection.execute(sql, (file_id,)).fetchone()
        return dict(row) if row else None

    def find_folder_by_fingerprint(self, sha256: str) -> dict | None:
        if not sha256:
            return None
        row = self._connection.execute(
            "SELECT f.*, u.name AS unit_name FROM files f "
            "JOIN units u ON u.id=f.unit_id WHERE f.mime='folder' AND f.sha256=? "
            "AND f.deleted_at IS NULL AND u.deleted_at IS NULL ORDER BY f.id LIMIT 1",
            (sha256,),
        ).fetchone()
        return dict(row) if row else None

    def find_file_by_sha(self, sha256: str) -> dict | None:
        if not sha256:
            return None
        row = self._connection.execute(
            "SELECT f.*, u.name AS unit_name FROM files f "
            "JOIN units u ON u.id=f.unit_id WHERE f.sha256=? AND f.deleted_at IS NULL "
            "AND u.deleted_at IS NULL ORDER BY f.id LIMIT 1",
            (sha256,),
        ).fetchone()
        return dict(row) if row else None

    def list_active_for_unit(self, unit_id: int) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM files WHERE unit_id=? AND deleted_at IS NULL ORDER BY orig_name", (unit_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_shareable_for_unit(self, unit_id: int) -> list[dict]:
        rows = self._connection.execute(
            "SELECT * FROM files WHERE unit_id=? AND deleted_at IS NULL AND exclusive_to IS NULL ORDER BY orig_name",
            (unit_id,),
        ).fetchall()
        return [dict(row) for row in rows]
