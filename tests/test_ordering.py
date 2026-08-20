"""v1.3 拖放排序：完整排列校验、持久化顺序和审计留痕。"""

import pytest


def test_reorder_units_persists_full_order_and_logs(proj):
    unit_a = proj.add_unit("单位A", "张三")
    unit_b = proj.add_unit("单位B", "张三")
    unit_c = proj.add_unit("单位C", "张三")

    assert proj.reorder_units([unit_c, unit_a, unit_b], "李四") is True
    assert [unit["id"] for unit in proj.list_units()] == [unit_c, unit_a, unit_b]
    assert [unit["sort_order"] for unit in proj.list_units()] == [0, 1, 2]
    assert any(log["action"] == "调整单位排序" and log["operator"] == "李四" for log in proj.list_logs())
    assert proj.reorder_units([unit_c, unit_a, unit_b], "李四") is False


def test_reorder_units_rejects_partial_or_duplicate_order(proj):
    unit_a = proj.add_unit("单位A", "张三")
    unit_b = proj.add_unit("单位B", "张三")

    with pytest.raises(ValueError, match="全部对象"):
        proj.reorder_units([unit_a], "张三")
    with pytest.raises(ValueError, match="不能重复"):
        proj.reorder_units([unit_a, unit_a], "张三")
    assert [unit["id"] for unit in proj.list_units()] == [unit_a, unit_b]


def test_reorder_issues_keeps_issue_numbers_and_versions(proj):
    unit_id = proj.add_unit("单位A", "张三")
    issue_a = proj.add_issue(unit_id, "张三", defect_type="问题A")
    issue_b = proj.add_issue(unit_id, "张三", defect_type="问题B")
    issue_c = proj.add_issue(unit_id, "张三", defect_type="问题C")
    original_seq = {issue["id"]: issue["seq"] for issue in proj.list_issues(unit_id)}

    assert proj.reorder_issues(unit_id, [issue_c, issue_a, issue_b], "李四") is True
    ordered = proj.list_issues(unit_id)
    assert [issue["id"] for issue in ordered] == [issue_c, issue_a, issue_b]
    assert {issue["id"]: issue["seq"] for issue in ordered} == original_seq
    assert any(log["action"] == "调整底稿排序" and log["operator"] == "李四" for log in proj.list_logs())


def test_reorder_issues_rejects_cross_unit_and_partial_order(proj):
    unit_a = proj.add_unit("单位A", "张三")
    unit_b = proj.add_unit("单位B", "张三")
    issue_a = proj.add_issue(unit_a, "张三")
    issue_b = proj.add_issue(unit_b, "张三")

    with pytest.raises(ValueError, match="当前范围"):
        proj.reorder_issues(unit_a, [issue_b], "张三")
    with pytest.raises(ValueError, match="全部对象"):
        proj.reorder_issues(unit_a, [], "张三")
    assert [issue["id"] for issue in proj.list_issues(unit_a)] == [issue_a]
