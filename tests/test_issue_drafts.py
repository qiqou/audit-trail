"""独立草稿：不污染正式版本，正式更新后拒绝静默覆盖。"""

import sqlite3

import pytest
from database import SCHEMA_VERSION, AuditProject
from domain.errors import ConflictError


def _baseline(proj, issue_id: int) -> tuple[int, str]:
    issue = proj.get_issue(issue_id)
    version_id = proj._conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM issue_versions WHERE issue_id=?", (issue_id,)
    ).fetchone()[0]
    return int(version_id), str(issue["updated_at"])


def test_issue_draft_is_separate_from_formal_issue_and_versions(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="原始定性")
    version_id, updated_at = _baseline(proj, issue_id)
    versions_before = proj.list_versions(issue_id)

    saved = proj.save_issue_draft(
        issue_id, {"defect_type": "草稿定性", "defect_desc": "尚未正式保存"},
        version_id, updated_at, "张三",
    )

    assert saved["draft"]["payload"]["defect_type"] == "草稿定性"
    assert proj.get_issue(issue_id)["defect_type"] == "原始定性"
    assert proj.list_versions(issue_id) == versions_before
    assert not any(log["action"] == "修改底稿" for log in proj.list_logs())


def test_issue_draft_rejects_stale_formal_baseline(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="原始定性")
    version_id, updated_at = _baseline(proj, issue_id)
    proj.update_issue(issue_id, "张三", defect_desc="正式版本已更新")

    with pytest.raises(ConflictError, match="正式底稿已更新"):
        proj.save_issue_draft(issue_id, {"defect_desc": "不应覆盖"}, version_id, updated_at, "张三")


def test_issue_draft_reports_conflict_and_can_be_discarded(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="原始定性")
    version_id, updated_at = _baseline(proj, issue_id)
    proj.save_issue_draft(issue_id, {"defect_desc": "本地草稿"}, version_id, updated_at, "张三")
    proj.update_issue(issue_id, "张三", defect_desc="正式版本")

    assert proj.get_issue_draft(issue_id)["conflicted"] is True
    assert proj.discard_issue_draft(issue_id) is True
    assert proj.get_issue_draft(issue_id) is None


def test_v17_project_migrates_to_v18_with_draft_and_review_tables(tmp_path):
    root = tmp_path / "v17项目"
    original = AuditProject(root)
    original.close()
    connection = sqlite3.connect(root / "audit.db")
    try:
        connection.execute("DROP TABLE review_note_events")
        connection.execute("DROP TABLE issue_drafts")
        connection.execute("UPDATE meta SET value='17' WHERE key='schema_version'")
        connection.commit()
    finally:
        connection.close()

    upgraded = AuditProject(root)
    try:
        assert upgraded.get_meta("schema_version") == str(SCHEMA_VERSION)
        tables = {row[0] for row in upgraded._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"issue_drafts", "review_note_events"}.issubset(tables)
        assert list((root / "快照").glob("pre_migration_v17_*.db"))
    finally:
        upgraded.close()
