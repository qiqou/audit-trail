"""项目库迁移的只读门禁与快照基础设施。"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class MigrationPreparation:
    """DDL 前已完成的只读判定与快照结果。"""

    source_version: int
    needs_snapshot: bool
    backup_rel_path: str


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


def prepare_schema_migration(
    connection: sqlite3.Connection, root: Path, *, schema_version_key: str, target_version: int, snapshot_dir: str,
) -> MigrationPreparation:
    """统一执行版本读取、高版本拒绝、只读预检与 DDL 前快照。"""
    connection.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT DEFAULT '')")
    row = connection.execute("SELECT value FROM meta WHERE key=?", (schema_version_key,)).fetchone()
    try:
        source_version = int(row["value"] if row is not None else 0)
    except (TypeError, ValueError, KeyError, IndexError):
        source_version = 0
    if source_version > target_version:
        raise ValueError(
            f"项目数据由更新版本（schema v{source_version}）创建，当前程序仅支持 v{target_version}。"
            "请升级审迹后再打开此项目；如需回退，请先备份 .auditbak"
        )
    has_legacy_data = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT IN ('meta', 'sqlite_sequence') LIMIT 1"
    ).fetchone() is not None
    needs_snapshot = source_version < target_version and (source_version > 0 or has_legacy_data)
    if needs_snapshot:
        problems = preflight_database(connection)
        if problems:
            raise ValueError("项目迁移预检未通过：" + "；".join(problems) + "。请从可信备份恢复后再升级。")
    backup_rel_path = create_snapshot(connection, root, snapshot_dir, source_version) if needs_snapshot else ""
    return MigrationPreparation(source_version, needs_snapshot, backup_rel_path)


def record_schema_migration(
    connection: sqlite3.Connection, *, schema_version_key: str, target_version: int, applied_at: str, backup_rel_path: str,
) -> None:
    """在 DDL/DML 与关系校验完成后，原子写入版本和迁移记录。"""
    connection.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (schema_version_key, str(target_version)),
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at, backup_rel_path) VALUES(?,?,?)",
        (target_version, applied_at, backup_rel_path),
    )
