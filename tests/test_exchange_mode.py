"""P1-14 问题交流：一轮一个版本，审阅窗保留可回溯记录。"""

from database import AuditProject
from fastapi.testclient import TestClient

from main import app


def _issue(proj: AuditProject) -> int:
    unit_id = proj.add_unit("单位A", "编制人")
    return proj.add_issue(
        unit_id, "编制人", department="采购", defect_type="采购审批缺失",
        defect_desc="原始描述", regulation_basis="制度第 1 条", suggestion="原始建议",
    )


def test_exchange_revision_saves_current_content_and_end_round_creates_one_version(proj):
    issue_id = _issue(proj)
    session = proj.start_exchange_session(issue_id, "交流人")

    first = proj.propose_exchange_revision(
        session["session_uuid"], "defect_desc", "修订后描述", "现场补充说明", "交流人",
    )
    revision = first["revisions"][0]
    assert first["issue"]["defect_desc"] == "修订后描述"
    assert revision["old_value"] == "原始描述"
    assert revision["new_value"] == "修订后描述"
    assert revision["applied_at"] is None
    versions = proj.list_versions(issue_id)
    assert len(versions) == 1, "单次保存不能单独生成版本"
    assert revision["version_id"] is None

    # 再次修订同一字段，原文必须来自刚保存的内容，不能回退到进入交流时的基线。
    second = proj.propose_exchange_revision(
        session["session_uuid"], "defect_desc", "第二轮修订描述", "补充资料已到位", "交流人",
    )
    next_revision = second["revisions"][-1]
    assert next_revision["old_value"] == "修订后描述"
    assert second["issue"]["defect_desc"] == "第二轮修订描述"
    assert len(proj.list_versions(issue_id)) == 1

    closed = proj.close_exchange_session(session["session_uuid"], "本轮确认", "交流人")
    versions = proj.list_versions(issue_id)
    assert closed["status"] == "closed"
    assert len(versions) == 2, "多次本轮修订结束时只应生成一个版本"
    assert {item["version_id"] for item in closed["revisions"]} == {versions[-1]["id"]}
    assert all(item["applied_at"] for item in closed["revisions"])
    assert any(log["action"] == "结束本轮交流" for log in proj.list_logs())


def test_blank_issue_does_not_create_meaningless_initial_version(proj):
    unit_id = proj.add_unit("单位A", "编制人")
    issue_id = proj.add_issue(unit_id, "编制人")
    assert proj.list_versions(issue_id) == []

    proj.update_issue(issue_id, "编制人", defect_desc="首次录入的问题描述")
    versions = proj.list_versions(issue_id)
    assert len(versions) == 1
    assert versions[0]["version_no"] == 1


def test_existing_revision_without_version_id_is_backfilled(proj):
    issue_id = _issue(proj)
    session = proj.start_exchange_session(issue_id, "交流人")
    session = proj.propose_exchange_revision(
        session["session_uuid"], "suggestion", "更新后的审计建议", "补充事实", "交流人",
    )
    closed = proj.close_exchange_session(session["session_uuid"], "确认", "交流人")
    revision_uuid = closed["revisions"][0]["revision_uuid"]
    with proj._conn:
        proj._conn.execute("UPDATE exchange_revisions SET version_id=NULL WHERE revision_uuid=?", (revision_uuid,))

    proj._backfill_exchange_revision_versions()
    revision = proj.get_exchange_session(session["session_uuid"])["revisions"][0]
    assert revision["version_id"] == proj.list_versions(issue_id)[-1]["id"]


def test_exchange_request_delete_is_soft_and_keeps_audit_record(proj):
    issue_id = _issue(proj)
    session = proj.start_exchange_session(issue_id, "交流人")
    created = proj.create_exchange_request(session["session_uuid"], "补充审批单", "交流人")
    request_uuid = created["requests"][0]["request_uuid"]

    updated = proj.update_exchange_request(
        session["session_uuid"], request_uuid, "withdrawn", None, "不再需要", "交流人",
    )

    assert len(updated["requests"]) == 1, "删除待补资料不能破坏永久交流留痕"
    assert updated["requests"][0]["status"] == "withdrawn"
    assert updated["requests"][0]["note"] == "不再需要"
    assert any(log["action"] == "更新待补资料" for log in proj.list_logs())


def test_new_exchange_round_keeps_all_previous_review_records(proj):
    issue_id = _issue(proj)
    first = proj.start_exchange_session(issue_id, "第一轮人员")
    first = proj.propose_exchange_revision(
        first["session_uuid"], "defect_desc", "第一轮修订描述", "", "第一轮人员",
    )
    first = proj.add_exchange_comment(
        first["session_uuid"], "第一轮批注", "defect_desc", "", "第一轮人员",
    )
    first = proj.create_exchange_request(first["session_uuid"], "第一轮待补资料", "第一轮人员")
    first = proj.close_exchange_session(first["session_uuid"], "第一轮结束", "第一轮人员")
    first_version_id = first["revisions"][0]["version_id"]

    second = proj.start_exchange_session(issue_id, "第二轮人员")
    assert second["session_uuid"] != first["session_uuid"]
    assert [(item["new_value"], item["version_id"]) for item in second["revisions"]] == [
        ("第一轮修订描述", first_version_id),
    ]
    assert [item["body"] for item in second["comments"]] == ["第一轮批注"]
    assert [item["content"] for item in second["requests"]] == ["第一轮待补资料"]

    second = proj.propose_exchange_revision(
        second["session_uuid"], "suggestion", "第二轮审计建议", "", "第二轮人员",
    )
    assert len(second["revisions"]) == 2
    assert second["revisions"][0]["version_id"] == first_version_id
    assert second["revisions"][1]["version_id"] is None


def test_exchange_api_creates_session_and_exposes_navigation_payload(tmp_path):
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "交流测试员"}).json()["token"]
        headers = {"X-Session": token}
        assert client.post("/api/project/create", json={"path": str(tmp_path / "交流项目")}, headers=headers).status_code == 200
        unit_id = client.post("/api/units", json={"name": "单位A"}, headers=headers).json()["id"]
        issue_id = client.post(
            f"/api/units/{unit_id}/issues", json={"department": "采购", "defect_type": "问题A"}, headers=headers,
        ).json()["id"]

        response = client.post(f"/api/issues/{issue_id}/exchange", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "open"
        assert payload["issue"]["id"] == issue_id
        assert payload["base_snapshot"]["defect_type"] == "问题A"
