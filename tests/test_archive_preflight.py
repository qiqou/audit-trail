"""归档核对清单：完整性阻断、警告提示及一次性确认令牌。"""

from pathlib import Path

import pytest
from export import archive_preflight
from fastapi.testclient import TestClient

import main
from main import app


def test_archive_preflight_blocks_missing_attachment_and_warns_unarchived_issue(proj, tmp_path):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="未归档底稿")
    source = tmp_path / "证据.txt"
    source.write_text("证据", encoding="utf-8")
    evidence = proj.add_file(unit_id, source, "张三")
    proj.link_file(issue_id, evidence["id"], "张三")

    clean = archive_preflight(proj)
    assert clean["ok"] is True
    assert any(item["code"] == "non_archived_issues" for item in clean["warnings"])

    Path(proj.attachment_path(evidence["rel_path"])).unlink()
    broken = archive_preflight(proj)
    assert broken["ok"] is False
    assert any(item["code"] == "missing_file" for item in broken["blockers"])


def test_archive_preflight_blocks_unresolved_merge_conflict_and_tampered_log(proj):
    """P0：未闭环合并冲突或永久日志被改动时，一律不能归档。"""
    proj.log("张三", "测试日志", "归档前核对")
    now = "2026-08-13 12:00:00"
    with proj._lock, proj._conn:
        proj._conn.execute(
            "INSERT INTO merge_batches(batch_uuid, operator, source_count, created_at) VALUES(?,?,?,?)",
            ("batch-open", "张三", 1, now),
        )
        proj._conn.execute(
            "INSERT INTO merge_conflicts(batch_uuid, source_name, conflict_type, message, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("batch-open", "来源.auditbak", "seq_reassign", "编号冲突待处理", "open", now),
        )
    blocked = archive_preflight(proj)
    assert any(item["code"] == "unresolved_merge_conflicts" for item in blocked["blockers"])

    with proj._lock, proj._conn:
        proj._conn.execute("UPDATE merge_conflicts SET status='resolved' WHERE batch_uuid='batch-open'")
        proj._conn.execute("UPDATE audit_log SET detail='已被篡改' WHERE id=(SELECT MAX(id) FROM audit_log)")
    tampered = archive_preflight(proj)
    assert any(item["code"] == "audit_log_chain_invalid" for item in tampered["blockers"])


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def test_package_requires_fresh_preflight_confirmation_token(client, tmp_path):
    token = client.post("/api/session", json={"operator": "张三"}).json()["token"]
    headers = {"X-Session": token}
    created = client.post(
        "/api/project/create", json={"path": str(tmp_path / "项目"), "name": "项目"}, headers=headers,
    )
    assert created.status_code == 200
    assert client.post("/api/units", json={"name": "甲单位"}, headers=headers).status_code == 200

    checked = client.post(
        "/api/export/package/preflight",
        json={"scope": "all", "unit_ids": [], "group_by_dept": False}, headers=headers,
    )
    assert checked.status_code == 200
    confirmation_token = checked.json()["confirmation_token"]
    assert confirmation_token

    project = main._sessions[token].project
    assert project is not None
    project.add_issue(1, "张三", defect_type="核对后新增")
    stale = client.post(
        "/api/export/package",
        json={"scope": "all", "unit_ids": [], "group_by_dept": False, "confirmation_token": confirmation_token},
        headers=headers,
    )
    assert stale.status_code == 409
    assert "重新核对" in stale.json()["detail"]
