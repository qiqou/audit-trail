"""资源边界回归用例（审查 F-07）。

通过 monkeypatch 把阈值调小，验证超限拒绝逻辑（不真传 500MB 文件）。
"""

import io
import zipfile

import limits
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, name="张三"):
    r = client.post("/api/session", json={"operator": name})
    return r.json()["token"]


def _open_project(client, token, tmp_path):
    p = tmp_path / "项目A"
    client.post("/api/project/create", json={"path": str(p), "name": "项目A"},
                headers={"X-Session": token})
    client.post("/api/units", json={"name": "单位A"}, headers={"X-Session": token})


def test_f07_upload_file_too_large(client, tmp_path, monkeypatch):
    """单文件超过上限时上传被拒，且不落库。"""
    monkeypatch.setattr(limits, "MAX_FILE_SIZE", 1024)  # 1KB 阈值
    t = _login(client)
    _open_project(client, t, tmp_path)
    r = client.post("/api/units/1/files",
                    files={"file": ("big.pdf", b"x" * 2048, "application/pdf")},
                    headers={"X-Session": t})
    assert r.status_code == 400
    assert "上限" in r.json()["detail"]
    files = client.get("/api/units/1/files", headers={"X-Session": t})
    assert files.json() == [], "超限文件不应入库"


def test_f07_folder_upload_batch_too_many(client, tmp_path, monkeypatch):
    """文件夹上传文件数超过上限被拒。"""
    monkeypatch.setattr(limits, "MAX_BATCH_FILES", 3)
    t = _login(client)
    _open_project(client, t, tmp_path)
    files = [("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(5)]
    r = client.post("/api/units/1/folder-upload",
                    data={"folder_name": "证据包"},
                    files=files,
                    headers={"X-Session": t})
    assert r.status_code == 400
    assert "单批" in r.json()["detail"]


def test_f07_import_excel_too_many_rows(client, tmp_path, monkeypatch):
    """Excel 导入行数超过上限被拒。"""
    monkeypatch.setattr(limits, "MAX_IMPORT_ROWS", 5)
    t = _login(client)
    _open_project(client, t, tmp_path)

    # 造一个 8 行数据的 xlsx（表头 + 8 数据行）
    from openpyxl import Workbook
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "导入模板"
    ws.append(["被审计单位*", "所属版块*", "缺陷定性*", "缺陷描述"])
    for i in range(8):
        ws.append([f"单位{i}", "营销管理", "问题类型", "描述"])
    wb.save(buf)
    buf.seek(0)

    r = client.post("/api/import/excel",
                    files={"file": ("导入.xlsx", buf.getvalue(),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    headers={"X-Session": t})
    assert r.status_code == 400
    assert "行数超过上限" in r.json()["detail"]
    units = client.get("/api/units", headers={"X-Session": t}).json()
    assert [unit["name"] for unit in units] == ["单位A"], "超限导入不得留下已解析的前几行"
    assert client.get("/api/units/1/issues", headers={"X-Session": t}).json() == []


def test_f07_restore_zip_bomb_blocked(client, tmp_path, monkeypatch):
    """备份恢复：解压总量超过上限被拒，目标目录不留半成品。"""
    monkeypatch.setattr(limits, "MAX_EXTRACT_TOTAL", 1024)
    t = _login(client)

    # 造一个成员总大小超限的 zip
    bad = tmp_path / "bomb.auditbak"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("audit.db", b"x" * 4096)
    target = tmp_path / "恢复目标"

    r = client.post("/api/backup/restore",
                    files={"file": ("bomb.auditbak", bad.read_bytes(), "application/zip")},
                    data={"target_dir": str(target)},
                    headers={"X-Session": t})
    assert r.status_code == 400
    assert "解压总量" in r.json()["detail"]
    assert not target.exists(), "解压炸弹不应留下目标目录"


def test_f07_merge_too_many_backups(client, tmp_path, monkeypatch):
    """合并导入：单批备份数超过上限被拒。"""
    monkeypatch.setattr(limits, "MAX_BATCH_FILES", 2)
    t = _login(client)
    _open_project(client, t, tmp_path)
    files = [("files", (f"b{i}.auditbak", b"x", "application/zip")) for i in range(4)]
    r = client.post("/api/import/merge", files=files, headers={"X-Session": t})
    assert r.status_code == 400
    assert "单批" in r.json()["detail"]
