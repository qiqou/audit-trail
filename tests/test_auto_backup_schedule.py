"""自动备份调度路径测试（审查 F5 补测）。

覆盖 main._auto_backup_due 边界与 main._maybe_schedule_auto_backup 的
启停/幂等/排程分支——此前只有手工恢复点 job 的测试，间隔到期自动触发
逻辑无回归保障。
"""

from datetime import datetime, timedelta

import pytest

import main
from main import _auto_backup_due, _maybe_schedule_auto_backup


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class TestAutoBackupDue:
    """_auto_backup_due 边界：从未成功/坏格式/未到期/已到期/失败冷却（I4）。"""

    def test_never_succeeded_is_due(self):
        assert _auto_backup_due({}, 30) is True

    def test_bad_format_is_due(self):
        assert _auto_backup_due({"last_success_at": "not-a-date"}, 30) is True

    def test_not_due_within_interval(self):
        recent = _fmt(datetime.now() - timedelta(minutes=1))
        assert _auto_backup_due({"last_success_at": recent}, 30) is False

    def test_due_after_interval(self):
        stale = _fmt(datetime.now() - timedelta(minutes=31))
        assert _auto_backup_due({"last_success_at": stale}, 30) is True

    def test_failure_within_cooldown_not_due(self):
        """I4：失败后冷却期内不得重试（持久故障避免每次刷新都触发）。"""
        failed = _fmt(datetime.now() - timedelta(minutes=5))
        assert _auto_backup_due({"last_success_at": "", "last_error_at": failed}, 30) is False

    def test_failure_after_cooldown_is_due(self):
        """I4：失败冷却期过后允许重试。"""
        failed = _fmt(datetime.now() - timedelta(minutes=35))
        assert _auto_backup_due({"last_success_at": "", "last_error_at": failed}, 30) is True

    def test_recent_success_wins_over_old_failure(self):
        """成功与失败并存时以较新者为准。"""
        success = _fmt(datetime.now() - timedelta(minutes=2))
        failed = _fmt(datetime.now() - timedelta(hours=2))
        assert _auto_backup_due({"last_success_at": success, "last_error_at": failed}, 30) is False


class TestMaybeScheduleAutoBackup:
    """_maybe_schedule_auto_backup：禁用/未到期/幂等/排程。"""

    @pytest.fixture
    def backup_proj(self, proj, tmp_path):
        target = tmp_path / "自动备份"
        target.mkdir()
        proj.save_backup_settings(
            "调度测试员", enabled=True, target_dir=str(target),
            interval_minutes=30, retention_days=7, max_bytes=10 * 1024 * 1024,
        )
        return proj

    def test_disabled_returns_none(self, proj, tmp_path, monkeypatch):
        proj.save_backup_settings(
            "调度测试员", enabled=False, target_dir="", interval_minutes=30,
            retention_days=7, max_bytes=10 * 1024 * 1024,
        )
        submitted = []
        monkeypatch.setattr(main.job_runner, "submit", lambda *a, **k: submitted.append(a))
        assert _maybe_schedule_auto_backup(proj, "调度测试员", force=True) is None
        assert submitted == []

    def test_not_due_returns_none(self, backup_proj, monkeypatch):
        # 刚成功过（last_success_at 为当前时刻），间隔 30 分钟内不应触发
        backup_proj.record_auto_backup_result(success=True, operator="调度测试员")
        submitted = []
        monkeypatch.setattr(main.job_runner, "submit", lambda *a, **k: submitted.append(a))
        assert _maybe_schedule_auto_backup(backup_proj, "调度测试员") is None
        assert submitted == []

    def test_idempotent_when_job_active(self, backup_proj, monkeypatch):
        """已有 queued auto_backup job 时不得重复排程（幂等）。"""
        backup_proj.create_job("auto_backup", {"target_dir": "x"})
        submitted = []
        monkeypatch.setattr(main.job_runner, "submit", lambda *a, **k: submitted.append(a))
        assert _maybe_schedule_auto_backup(backup_proj, "调度测试员", force=True) is None
        assert submitted == []

    def test_due_schedules_and_submits(self, backup_proj, monkeypatch):
        """到期且无活动任务时排队并提交。"""
        submitted = []
        monkeypatch.setattr(main.job_runner, "submit", lambda *a, **k: submitted.append(a) or {"job_id": "mock"})
        result = _maybe_schedule_auto_backup(backup_proj, "调度测试员", force=True)
        assert result is not None
        assert len(submitted) == 1
        jobs = backup_proj.list_jobs(limit=100)
        auto_jobs = [j for j in jobs if j["type"] == "auto_backup"]
        assert len(auto_jobs) == 1
        assert auto_jobs[0]["status"] in {backup_proj.JOB_QUEUED, backup_proj.JOB_RUNNING}
