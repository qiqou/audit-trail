"""项目库迁移的只读门禁与快照基础设施。"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path


def preflight_database(connection: sqlite3.Connection) -> list[str]:
    """只读检查损坏、外键异常和已有 UUID 重复，不修改数据库。"""
    problems: list[str] = []
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is not None and integrity[0] != "ok":
        problems.append("SQLite 完整性检查失败")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        problems.append("项目包含孤儿关联或外键约束异常")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column in (("units", "unit_uuid"), ("issues", "issue_uuid"), ("files", "file_uuid")):
        if table not in tables:
            continue
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            continue
        duplicate = connection.execute(
            f"SELECT 1 FROM {table} WHERE {column} <> '' GROUP BY {column} HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            problems.append(f"{table} 存在重复 {column}")
    return problems


def create_snapshot(connection: sqlite3.Connection, root: Path, snapshot_dir: str, source_version: int) -> str:
    """通过 SQLite backup API 创建 DDL 前一致性快照，失败不留下半成品。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = root / snapshot_dir / f"pre_migration_v{source_version}_{stamp}.db"
    temp_target = target.with_suffix(".tmp")
    backup_connection = sqlite3.connect(temp_target)
    try:
        connection.backup(backup_connection)
    finally:
        backup_connection.close()
    os.replace(temp_target, target)
    return str(target.relative_to(root).as_posix())
