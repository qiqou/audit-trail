"""验收演练（审查报告第 4 阶段）：样本数据完整闭环。

规模：10 个单位 × 20 条底稿 = 200 条，含重复内容附件、跨单位引用、大附件。
闭环：创建 → 导入（Excel）→ 编辑（部分更新）→ 导出 Excel → 打包 ZIP
      → 备份 → 恢复 → 数据一致性核对 → 输出不覆盖。
"""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _login(client, name="验收员"):
    return client.post("/api/session", json={"operator": name}).json()["token"]


def _h(token):
    return {"X-Session": token}


def _make_import_xlsx(n_units=10, per_unit=20):
    """造导入模板 xlsx：n_units 个单位 × per_unit 条底稿。"""
    from openpyxl import Workbook

    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "导入模板"
    ws.append(["被审计单位*", "所属版块*", "缺陷定性*", "缺陷描述",
               "问题金额", "制度依据", "审计建议", "编写人", "审核人"])
    for u in range(1, n_units + 1):
        for i in range(1, per_unit + 1):
            ws.append([f"华电集团{['XX','YY','ZZ','AB','CD','EF','GH','IJ','KL','MN'][u-1]}电厂",
                       f"版块{['营销管理','安全生产','财务管理','合规管理'][i % 4]}",
                       f"缺陷类型{i}",
                       f"第{u}单位第{i}条缺陷描述",
                       f"{i * 10}万",
                       "《制度》第X条",
                       f"建议{u}-{i}",
                       "张三", "李四"])
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def test_acceptance_full_loop(client, tmp_path):
    t = _login(client)
    project_path = tmp_path / "2026专项审计"
    # 1) 创建项目
    r = client.post("/api/project/create", json={"path": str(project_path), "name": "2026专项审计"},
                    headers=_h(t))
    assert r.status_code == 200
    # 目录伪装：创建后目录名带 .auditproj，后续路径断言用接口返回的实际路径
    project_path = Path(r.json()["path"])

    # 2) Excel 批量导入 10×20 = 200 条
    xlsx = _make_import_xlsx()
    r = client.post("/api/import/excel",
                    files={"file": ("导入.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    headers=_h(t))
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 200
    units = client.get("/api/units", headers=_h(t)).json()
    assert len(units) == 10
    total_issues = sum(len(client.get(f"/api/units/{u['id']}/issues", headers=_h(t)).json()) for u in units)
    assert total_issues == 200

    # 3) 上传附件：重复内容（查重）+ 大附件 + 跨单位引用
    dup_content = b"%PDF evidence v1"
    r1 = client.post(f"/api/units/{units[0]['id']}/files",
                     files={"file": ("证据A.pdf", dup_content, "application/pdf")}, headers=_h(t))
    assert r1.status_code == 200
    fid_a = r1.json()["id"]
    # 同内容再传 → 查重复用
    r2 = client.post(f"/api/units/{units[1]['id']}/files",
                     files={"file": ("证据A副本.pdf", dup_content, "application/pdf")}, headers=_h(t))
    assert r2.status_code == 200 and r2.json()["duplicated"] is True
    # 大附件 ~1.5MB，并关联到单位0 的第一条底稿（验证打包包含大附件）
    r3 = client.post(f"/api/units/{units[0]['id']}/files",
                     files={"file": ("大附件.bin", b"x" * (1_500_000), "application/octet-stream")},
                     headers=_h(t))
    assert r3.status_code == 200
    fid_big = r3.json()["id"]
    iss0_first = client.get(f"/api/units/{units[0]['id']}/issues", headers=_h(t)).json()[0]
    r = client.post(f"/api/issues/{iss0_first['id']}/files/{fid_big}/link", headers=_h(t))
    assert r.status_code == 200
    # 单位0 的附件跨单位关联到单位1 的底稿（跨单位引用）
    iss1_first = client.get(f"/api/units/{units[1]['id']}/issues", headers=_h(t)).json()[0]
    r = client.post(f"/api/issues/{iss1_first['id']}/files/{fid_a}/link", headers=_h(t))
    assert r.status_code == 200

    # 4) 编辑：部分字段更新（F-02 验证）
    iss0_first = client.get(f"/api/units/{units[0]['id']}/issues", headers=_h(t)).json()[0]
    r = client.patch(f"/api/issues/{iss0_first['id']}", json={"amount": "999万"}, headers=_h(t))
    assert r.status_code == 200 and r.json()["changed"] is True
    got = client.get(f"/api/issues/{iss0_first['id']}", headers=_h(t)).json()
    assert got["amount"] == "999万" and got["department"] != ""

    # 5) 删除被跨单位引用的单位 → 保护阻止（F-01 验证）
    r = client.delete(f"/api/units/{units[0]['id']}", headers=_h(t))
    assert r.status_code == 400

    # 6) 导出 Excel（全部单位）
    r = client.post("/api/export/excel", json={"scope": "project"}, headers=_h(t))
    assert r.status_code == 200
    assert r.json()["count"] == 200
    xlsx_out = project_path / "输出" / r.json()["filename"]
    assert xlsx_out.exists()

    # 7) 打包 ZIP（按版块分类）
    r = client.post("/api/export/package", json={"scope": "all", "group_by_dept": True}, headers=_h(t))
    assert r.status_code == 200
    zip_out = project_path / "输出" / r.json()["filename"]
    with zipfile.ZipFile(zip_out) as zf:
        names = zf.namelist()
        assert any("审计问题汇总.xlsx" in n for n in names)
        assert any("证据A.pdf" in n for n in names), "跨单位引用附件应打包"
        assert any("大附件.bin" in n for n in names)
        assert sum(i.file_size for i in zf.infolist()) >= 1_500_000

    # 8) 备份 → 恢复 → 一致性核对
    r = client.post("/api/backup/create", headers=_h(t))
    assert r.status_code == 200
    bak_path = tmp_path / r.json()["filename"]
    assert bak_path.exists()

    restore_target = tmp_path / "恢复项目"
    with open(bak_path, "rb") as fh:
        r = client.post("/api/backup/restore",
                        files={"file": ("backup.auditbak", fh.read(), "application/zip")},
                        data={"target_dir": str(restore_target)}, headers=_h(t))
    assert r.status_code == 200, r.text
    # 恢复目标自动加 .auditproj 后缀；打开接口会按原路径自动补后缀
    restore_actual = restore_target.with_name(restore_target.name + ".auditproj")
    assert restore_actual.is_dir(), "恢复项目应落在 .auditproj 伪装目录"
    # 打开恢复项目核对
    r = client.post("/api/project/open", json={"path": str(restore_target)}, headers=_h(t))
    assert r.status_code == 200
    units_r = client.get("/api/units", headers=_h(t)).json()
    assert len(units_r) == 10
    issues_r = sum(len(client.get(f"/api/units/{u['id']}/issues", headers=_h(t)).json()) for u in units_r)
    assert issues_r == 200, "恢复后底稿数一致"
    # 恢复项目的附件库有 unit_{id} 结构
    assert (restore_actual / "附件库" / "unit_1").is_dir()
    # 跨单位引用在恢复后仍存在
    iss_r = client.get(f"/api/units/{units_r[1]['id']}/issues", headers=_h(t)).json()
    files_r = client.get(f"/api/issues/{iss_r[0]['id']}/files", headers=_h(t)).json()
    assert files_r, "恢复后跨单位引用附件仍在"

    # 9) 输出不覆盖（F-04 验证：同秒两次导出文件名不同）
    r1 = client.post("/api/export/excel", json={"scope": "project"}, headers=_h(t))
    r2 = client.post("/api/export/excel", json={"scope": "project"}, headers=_h(t))
    assert r1.json()["filename"] != r2.json()["filename"]
