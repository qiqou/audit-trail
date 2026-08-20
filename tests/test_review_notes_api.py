"""内部复核意见 HTTP 合同：事件追加与基线冲突均使用稳定状态码。"""

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _headers(client: TestClient) -> dict[str, str]:
    token = client.post("/api/session", json={"operator": "李四"}).json()["token"]
    return {"X-Session": token}


def test_review_note_api_preserves_event_chain(client: TestClient, tmp_path):
    headers = _headers(client)
    assert client.post("/api/project/create", json={"path": str(tmp_path / "复核项目")}, headers=headers).status_code == 200
    unit_id = client.post("/api/units", json={"name": "甲单位"}, headers=headers).json()["id"]
    issue_id = client.post(
        f"/api/units/{unit_id}/issues", json={"defect_type": "收入截止"}, headers=headers,
    ).json()["id"]
    baseline = client.get(f"/api/issues/{issue_id}/draft", headers=headers).json()["current_version_id"]

    created = client.post(
        f"/api/issues/{issue_id}/review-notes",
        json={"body": "请补充回函", "base_version_id": baseline, "anchor_field": "defect_desc"},
        headers=headers,
    )
    assert created.status_code == 200
    note_uuid = created.json()["note_uuid"]
    assert client.post(f"/api/review-notes/{note_uuid}/reply", json={"body": "已补充"}, headers=headers).status_code == 200
    resolved = client.post(f"/api/review-notes/{note_uuid}/resolve", json={"body": "确认"}, headers=headers)
    assert resolved.status_code == 200
    assert [event["event_type"] for event in resolved.json()["events"]] == ["created", "replied", "resolved"]
    assert client.post(f"/api/review-notes/{note_uuid}/resolve", json={}, headers=headers).status_code == 409
