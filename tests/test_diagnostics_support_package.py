"""诊断支持包：只含匿名化运行摘要，不能夹带业务数据。"""

import json

from fastapi.testclient import TestClient

from main import app


def test_support_package_excludes_sensitive_project_data(tmp_path):
    sensitive_project = "项目-不得外发"
    sensitive_unit = "单位-不得外发"
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "人员-不得外发"}).json()["token"]
        headers = {"X-Session": token}
        assert client.post("/api/project/create", json={"path": str(tmp_path / sensitive_project)}, headers=headers).status_code == 200
        assert client.post("/api/units", json={"name": sensitive_unit}, headers=headers).status_code == 200
        response = client.post("/api/diagnostics/support-package", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"filename", "download_url"}
        downloaded = client.get(payload["download_url"], headers=headers)

    assert downloaded.status_code == 200
    package = json.loads(downloaded.content)
    serialized = json.dumps(package, ensure_ascii=False)
    assert sensitive_project not in serialized
    assert sensitive_unit not in serialized
    assert "人员-不得外发" not in serialized
    assert str(tmp_path) not in serialized
    assert package["privacy"]["excluded"] == [
        "project_name", "unit_names", "operator_names", "issue_content",
        "attachment_names", "attachment_paths", "attachment_content",
    ]
    assert package["audit_log_chain"]["ok"] is True
