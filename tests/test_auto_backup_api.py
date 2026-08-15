"""自动备份策略 API 与后台恢复点任务。"""

import time

import pytest
from fastapi.testclient import TestClient

import main
from main import app


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _login(client) -> str:
    return client.post("/api/session", json={"operator": "张三"}).json()["token"]


def test_auto_backup_settings_and_manual_recovery_point_job(client, tmp_path):
    token = _login(client)
    headers = {"X-Session": token}
    assert client.post(
        "/api/project/create", json={"path": str(tmp_path / "项目"), "name": "项目"}, headers=headers
    ).status_code == 200
    target = tmp_path / "自动备份"
    target.mkdir()

    invalid = client.post(
        "/api/backup/settings",
        json={"enabled": True, "target_dir": "", "interval_minutes": 360, "retention_days": 7, "max_bytes": 1024},
        headers=headers,
    )
    assert invalid.status_code == 400
    assert "必须指定" in invalid.json()["detail"]

    saved = client.post(
        "/api/backup/settings",
        json={"enabled": True, "target_dir": str(target), "interval_minutes": 30, "retention_days": 14, "max_bytes": 10 * 1024 * 1024},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["enabled"] is True
    assert saved.json()["retention_days"] == 14

    started = client.post("/api/backup/recovery-point", headers=headers)
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    project = main._sessions[token].project
    assert project is not None
    for _ in range(100):
        job = project.get_job(job_id)
        if job and job["status"] in {project.JOB_DONE, project.JOB_ERROR, project.JOB_CANCELLED}:
            break
        time.sleep(0.02)
    assert job is not None and job["status"] == project.JOB_DONE
    assert project.get_backup_settings()["last_success_at"]
