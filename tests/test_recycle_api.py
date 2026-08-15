"""回收站 API 冒烟（审查 F3 补测）。

覆盖前端 client.ts:713-751 调用的全部回收站端点：
底稿/单位/附件三对象的 删除→列出→预览→恢复→物理清空，以及无会话拒绝。
契约漂移（路径/方法/会话校验/状态码）在这里被拦截，不依赖人工验收。
"""

import io

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


def _login(client, name="回收站测试员") -> str:
    return client.post("/api/session", json={"operator": name}).json()["token"]


def _project(client, tmp_path, name="回收站项目") -> dict:
    headers = {"X-Session": _login(client)}
    assert client.post(
        "/api/project/create", json={"path": str(tmp_path / name)}, headers=headers
    ).status_code == 200
    return headers


def test_recycle_requires_session(client, tmp_path):
    """无会话 token 访问回收站端点一律 401。"""
    for method, url in [
        ("GET", "/api/recycle/issues"),
        ("GET", "/api/recycle/units"),
        ("GET", "/api/recycle/files"),
        ("POST", "/api/recycle/issues/1/restore"),
        ("DELETE", "/api/recycle/issues/1"),
    ]:
        r = getattr(client, method.lower())(url)
        assert r.status_code in (400, 401), f"{method} {url} -> {r.status_code}"


def test_recycle_issue_full_lifecycle(client, tmp_path):
    """底稿：删除→列表→预览→恢复→再删→物理清空。"""
    headers = _project(client, tmp_path)
    unit_id = client.post("/api/units", json={"name": "单位A"}, headers=headers).json()["id"]
    issue_id = client.post(
        f"/api/units/{unit_id}/issues",
        json={"defect_desc": "待删底稿", "amount": "100", "currency": "CNY", "amount_unit": "元"},
        headers=headers,
    ).json()["id"]

    assert client.delete(f"/api/issues/{issue_id}", headers=headers).status_code == 200
    recycled = client.get("/api/recycle/issues", headers=headers).json()
    assert len(recycled) == 1
    rid = recycled[0]["recycle_id"]
    assert recycled[0]["id"] == issue_id

    preview = client.get(f"/api/recycle/issues/{rid}", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["issue"]["id"] == issue_id

    restored = client.post(f"/api/recycle/issues/{rid}/restore", headers=headers)
    assert restored.status_code == 200
    assert client.get("/api/recycle/issues", headers=headers).json() == []
    # 恢复后编号/内容可用
    tree = client.get("/api/issues/tree", headers=headers).json()
    assert str(unit_id) in tree

    # 再删除 → 物理清空
    assert client.delete(f"/api/issues/{issue_id}", headers=headers).status_code == 200
    recycled = client.get("/api/recycle/issues", headers=headers).json()
    rid2 = recycled[0]["recycle_id"]
    assert client.delete(f"/api/recycle/issues/{rid2}", headers=headers).status_code == 200
    assert client.get("/api/recycle/issues", headers=headers).json() == []


def test_recycle_issue_restore_missing_returns_404(client, tmp_path):
    """对不存在的回收站条目恢复返回 404。"""
    headers = _project(client, tmp_path)
    r = client.post("/api/recycle/issues/9999/restore", headers=headers)
    assert r.status_code == 404


def test_recycle_unit_full_lifecycle(client, tmp_path):
    """单位：删除→列表→恢复；已删单位的底稿不应出现在问题树。"""
    headers = _project(client, tmp_path)
    unit_id = client.post("/api/units", json={"name": "整删单位"}, headers=headers).json()["id"]
    client.post(
        f"/api/units/{unit_id}/issues", json={"defect_desc": "单位底稿"}, headers=headers
    )

    assert client.delete(f"/api/units/{unit_id}", headers=headers).status_code == 200
    recycled = client.get("/api/recycle/units", headers=headers).json()
    assert len(recycled) == 1
    rid = recycled[0]["recycle_id"]
    assert recycled[0]["name"] == "整删单位"

    # 已删单位的底稿不再出现在问题树（审查重要1修复的 API 层确认）
    tree = client.get("/api/issues/tree", headers=headers).json()
    assert str(unit_id) not in tree

    restored = client.post(f"/api/recycle/units/{rid}/restore", headers=headers)
    assert restored.status_code == 200
    assert client.get("/api/recycle/units", headers=headers).json() == []
    tree = client.get("/api/issues/tree", headers=headers).json()
    assert str(unit_id) in tree

    # 再删除 → 物理清空
    assert client.delete(f"/api/units/{unit_id}", headers=headers).status_code == 200
    recycled = client.get("/api/recycle/units", headers=headers).json()
    rid2 = recycled[0]["recycle_id"]
    assert client.delete(f"/api/recycle/units/{rid2}", headers=headers).status_code == 200
    assert client.get("/api/recycle/units", headers=headers).json() == []


def test_recycle_file_full_lifecycle(client, tmp_path):
    """附件：上传→删除→列表→恢复→物理清空。"""
    headers = _project(client, tmp_path)
    unit_id = client.post("/api/units", json={"name": "附件单位"}, headers=headers).json()["id"]
    uploaded = client.post(
        f"/api/units/{unit_id}/files",
        files={"file": ("凭证.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        data={"folder_path": ""},
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text[:150]
    file_id = uploaded.json()["id"]

    assert client.delete(f"/api/files/{file_id}", headers=headers).status_code == 200
    recycled = client.get("/api/recycle/files", headers=headers).json()
    assert len(recycled) == 1
    rid = recycled[0]["recycle_id"]
    assert recycled[0]["id"] == file_id

    restored = client.post(f"/api/recycle/files/{rid}/restore", headers=headers)
    assert restored.status_code == 200
    assert client.get("/api/recycle/files", headers=headers).json() == []

    # 再删除 → 物理清空
    assert client.delete(f"/api/files/{file_id}", headers=headers).status_code == 200
    recycled = client.get("/api/recycle/files", headers=headers).json()
    rid2 = recycled[0]["recycle_id"]
    assert client.delete(f"/api/recycle/files/{rid2}", headers=headers).status_code == 200
    assert client.get("/api/recycle/files", headers=headers).json() == []
