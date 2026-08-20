"""确认单 DOCX：标准 OOXML 包、字段来自当前正式底稿且下载路径受限。"""

from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient

from main import app


def test_issue_confirmation_docx_contains_current_issue_fields(tmp_path):
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "张三"}).json()["token"]
        headers = {"X-Session": token}
        assert client.post("/api/project/create", json={"path": str(tmp_path / "确认单")}, headers=headers).status_code == 200
        unit = client.post("/api/units", json={"name": "甲单位"}, headers=headers).json()["id"]
        issue = client.post(f"/api/units/{unit}/issues", json={"department": "财务", "defect_type": "收入确认", "defect_desc": "描述内容", "suggestion": "整改建议"}, headers=headers).json()["id"]
        response = client.post(f"/api/issues/{issue}/confirmation-docx", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["filename"].endswith(".docx")
        downloaded = client.get(payload["download_url"], headers=headers)

    with ZipFile(BytesIO(downloaded.content)) as archive:
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}.issubset(archive.namelist())
        document = archive.read("word/document.xml").decode("utf-8")
    assert "甲单位" in document
    assert "收入确认" in document
    assert "整改建议" in document
