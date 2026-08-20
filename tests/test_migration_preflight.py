"""迁移前的异常旧库必须被只读门禁阻断，不能静默修补。"""

import sqlite3

import pytest
from database import AuditProject


def test_migration_preflight_rejects_duplicate_issue_uuid_before_snapshot(tmp_path):
    root = tmp_path / "重复 UUID 项目"
    root.mkdir()
    with sqlite3.connect(root / "audit.db") as connection:
        connection.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE issues(id INTEGER PRIMARY KEY, issue_uuid TEXT);"
            "INSERT INTO meta VALUES('schema_version', '17');"
            "INSERT INTO issues VALUES(1, 'same-uuid'), (2, 'same-uuid');"
        )

    with pytest.raises(ValueError, match="重复 issue_uuid"):
        AuditProject(root)
    assert not list((root / "快照").glob("pre_migration_v17_*.db"))


def test_preflight_database_reports_foreign_key_violation(tmp_path):
    from db.migration_runner import preflight_database

    db = tmp_path / "broken.db"
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript("CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(parent_id INTEGER REFERENCES parent(id)); INSERT INTO child VALUES(7);")
        assert "项目包含孤儿关联或外键约束异常" in preflight_database(connection)


def test_completed_migration_validation_rejects_missing_required_column(tmp_path):
    from db.migration_runner import validate_completed_schema

    db = tmp_path / "incomplete.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE units(id INTEGER PRIMARY KEY, name TEXT)")
        with pytest.raises(ValueError, match="units.unit_uuid"):
            validate_completed_schema(connection, required_columns={"units": {"id", "unit_uuid"}})
