"""排序 HTTP 合同：验证会话、完整 ID 校验和持久化显示顺序。"""

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/session", json={"operator": "张三"})
    assert response.status_code == 200
    return {"X-Session": response.json()["token"]}


def test_ordering_endpoints_keep_issue_numbers(client: TestClient, tmp_path):
    headers = _headers(client)
    assert client.post(
        "/api/project/create", json={"path": str(tmp_path / "排序项目")}, headers=headers,
    ).status_code == 200
    unit_ids = [
        client.post("/api/units", json={"name": f"单位{suffix}"}, headers=headers).json()["id"]
        for suffix in ("A", "B", "C")
    ]

    assert client.put("/api/units/order", json={"ids": [unit_ids[2], unit_ids[0], unit_ids[1]]}, headers=headers).json() == {
        "changed": True,
    }
    assert [unit["id"] for unit in client.get("/api/units", headers=headers).json()] == [
        unit_ids[2], unit_ids[0], unit_ids[1],
    ]
    bad_order = client.put("/api/units/order", json={"ids": [unit_ids[0]]}, headers=headers)
    assert bad_order.status_code == 400

    issue_ids = [
        client.post(
            f"/api/units/{unit_ids[0]}/issues", json={"defect_type": f"问题{suffix}"}, headers=headers,
        ).json()["id"]
        for suffix in ("A", "B", "C")
    ]
    before = client.get(f"/api/units/{unit_ids[0]}/issues", headers=headers).json()
    before_seq = {issue["id"]: issue["seq"] for issue in before}
    reordered = client.put(
        f"/api/units/{unit_ids[0]}/issues/order",
        json={"ids": [issue_ids[2], issue_ids[0], issue_ids[1]]},
        headers=headers,
    )
    assert reordered.json() == {"changed": True}
    after = client.get(f"/api/units/{unit_ids[0]}/issues", headers=headers).json()
    assert [issue["id"] for issue in after] == [issue_ids[2], issue_ids[0], issue_ids[1]]
    assert {issue["id"]: issue["seq"] for issue in after} == before_seq
