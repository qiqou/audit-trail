"""交流模式写端点 API 级链路测试（审查 F4 补测）。

覆盖前端 client.ts:672-707 调用的全部写端点：start → propose → comment
→ request → update request → close → 再开一轮看到历史记录；以及无会话拒绝
与字段校验失败。请求体字段名与后端 Pydantic 模型的漂移在这里被拦截。
"""

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _login(client, name="交流测试员") -> str:
    return client.post("/api/session", json={"operator": name}).json()["token"]


def _project_with_issue(client, tmp_path, defect_desc="原始描述"):
    headers = {"X-Session": _login(client)}
    assert client.post(
        "/api/project/create", json={"path": str(tmp_path / "交流项目")}, headers=headers
    ).status_code == 200
    unit_id = client.post("/api/units", json={"name": "单位A"}, headers=headers).json()["id"]
    issue_id = client.post(
        f"/api/units/{unit_id}/issues",
        json={"defect_desc": defect_desc, "department": "采购"},
        headers=headers,
    ).json()["id"]
    return headers, issue_id


def test_exchange_write_api_full_round(client, tmp_path):
    """完整一轮：发起→修订→批注→待补资料→更新资料→结束→再开一轮看历史。"""
    headers, issue_id = _project_with_issue(client, tmp_path)

    started = client.post(f"/api/issues/{issue_id}/exchange", headers=headers)
    assert started.status_code == 200
    suuid = started.json()["session_uuid"]
    assert started.json()["status"] == "open"

    # 提修订
    revised = client.post(
        f"/api/exchanges/{suuid}/revisions",
        json={"field_name": "defect_desc", "new_value": "修订后描述", "reason": "现场补充"},
        headers=headers,
    )
    assert revised.status_code == 200
    assert revised.json()["issue"]["defect_desc"] == "修订后描述"
    assert len(revised.json()["revisions"]) == 1
    assert revised.json()["revisions"][0]["old_value"] == "原始描述"

    # 批注
    commented = client.post(
        f"/api/exchanges/{suuid}/comments",
        json={"body": "同意修订", "anchor_field": "defect_desc"},
        headers=headers,
    )
    assert commented.status_code == 200
    assert commented.json()["comments"][-1]["body"] == "同意修订"

    # 待补资料
    requested = client.post(
        f"/api/exchanges/{suuid}/requests", json={"content": "补充采购审批单"}, headers=headers
    )
    assert requested.status_code == 200
    request_uuid = requested.json()["requests"][-1]["request_uuid"]

    # 更新待补资料状态：标记已提供但未关联附件 → 400（契约校验）
    bad_update = client.patch(
        f"/api/exchanges/{suuid}/requests/{request_uuid}",
        json={"status": "provided"},
        headers=headers,
    )
    assert bad_update.status_code == 400
    # 撤回（withdrawn）→ 200
    withdrawn = client.patch(
        f"/api/exchanges/{suuid}/requests/{request_uuid}",
        json={"status": "withdrawn", "note": "附件已另附"},
        headers=headers,
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["requests"][-1]["status"] == "withdrawn"

    # 结束本轮 → 生成版本
    closed = client.post(
        f"/api/exchanges/{suuid}/close", json={"note": "本轮结束"}, headers=headers
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    versions = closed.json()["revisions"]
    assert all(v["version_id"] is not None for v in versions), "结束后修订应绑定版本"

    # 再开一轮 → 历史记录完整可见
    second = client.post(f"/api/issues/{issue_id}/exchange", headers=headers)
    assert second.status_code == 200
    payload = second.json()
    assert payload["session_uuid"] != suuid
    assert payload["revisions"][0]["new_value"] == "修订后描述"
    assert any(c["body"] == "同意修订" for c in payload["comments"])
    assert any(req["content"] == "补充采购审批单" for req in payload["requests"])


def test_exchange_api_requires_session(client, tmp_path):
    """无会话访问交流写端点 → 400/401。"""
    r = client.post("/api/exchanges/abc/revisions", json={"field_name": "defect_desc", "new_value": "x"})
    assert r.status_code in (400, 401)


def test_exchange_api_invalid_field_rejected(client, tmp_path):
    """空字段名 → 400（契约校验）。"""
    headers, issue_id = _project_with_issue(client, tmp_path)
    suuid = client.post(f"/api/issues/{issue_id}/exchange", headers=headers).json()["session_uuid"]
    r = client.post(
        f"/api/exchanges/{suuid}/revisions",
        json={"field_name": "", "new_value": "x"},
        headers=headers,
    )
    assert r.status_code == 400


def test_exchange_api_empty_comment_rejected(client, tmp_path):
    """空批注 → 400。"""
    headers, issue_id = _project_with_issue(client, tmp_path)
    suuid = client.post(f"/api/issues/{issue_id}/exchange", headers=headers).json()["session_uuid"]
    r = client.post(
        f"/api/exchanges/{suuid}/comments", json={"body": "   "}, headers=headers
    )
    assert r.status_code == 400
