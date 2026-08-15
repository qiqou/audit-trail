"""本机备份合并预检：来源不落库、冲突显式确认、令牌防变化。"""

from database import AuditProject
from export import create_backup, merge_preflight
from fastapi.testclient import TestClient

from main import app


def _source_backup(tmp_path, name: str, unit_name: str, issue_name: str):
    source = AuditProject(tmp_path / name)
    try:
        unit_id = source.add_unit(unit_name, "来源人")
        source.add_issue(unit_id, "来源人", defect_type=issue_name)
        return create_backup(source)["abs_path"]
    finally:
        source.close()


def test_merge_preflight_is_read_only_and_reports_existing_unit_conflict(proj, tmp_path):
    target_unit = proj.add_unit("甲单位", "张三")
    proj.add_issue(target_unit, "张三", defect_type="目标底稿")
    backup = _source_backup(tmp_path, "来源项目", "甲单位", "来源底稿")

    result = merge_preflight(proj, [backup])

    assert result["ok"] is True
    assert any(item["type"] == "unit_exists" for item in result["conflicts"])
    assert proj.list_units()[0]["name"] == "甲单位"
    assert len(proj.list_issues(target_unit)) == 1


def test_local_merge_requires_preflight_token_and_rejects_stale_target(tmp_path):
    backup = _source_backup(tmp_path, "来源项目", "甲单位", "来源底稿")
    with TestClient(app) as client:
        token = client.post("/api/session", json={"operator": "张三"}).json()["token"]
        headers = {"X-Session": token}
        created = client.post(
            "/api/project/create", json={"path": str(tmp_path / "目标项目"), "name": "目标项目"}, headers=headers,
        )
        assert created.status_code == 200
        checked = client.post("/api/import/merge-local/preflight", json={"backup_paths": [backup]}, headers=headers)
        assert checked.status_code == 200
        confirmation_token = checked.json()["confirmation_token"]
        assert confirmation_token

        assert client.post("/api/units", json={"name": "核对后新增"}, headers=headers).status_code == 200
        merged = client.post(
            "/api/import/merge-local",
            json={"backup_paths": [backup], "confirmation_token": confirmation_token}, headers=headers,
        )
        assert merged.status_code == 409
        assert "重新预检" in merged.json()["detail"]


def test_merge_preflight_surfaces_file_and_preset_conflicts(proj, tmp_path):
    """P0：附件同名异内容、版块和分类预设差异必须在写入前展示。"""
    target_unit = proj.add_unit("甲单位", "张三")
    target_file = tmp_path / "目标证据.pdf"
    target_file.write_bytes(b"target")
    proj.add_file(target_unit, target_file, "张三", orig_name="同名证据.pdf")
    proj.set_meta("departments", '["经营"]')
    proj.set_meta("categories", '["管理"]')

    source = AuditProject(tmp_path / "来源冲突")
    try:
        source_unit = source.add_unit("甲单位", "李四")
        source_file = tmp_path / "来源证据.pdf"
        source_file.write_bytes(b"source-different")
        source.add_file(source_unit, source_file, "李四", orig_name="同名证据.pdf")
        source.set_meta("departments", '["经营", "安全"]')
        source.set_meta("categories", '["管理", "采购"]')
        backup = create_backup(source)["abs_path"]
    finally:
        source.close()

    result = merge_preflight(proj, [backup])
    types = {item["type"] for item in result["conflicts"]}
    assert {"file_same_name", "dept_merge", "category_merge"}.issubset(types)
