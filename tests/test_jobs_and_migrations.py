"""3.0 重构基础设施回归：数据库迁移快照、持久任务与动态端口。"""

import socket
import sqlite3

import pytest
from config import RuntimeSettings
from database import SCHEMA_VERSION, AuditProject
from platform_adapter import port_in_use, reserve_local_port


def test_v1_project_migrates_with_pre_migration_snapshot(tmp_path):
    """旧项目升级前产生一致性快照，升级后具有 jobs 和迁移记录。"""
    root = tmp_path / "旧项目"
    project = AuditProject(root)
    project.add_unit("单位A", "测试员")
    with project._lock, project._conn:
        project._conn.execute("DROP TABLE jobs")
        project._conn.execute("DROP TABLE schema_migrations")
        project._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '1')"
        )
    project.close()

    upgraded = AuditProject(root)
    try:
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert upgraded.create_job("health_scan")["status"] == AuditProject.JOB_QUEUED
        snapshots = list((root / "快照").glob("pre_migration_v1_*.db"))
        assert len(snapshots) == 1

        with sqlite3.connect(snapshots[0]) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "jobs" not in tables, "迁移快照必须是 DDL 前的旧数据库"
    finally:
        upgraded.close()


def test_v2_project_migrates_problem_category_with_snapshot(tmp_path):
    """v2 项目打开后新增 category 列，并在 DDL 前保留 v2 快照。"""
    root = tmp_path / "v2项目"
    project = AuditProject(root)
    with project._lock, project._conn:
        project._conn.execute("ALTER TABLE issues DROP COLUMN category")
        project._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '2')"
        )
    project.close()

    upgraded = AuditProject(root)
    try:
        cols = {row[1] for row in upgraded._conn.execute("PRAGMA table_info(issues)")}
        assert "category" in cols
        assert list((root / "快照").glob("pre_migration_v2_*.db"))
    finally:
        upgraded.close()


def test_local_job_state_is_persisted(proj):
    """排队、执行、取消、结果均随项目落 SQLite，而不是存在 API 进程内存。"""
    job = proj.create_job("health_scan", {"sample_size": 0})
    assert job["status"] == "queued"
    running = proj.start_job(job["id"])
    assert running["status"] == "running"

    proj.update_job_progress(job["id"], {"phase": "hash", "done": 2, "total": 3})
    done = proj.finish_job(job["id"], {"counts": {"files": 3}})
    assert done["status"] == "done"
    assert done["progress"]["phase"] == "hash"
    assert done["result"]["counts"]["files"] == 3

    cancelled = proj.create_job("health_scan")
    result = proj.request_job_cancel(cancelled["id"])
    assert result["status"] == "cancelled"
    assert result["cancel_requested"] is True


def test_dynamic_port_is_reserved_until_listener_is_closed():
    """动态端口在返回给启动器后立即处于占用状态，避免固定端口和启动竞态。"""
    listener, port = reserve_local_port()
    try:
        assert port > 0
        assert port_in_use("127.0.0.1", port) is True
        with pytest.raises(OSError):
            other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                other.bind(("127.0.0.1", port))
            finally:
                other.close()
    finally:
        listener.close()
    assert port_in_use("127.0.0.1", port) is False


def test_runtime_settings_read_environment(monkeypatch):
    monkeypatch.setenv("AUDIT_ASSISTANT_PORT", "45678")
    monkeypatch.setenv("AUDIT_ASSISTANT_DEBUG", "true")
    monkeypatch.delenv("AUDIT_ASSISTANT_FRONTEND", raising=False)
    settings = RuntimeSettings.from_environment()
    assert settings.port == 45678
    assert settings.debug is True
    assert settings.frontend == "v3"

    monkeypatch.setenv("AUDIT_ASSISTANT_FRONTEND", "v3")
    assert RuntimeSettings.from_environment().frontend == "v3"
    monkeypatch.setenv("AUDIT_ASSISTANT_FRONTEND", "legacy")
    with pytest.raises(ValueError, match="AUDIT_ASSISTANT_FRONTEND"):
        RuntimeSettings.from_environment()

    monkeypatch.setenv("AUDIT_ASSISTANT_PORT", "not-a-port")
    with pytest.raises(ValueError, match="AUDIT_ASSISTANT_PORT"):
        RuntimeSettings.from_environment()

    monkeypatch.setenv("AUDIT_ASSISTANT_PORT", "0")
    monkeypatch.setenv("AUDIT_ASSISTANT_FRONTEND", "unsupported")
    with pytest.raises(ValueError, match="AUDIT_ASSISTANT_FRONTEND"):
        RuntimeSettings.from_environment()
