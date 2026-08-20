"""内部复核意见只追加事件，并始终保留版本锚点。"""

import pytest
from domain.errors import ConflictError


def _issue_and_baseline(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="收入截止")
    version_id = proj._conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM issue_versions WHERE issue_id=?", (issue_id,)
    ).fetchone()[0]
    return issue_id, int(version_id)


def test_review_note_events_are_immutable_and_reopenable(proj):
    issue_id, version_id = _issue_and_baseline(proj)
    note = proj.create_review_note(issue_id, "请补充函证回函", "defect_desc", version_id, "李四")
    note = proj.append_review_note_event(note["note_uuid"], "replied", "已补充并附后", "张三")
    note = proj.append_review_note_event(note["note_uuid"], "resolved", "复核确认", "李四")
    note = proj.append_review_note_event(note["note_uuid"], "reopened", "回函金额仍需核对", "李四")

    assert note["status"] == "open"
    assert [event["event_type"] for event in note["events"]] == ["created", "replied", "resolved", "reopened"]
    assert note["base_version_id"] == version_id
    assert any(log["action"] == "提出复核意见" for log in proj.list_logs())


def test_review_note_cannot_attach_to_stale_or_resolve_twice(proj):
    issue_id, version_id = _issue_and_baseline(proj)
    proj.update_issue(issue_id, "张三", defect_desc="正式内容已更新")
    with pytest.raises(ConflictError, match="版本已变化"):
        proj.create_review_note(issue_id, "不应附到旧版本", "", version_id, "李四")

    current_version = proj._conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM issue_versions WHERE issue_id=?", (issue_id,)
    ).fetchone()[0]
    note = proj.create_review_note(issue_id, "请确认新版", "", int(current_version), "李四")
    proj.append_review_note_event(note["note_uuid"], "resolved", "已处理", "李四")
    with pytest.raises(ConflictError, match="已清除"):
        proj.append_review_note_event(note["note_uuid"], "resolved", "重复处理", "李四")
