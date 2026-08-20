"""批量维护底稿元数据：预检范围、事务提交和既有状态语义。"""

import pytest


def _issue(proj, unit_id, name, reviewer=""):
    return proj.add_issue(
        unit_id, "张三", department="财务", category="原分类", defect_type=name,
        defect_desc=f"{name} 描述", author="张三", reviewer=reviewer,
    )


def test_batch_metadata_updates_only_whitelist_and_keeps_versions(proj):
    unit_id = proj.add_unit("单位A", "张三")
    draft_id = _issue(proj, unit_id, "草稿底稿")
    reviewed_id = _issue(proj, unit_id, "已复核底稿", reviewer="李四")
    proj.change_status(reviewed_id, "编制完成", "张三")
    proj.change_status(reviewed_id, "已复核", "李四")
    before_versions = len(proj.list_versions(reviewed_id))

    preflight = proj.preflight_batch_issue_metadata(
        [draft_id, reviewed_id], {"department": "经营管理", "reviewer": "王五"},
    )
    assert preflight["selected"] == 2 and preflight["affected"] == 2
    assert preflight["reviewed"] == 1 and preflight["unchanged"] == 0

    result = proj.batch_update_issue_metadata(preflight["issue_ids"], preflight["changes"], "赵六")

    assert result == {"updated": 2, "unchanged": 0, "issue_ids": [draft_id, reviewed_id]}
    assert proj.get_issue(draft_id)["department"] == "经营管理"
    assert proj.get_issue(reviewed_id)["reviewer"] == "王五"
    assert proj.get_issue(reviewed_id)["status"] == "编制完成"
    assert len(proj.list_versions(reviewed_id)) == before_versions + 1
    assert any(log["action"] == "批量维护底稿元数据" and "2 条底稿" in log["target"] for log in proj.list_logs())


def test_batch_metadata_rejects_archived_scope_without_partial_write(proj):
    unit_id = proj.add_unit("单位A", "张三")
    normal_id = _issue(proj, unit_id, "普通底稿")
    archived_id = _issue(proj, unit_id, "归档底稿", reviewer="李四")
    proj.change_status(archived_id, "编制完成", "张三")
    proj.change_status(archived_id, "已复核", "李四")
    proj.change_status(archived_id, "已归档", "李四")

    with pytest.raises(ValueError, match="已归档"):
        proj.preflight_batch_issue_metadata([normal_id, archived_id], {"category": "新分类"})

    assert proj.get_issue(normal_id)["category"] == "原分类"
    assert proj.get_issue(archived_id)["category"] == "原分类"
