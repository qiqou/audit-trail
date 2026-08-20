"""Excel 导入必须先在隔离副本预检，再使用原文件和令牌原子提交。"""

from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from main import app


def _workbook_bytes(unit_name: str = "甲单位") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "导入模板"
    sheet.append(["被审计单位", "所属版块", "缺陷定性"])
    sheet.append([unit_name, "财务", "收入确认"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_excel_import_preflight_does_not_write_then_commit_once(tmp_path):
    content = _workbook_bytes()
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "张三"}).json()["token"]
        headers = {"X-Session": token}
        assert client.post("/api/project/create", json={"path": str(tmp_path / "导入预检")}, headers=headers).status_code == 200
        preflight = client.post(
            "/api/import/excel/preflight",
            files={"file": ("导入.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            headers=headers,
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["imported"] == 1
        assert client.get("/api/units", headers=headers).json() == []
        committed = client.post(
            "/api/import/excel/commit",
            params={"confirmation_token": preflight.json()["confirmation_token"]},
            files={"file": ("导入.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            headers=headers,
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["imported"] == 1
        assert len(client.get("/api/units", headers=headers).json()) == 1


def test_excel_import_commit_rejects_changed_file_or_target(tmp_path):
    content = _workbook_bytes()
    changed = _workbook_bytes("乙单位")
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "张三"}).json()["token"]
        headers = {"X-Session": token}
        assert client.post("/api/project/create", json={"path": str(tmp_path / "导入确认")}, headers=headers).status_code == 200
        preflight = client.post("/api/import/excel/preflight", files={"file": ("导入.xlsx", content)}, headers=headers)
        assert preflight.status_code == 200
        rejected = client.post(
            "/api/import/excel/commit", params={"confirmation_token": preflight.json()["confirmation_token"]},
            files={"file": ("导入.xlsx", changed)}, headers=headers,
        )
        assert rejected.status_code == 409
        assert client.get("/api/units", headers=headers).json() == []
