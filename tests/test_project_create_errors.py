"""项目创建失败必须给现场人员可执行提示，而非 HTTP 500。"""

from fastapi.testclient import TestClient

import main
from main import app


def _login(client: TestClient) -> dict[str, str]:
    response = client.post("/api/session", json={"operator": "张三"})
    assert response.status_code == 200
    return {"X-Session": response.json()["token"]}


def test_create_project_reports_unwritable_directory_as_client_error(monkeypatch, tmp_path):
    def denied_project(_path):
        raise PermissionError("没有写入权限")

    monkeypatch.setattr(main, "AuditProject", denied_project)
    with TestClient(app) as client:
        response = client.post(
            "/api/project/create",
            json={"path": str(tmp_path / "不可写项目"), "name": "测试项目"},
            headers=_login(client),
        )

    assert response.status_code == 400
    assert "无法创建项目" in response.json()["detail"]
    assert "可写" in response.json()["detail"]
