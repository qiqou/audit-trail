"""底稿只读仓储必须保留排序、附件计数和软删除边界。"""

from repositories.issues import IssueRepository


def test_issue_repository_reads_active_issues_with_file_count_and_scope(proj):
    first_unit = proj.add_unit("甲单位", "张三")
    second_unit = proj.add_unit("乙单位", "张三")
    first_issue = proj.add_issue(first_unit, "张三", defect_type="甲一")
    second_issue = proj.add_issue(first_unit, "张三", defect_type="甲二")
    other_issue = proj.add_issue(second_unit, "张三", defect_type="乙一")
    with proj._lock, proj._conn:
        proj._conn.execute("UPDATE issues SET deleted_at='2026-08-21 00:00:00' WHERE id=?", (second_issue,))

    repository = IssueRepository(proj._conn)
    assert [row["id"] for row in repository.list_active_for_unit(first_unit)] == [first_issue]
    assert repository.get(second_issue) is None
    assert repository.get(second_issue, include_deleted=True)["defect_type"] == "甲二"
    assert {unit_id: [row["id"] for row in rows] for unit_id, rows in repository.list_active_grouped_by_unit().items()} == {
        first_unit: [first_issue], second_unit: [other_issue],
    }
