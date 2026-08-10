"""重置项目（清空全部数据）与重启程序接口测试（V3.2）。

- 重置：确认文字必须与项目名称一致（防误触）；清空单位/底稿/附件/日志，
  保留版块预设等配置；附件库与输出目录物理清空；留痕一条「重置项目」。
- 重启：接口返回 200 并触发调度（不真重启进程，monkeypatch 拦下）。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as main_module
from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, name="重置测试员"):
    r = client.post("/api/session", json={"operator": name})
    assert r.status_code == 200
    return r.json()["token"]


def _h(token):
    return {"X-Session": token}


def _create_project_with_data(client, t, tmp_path):
    """建项目 + 单位 + 底稿 + 版块预设，返回伪装目录路径。"""
    raw = tmp_path / "重置源项目"
    r = client.post("/api/project/create", json={"path": str(raw), "name": "重置源项目"}, headers=_h(t))
    assert r.status_code == 200
    created = Path(r.json()["path"])
    r2 = client.post("/api/units", json={"name": "华电集团XX电厂"}, headers=_h(t))
    assert r2.status_code == 200
    uid = r2.json()["id"]
    r3 = client.post(f"/api/units/{uid}/issues",
                     json={"defect_type": "问题A", "defect_desc": "重置前描述"}, headers=_h(t))
    assert r3.status_code == 200
    r4 = client.post("/api/settings/departments",
                     json={"departments": ["营销管理", "安全生产"]}, headers=_h(t))
    assert r4.status_code == 200
    return created


def test_reset_requires_project_name_confirmation(client, tmp_path):
    """确认文字必须与项目名称一致，否则 400 且数据不动。"""
    t = _login(client)
    _create_project_with_data(client, t, tmp_path)

    r = client.post("/api/project/reset", json={"confirm_text": "错误文字"}, headers=_h(t))
    assert r.status_code == 400
    assert "不一致" in r.json()["detail"]
    # 数据未被清空
    assert client.get("/api/units", headers=_h(t)).json(), "确认失败不应清空数据"

    r2 = client.post("/api/project/reset", json={"confirm_text": "重置源项目"}, headers=_h(t))
    assert r2.status_code == 200
    assert client.get("/api/units", headers=_h(t)).json() == []


def test_reset_clears_all_data(client, tmp_path):
    """重置后：单位/底稿/附件/输出全空，预设保留，留痕可查。"""
    t = _login(client)
    created = _create_project_with_data(client, t, tmp_path)
    # 手工造物理附件与旧导出文件
    att_unit = created / "附件库" / "unit_1"
    att_unit.mkdir(parents=True, exist_ok=True)
    (att_unit / "证据.pdf").write_bytes(b"evidence")
    (created / "输出" / "旧导出.xlsx").write_bytes(b"x")

    r = client.post("/api/project/reset", json={"confirm_text": "重置源项目"}, headers=_h(t))
    assert r.status_code == 200

    assert client.get("/api/units", headers=_h(t)).json() == []
    assert client.get("/api/issues/tree", headers=_h(t)).json() == {}
    # 附件库物理清空（目录保留，单位目录消失）
    assert not (created / "附件库" / "unit_1").exists()
    assert list((created / "附件库").iterdir()) == []
    # 输出目录清空
    assert list((created / "输出").iterdir()) == []
    # 版块预设保留（配置而非数据）
    depts = client.get("/api/settings/departments", headers=_h(t)).json()
    assert depts == ["营销管理", "安全生产"]
    # 留痕
    logs = client.get("/api/logs", headers=_h(t)).json()
    assert any(log["action"] == "重置项目" for log in logs)
    # 数据表核实：files / issue_versions 已清空
    from database import AuditProject

    proj = AuditProject(created)
    try:
        n_files = proj._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_versions = proj._conn.execute("SELECT COUNT(*) FROM issue_versions").fetchone()[0]
        n_issues = proj._conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    finally:
        proj.close()
    assert n_files == 0
    assert n_versions == 0
    assert n_issues == 0


def test_restart_endpoint_schedules(client, monkeypatch):
    """重启接口返回 200 并触发调度（不真重启进程）。"""
    t = _login(client)
    called = []
    monkeypatch.setattr(main_module, "_schedule_restart", lambda: called.append(True))
    r = client.post("/api/system/restart", headers=_h(t))
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert called == [True], "重启接口应触发重启调度"
