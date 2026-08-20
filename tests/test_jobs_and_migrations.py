"""3.0 重构基础设施回归：数据库迁移快照、持久任务与动态端口。"""

import shutil
import socket
import sqlite3
import threading
import time

import pytest
from config import RuntimeSettings
from database import SCHEMA_VERSION, AuditProject
from jobs import ProjectJobRunner
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


def test_v6_project_migration_preserves_one_thousand_issue_records(tmp_path):
    """P0：迁移不允许静默丢改历史底稿；用 1,000 条记录做字段与数量对账。"""
    root = tmp_path / "千条迁移项目"
    project = AuditProject(root)
    unit_id = project.add_unit("甲单位", "张三")
    for index in range(1000):
        project.add_issue(
            unit_id,
            "张三",
            defect_type=f"问题{index}",
            amount=f"{index}.00",
            currency="CNY",
            amount_unit="元",
        )
    before = [
        tuple(row) for row in project._conn.execute(
            "SELECT issue_uuid, seq, defect_type, amount, amount_minor, currency, amount_unit "
            "FROM issues ORDER BY id"
        ).fetchall()
    ]
    project.set_meta("schema_version", "6")
    project.close()

    upgraded = AuditProject(root)
    try:
        after = [
            tuple(row) for row in upgraded._conn.execute(
                "SELECT issue_uuid, seq, defect_type, amount, amount_minor, currency, amount_unit "
                "FROM issues ORDER BY id"
            ).fetchall()
        ]
        assert after == before
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert list((root / "快照").glob("pre_migration_v6_*.db"))
    finally:
        upgraded.close()


@pytest.mark.parametrize(
    ("source_version", "missing_tables"),
    [
        (14, ("workpaper_templates", "issue_drafts", "review_note_events")),
        (15, ("workpaper_templates", "issue_drafts", "review_note_events")),
        (16, ("issue_drafts", "review_note_events")),
        (17, ("issue_drafts", "review_note_events")),
    ],
)
def test_v14_to_v17_migrations_keep_counts_and_preserve_a_reopenable_snapshot(
    tmp_path, source_version, missing_tables,
):
    """v14→v18 每个入口均应保留 DDL 前快照，且快照本身可再次打开升级。"""
    root = tmp_path / f"v{source_version}项目"
    original = AuditProject(root)
    unit_id = original.add_unit("甲单位", "张三")
    original.add_issue(unit_id, "张三", defect_type=f"v{source_version}底稿")
    before_counts = {
        table: original._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("units", "issues", "files", "audit_log")
    }
    with original._lock, original._conn:
        for table in missing_tables:
            original._conn.execute(f"DROP TABLE {table}")
        original._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(source_version),)
        )
    original.close()

    upgraded = AuditProject(root)
    try:
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert {
            table: upgraded._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before_counts
        } == before_counts
        snapshot = next((root / "快照").glob(f"pre_migration_v{source_version}_*.db"))
    finally:
        upgraded.close()

    recovered_root = tmp_path / f"v{source_version}快照恢复"
    recovered_root.mkdir()
    shutil.copy2(snapshot, recovered_root / "audit.db")
    recovered = AuditProject(recovered_root)
    try:
        assert recovered.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert recovered._conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == before_counts["issues"]
    finally:
        recovered.close()


def test_real_v11_table_layout_migrates_by_column_name_and_keeps_exchange_foreign_keys(tmp_path):
    """v1.1 原始列序升级后不得错位业务数据，交流外键也必须指向新表。

    不能用当前 schema 再 DROP COLUMN 伪造旧库：那种做法保留了 v1.2 的物理列
    顺序，无法覆盖此次重建中 ``SELECT *`` 错位的真实风险。
    """
    root = tmp_path / "v1.1 原始结构"
    root.mkdir()
    db_path = root / "audit.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT DEFAULT '');
            CREATE TABLE units(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0, created_at TEXT
            );
            CREATE TABLE issues(
                id INTEGER PRIMARY KEY AUTOINCREMENT, unit_id INTEGER NOT NULL, seq INTEGER NOT NULL,
                department TEXT DEFAULT '', category TEXT DEFAULT '', defect_type TEXT DEFAULT '',
                defect_desc TEXT DEFAULT '', amount TEXT DEFAULT '', regulation_basis TEXT DEFAULT '',
                suggestion TEXT DEFAULT '', author TEXT DEFAULT '', reviewer TEXT DEFAULT '',
                status TEXT DEFAULT '草稿', created_at TEXT, updated_at TEXT
            );
            CREATE TABLE issue_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id INTEGER NOT NULL, version_no INTEGER NOT NULL,
                snapshot TEXT NOT NULL, saved_by TEXT DEFAULT '', created_at TEXT
            );
            CREATE TABLE files(
                id INTEGER PRIMARY KEY AUTOINCREMENT, unit_id INTEGER NOT NULL, stored_name TEXT NOT NULL,
                orig_name TEXT NOT NULL, folder_path TEXT NOT NULL DEFAULT '', rel_path TEXT NOT NULL,
                size INTEGER DEFAULT 0, sha256 TEXT DEFAULT '', mime TEXT DEFAULT '', exclusive_to INTEGER,
                created_at TEXT
            );
            CREATE TABLE issue_files(issue_id INTEGER NOT NULL, file_id INTEGER NOT NULL, linked_at TEXT,
                PRIMARY KEY(issue_id, file_id));
            CREATE TABLE audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, operator TEXT NOT NULL, action TEXT NOT NULL,
                target TEXT DEFAULT '', detail TEXT DEFAULT '', created_at TEXT
            );
            CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, backup_rel_path TEXT DEFAULT ''
            );
            CREATE TABLE jobs(
                id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
                progress TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}', error TEXT DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT
            );
            INSERT INTO meta(key, value) VALUES('schema_version', '3');
            INSERT INTO units(name, sort_order, created_at) VALUES('甲单位', 7, '2026-08-01 09:00:00');
            INSERT INTO issues(
                unit_id, seq, department, category, defect_type, defect_desc, amount, regulation_basis,
                suggestion, author, reviewer, status, created_at, updated_at
            ) VALUES(
                1, 3, '采购', '管理', '审批缺失', '描述', '100', '制度', '建议', '张三', '李四',
                '草稿', '2026-08-01 09:00:00', '2026-08-02 09:00:00'
            );
            INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at)
                VALUES(1, 1, '{}', '张三', '2026-08-02 09:00:00');
            INSERT INTO audit_log(operator, action, created_at) VALUES('张三', '新建底稿', '2026-08-01 09:00:00');
            """
        )

    upgraded = AuditProject(root)
    try:
        assert upgraded.list_units()[0]["name"] == "甲单位"
        issue = upgraded.get_issue(1)
        assert issue is not None
        assert {
            key: issue[key]
            for key in ("seq", "department", "category", "defect_type", "defect_desc", "amount", "status")
        } == {
            "seq": 3, "department": "采购", "category": "管理", "defect_type": "审批缺失",
            "defect_desc": "描述", "amount": "100", "status": "草稿",
        }
        session = upgraded.start_exchange_session(1, "复核人")
        assert session["issue"]["id"] == 1
        assert not upgraded._conn.execute("PRAGMA foreign_key_check").fetchall()
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


def test_project_jobs_run_in_fifo_order_and_cancelled_queue_item_never_starts(proj):
    """同一项目的重任务不能并行；取消排队项不能在前项结束后被意外执行。"""
    runner = ProjectJobRunner()
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    first = proj.create_job("backup")
    second = proj.create_job("health_scan")
    third = proj.create_job("archive")

    def first_handler(_):
        order.append("first")
        started.set()
        assert release.wait(timeout=2)
        return {"ok": True}

    def second_handler(_):
        order.append("second")
        return {"ok": True}

    def third_handler(_):
        order.append("third")
        return {"ok": True}

    runner.submit(proj, first["id"], first_handler)
    assert started.wait(timeout=2)
    runner.submit(proj, second["id"], second_handler)
    runner.submit(proj, third["id"], third_handler)
    assert proj.get_job(second["id"])["status"] == AuditProject.JOB_QUEUED
    assert runner.cancel(proj, second["id"])["status"] == AuditProject.JOB_CANCELLED

    release.set()
    deadline = time.monotonic() + 2
    while proj.get_job(third["id"])["status"] != AuditProject.JOB_DONE and time.monotonic() < deadline:
        time.sleep(0.01)

    assert proj.get_job(first["id"])["status"] == AuditProject.JOB_DONE
    assert proj.get_job(second["id"])["status"] == AuditProject.JOB_CANCELLED
    assert proj.get_job(third["id"])["status"] == AuditProject.JOB_DONE
    assert order == ["first", "third"]


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


def test_job_runner_can_cancel_and_wait_for_project_queue(proj):
    """受控关闭可请求取消队列，不会强关仍在运行的项目连接。"""
    from jobs import ProjectJobRunner

    runner = ProjectJobRunner()
    job = proj.create_job("test", {})
    started = threading.Event()

    def handler(ctx):
        started.set()
        while not ctx.cancelled():
            time.sleep(0.01)
        raise InterruptedError("cancelled")

    runner.submit(proj, job["id"], handler)
    assert started.wait(timeout=1)
    assert runner.cancel_all(proj) == 1
    assert runner.wait_until_idle(proj, timeout=1) is True
    assert proj.get_job(job["id"])["status"] == proj.JOB_CANCELLED


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


def test_runtime_settings_uses_upgrade_port_by_default(monkeypatch):
    """改造版必须避开原审迹 v1.1 的固定 8765 端口。"""
    monkeypatch.delenv("AUDIT_ASSISTANT_PORT", raising=False)
    assert RuntimeSettings.from_environment().port == 8766

    monkeypatch.setenv("AUDIT_ASSISTANT_PORT", "not-a-port")
    with pytest.raises(ValueError, match="AUDIT_ASSISTANT_PORT"):
        RuntimeSettings.from_environment()

    monkeypatch.setenv("AUDIT_ASSISTANT_PORT", "0")
    monkeypatch.setenv("AUDIT_ASSISTANT_FRONTEND", "unsupported")
    with pytest.raises(ValueError, match="AUDIT_ASSISTANT_FRONTEND"):
        RuntimeSettings.from_environment()
