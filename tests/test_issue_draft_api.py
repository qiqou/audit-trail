"""独立草稿 HTTP 合同：空草稿也返回可保存的正式基线。"""

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/session", json={"operator": "张三"})
    assert response.status_code == 200
    return {"X-Session": response.json()["token"]}


def test_issue_draft_api_returns_baseline_and_rejects_stale_save(client: TestClient, tmp_path):
    headers = _headers(client)
    assert client.post("/api/project/create", json={"path": str(tmp_path / "草稿项目")}, headers=headers).status_code == 200
    unit_id = client.post("/api/units", json={"name": "甲单位"}, headers=headers).json()["id"]
    issue_id = client.post(
        f"/api/units/{unit_id}/issues", json={"defect_type": "初始问题"}, headers=headers,
    ).json()["id"]

    state = client.get(f"/api/issues/{issue_id}/draft", headers=headers)
    assert state.status_code == 200
    assert state.json()["draft"] is None
    saved = client.put(
        f"/api/issues/{issue_id}/draft",
        json={
            "payload": {"defect_desc": "仅草稿"},
            "base_version_id": state.json()["current_version_id"],
            "base_updated_at": state.json()["current_updated_at"],
        },
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["draft"]["payload"] == {"defect_desc": "仅草稿"}
    assert client.patch(
        f"/api/issues/{issue_id}", json={"defect_desc": "正式更新"}, headers=headers,
    ).status_code == 200

    stale = client.put(
        f"/api/issues/{issue_id}/draft",
        json={
            "payload": {"defect_desc": "不应覆盖"},
            "base_version_id": state.json()["current_version_id"],
            "base_updated_at": state.json()["current_updated_at"],
        },
        headers=headers,
    )
    assert stale.status_code == 409
