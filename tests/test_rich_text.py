"""v1.3 富文本底稿：安全规范化、纯文本投影和版本兼容。"""

import sqlite3

from database import SCHEMA_VERSION, AuditProject
from rich_text import rich_html_to_plain_text, sanitize_rich_html


def test_rich_text_sanitizer_keeps_supported_formatting_and_removes_unsafe_markup():
    raw = (
        '<p>审计<span style="color:#ff0000;position:fixed"><strong>发现</strong></span></p>'
        '<script>alert(1)</script><img src=x onerror=alert(2)><font face="SimSun" size="4">依据</font>'
    )
    cleaned = sanitize_rich_html(raw)

    assert '<p>' in cleaned
    assert '<strong>发现</strong>' in cleaned
    assert 'color:#ff0000' in cleaned
    assert 'position' not in cleaned
    assert '<script' not in cleaned and '<img' not in cleaned and 'onerror' not in cleaned
    assert '<span style="font-family:SimSun;font-size:16px">依据</span>' in cleaned
    assert rich_html_to_plain_text(cleaned) == "审计发现\nalert(1)依据"


def test_rich_text_sanitizer_keeps_plain_typing_boundary_after_bold_text():
    """编辑器尾部的 normal span 必须能保存，避免重开后继承前一段粗体。"""
    cleaned = sanitize_rich_html('<p><strong>已加粗内容</strong></p><p><span style="font-weight:normal"><br></span></p>')

    assert cleaned.endswith('<p><span style="font-weight:normal"><br></span></p>')
    assert rich_html_to_plain_text(cleaned) == "已加粗内容"


def test_rich_text_sanitizer_keeps_auditable_table_structure_but_rejects_unsafe_style():
    cleaned = sanitize_rich_html(
        '<table style="width:75%;height:180px;position:fixed"><tbody><tr><th colspan="2">表头</th></tr>'
        '<tr><td>合同</td><td rowspan="2">缺失</td></tr></tbody></table>'
    )

    assert '<table style="width:75%;height:180px">' in cleaned
    assert '<th colspan="2">表头</th>' in cleaned
    assert '<td rowspan="2">缺失</td>' in cleaned
    assert "position" not in cleaned
    assert rich_html_to_plain_text(cleaned) == "表头\n合同 | 缺失"


def test_rich_text_issue_stores_plain_projection_and_keeps_auditable_snapshot(proj):
    unit_id = proj.add_unit("单位A", "张三")
    issue_id = proj.add_issue(
        unit_id,
        "张三",
        defect_type="收入确认",
        defect_desc_rich='<p>收入 <strong>截止</strong>测试</p><table><tr><td>合同</td><td>缺失</td></tr></table>',
    )

    issue = proj.get_issue(issue_id)
    assert issue["defect_desc"] == "收入 截止测试\n合同 | 缺失"
    assert "<strong>截止</strong>" in issue["defect_desc_rich"]
    snapshot = proj.list_versions(issue_id)[0]["snapshot"]
    assert snapshot["defect_desc"] == issue["defect_desc"]
    assert snapshot["defect_desc_rich"] == issue["defect_desc_rich"]

    proj.update_issue(issue_id, "李四", defect_desc="改为纯文本")
    current = proj.get_issue(issue_id)
    assert current["defect_desc"] == "改为纯文本"
    assert current["defect_desc_rich"] == ""


def test_plain_text_edit_clears_stale_rich_view_without_deleting_history(proj):
    unit_id = proj.add_unit("单位A", "张三")
    issue_id = proj.add_issue(
        unit_id,
        "张三",
        defect_type="旧格式底稿",
        defect_desc_rich="<p><strong>旧的富文本</strong></p>",
    )
    version_count_before = len(proj.list_versions(issue_id))

    assert proj.update_issue(issue_id, "李四", defect_desc="改为普通多行文本") is True
    current = proj.get_issue(issue_id)
    assert current["defect_desc"] == "改为普通多行文本"
    assert current["defect_desc_rich"] == ""
    assert len(proj.list_versions(issue_id)) == version_count_before + 1


def test_v14_project_adds_rich_text_columns_without_losing_plain_text(tmp_path):
    root = tmp_path / "v14项目"
    original = AuditProject(root)
    unit_id = original.add_unit("单位A", "张三")
    issue_id = original.add_issue(unit_id, "张三", defect_type="旧底稿", defect_desc="旧描述")
    original.close()

    connection = sqlite3.connect(root / "audit.db")
    try:
        for column in ("defect_desc_rich", "regulation_basis_rich", "suggestion_rich"):
            connection.execute(f"ALTER TABLE issues DROP COLUMN {column}")
        connection.execute("UPDATE meta SET value='14' WHERE key='schema_version'")
        connection.commit()
    finally:
        connection.close()

    upgraded = AuditProject(root)
    try:
        issue = upgraded.get_issue(issue_id)
        assert issue["defect_desc"] == "旧描述"
        assert issue["defect_desc_rich"] == ""
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION)
    finally:
        upgraded.close()
