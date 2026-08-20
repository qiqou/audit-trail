"""操作日志导出：字段稳定、只含日志且下载路径受限。"""

from fastapi.testclient import TestClient

from main import app


def test_audit_log_export_creates_downloadable_csv(tmp_path):
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "张三"}).json()["token"]
        headers = {"X-Session": token}
        assert client.post("/api/project/create", json={"path": str(tmp_path / "日志项目")}, headers=headers).status_code == 200
        assert client.post("/api/units", json={"name": "甲单位"}, headers=headers).status_code == 200
        exported = client.post("/api/logs/export", headers=headers)
        assert exported.status_code == 200
        body = exported.json()
        assert body["filename"].endswith(".csv")
        assert body["count"] >= 2
        downloaded = client.get(body["download_url"], headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content.decode("utf-8-sig").startswith("id,created_at,operator,action,target,detail")


def test_audit_log_filters_apply_to_list_and_export(tmp_path):
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "张三"}).json()["token"]
        headers = {"X-Session": token}
        assert client.post("/api/project/create", json={"path": str(tmp_path / "筛选项目")}, headers=headers).status_code == 200
        assert client.post("/api/units", json={"name": "甲单位"}, headers=headers).status_code == 200
        only_units = client.get("/api/logs", params={"action": "新建单位"}, headers=headers)
        assert only_units.status_code == 200
        assert only_units.json() and {item["action"] for item in only_units.json()} == {"新建单位"}
        exported = client.post("/api/logs/export", params={"action": "新建单位"}, headers=headers)
        assert exported.status_code == 200
        downloaded = client.get(exported.json()["download_url"], headers=headers)
    lines = downloaded.content.decode("utf-8-sig").splitlines()
    assert len(lines) == 2
    assert "新建单位" in lines[1]
