import pytest

from database import SCHEMA_VERSION, AuditProject


def test_workpaper_template_reuses_only_common_content_and_leaves_audit_boundary_clean(proj, tmp_path):
    source_unit = proj.add_unit("单位A", "张三")
    target_unit = proj.add_unit("单位B", "张三")
    source_id = proj.add_issue(
        source_unit, "张三", department="收入", category="截止", defect_type="收入截止测试",
        defect_desc="期末收入截止存在异常", amount="120.00", currency="CNY", amount_unit="元",
        regulation_basis="收入确认制度", suggestion="补充截止测试", author="原编制人", reviewer="原复核人",
    )
    evidence_path = tmp_path / "凭证.pdf"
    evidence_path.write_bytes(b"evidence")
    evidence = proj.add_file(source_unit, evidence_path, "张三")
    proj.link_file(source_id, evidence["id"], "张三")

    template = proj.create_workpaper_template("收入截止测试模板", source_id, "张三")

    assert template["data"]["defect_desc"] == "期末收入截止存在异常"
    assert "author" not in template["data"] and "status" not in template["data"]
    with pytest.raises(ValueError, match="模板名称已存在"):
        proj.create_workpaper_template("收入截止测试模板", source_id, "张三")

    copied = proj.create_issue_from_template(template["id"], target_unit, "李四")

    assert copied["unit_id"] == target_unit and copied["status"] == "草稿"
    assert copied["defect_type"] == "收入截止测试"
    assert copied["author"] == "李四" and copied["reviewer"] == ""
    assert proj.files_for_issue(copied["id"]) == []
    actions = [item["action"] for item in proj.list_logs()]
    assert "保存底稿模板" in actions and "按模板新建底稿" in actions

    proj.delete_workpaper_template(template["id"], "李四")
    assert proj.list_workpaper_templates() == []
    assert any(item["action"] == "删除底稿模板" for item in proj.list_logs())


def test_v16_project_gets_template_table_with_snapshot_and_preserves_issues(tmp_path):
    root = tmp_path / "v16模板迁移"
    project = AuditProject(root)
    unit_id = project.add_unit("单位A", "张三")
    issue_id = project.add_issue(unit_id, "张三", department="财务", defect_type="旧底稿", defect_desc="旧正文")
    with project._lock, project._conn:
        project._conn.execute("DROP TABLE workpaper_templates")
        project._conn.execute("UPDATE meta SET value='16' WHERE key='schema_version'")
    project.close()

    upgraded = AuditProject(root)
    try:
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert upgraded.get_issue(issue_id)["defect_desc"] == "旧正文"
        assert upgraded._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workpaper_templates'"
        ).fetchone()
        assert list((root / "快照").glob("pre_migration_v16_*.db"))
    finally:
        upgraded.close()
