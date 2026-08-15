"""P0 底稿回收站：软删除、恢复、编号冲突和手动清空。"""


def test_delete_moves_issue_to_recycle_bin_and_preserves_attachments(proj, tmp_path):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="待删除")
    source = tmp_path / "证据.txt"
    source.write_text("evidence", encoding="utf-8")
    file_id = proj.add_file(unit_id, source, "张三")["id"]
    proj.link_file(issue_id, file_id, "张三")

    proj.delete_issue(issue_id, "张三")

    assert proj.get_issue(issue_id) is None
    recycled = proj.list_recycled_issues()
    assert len(recycled) == 1
    assert recycled[0]["issue_uuid"]
    assert proj._conn.execute("SELECT COUNT(*) FROM issue_files WHERE issue_id=?", (issue_id,)).fetchone()[0] == 1
    assert any(log["action"] == "移入回收站" for log in proj.list_logs())

    detail = proj.get_recycled_issue_detail(recycled[0]["recycle_id"])
    assert detail["issue"]["id"] == issue_id
    assert detail["issue"]["defect_type"] == "待删除"
    assert detail["attachment_total"] == 1
    assert detail["attachments"][0]["orig_name"] == "证据.txt"
    assert detail["version_count"] >= 1


def test_restore_recycled_issue_uses_new_number_when_old_number_was_reused(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    original = proj.add_issue(unit_id, "张三", defect_type="原问题")
    proj.add_issue(unit_id, "张三", defect_type="问题二")
    proj.delete_issue(original, "张三")
    replacement = proj.add_issue(unit_id, "张三", defect_type="复用一号")
    recycle_id = proj.list_recycled_issues()[0]["recycle_id"]

    restored = proj.restore_recycled_issue(recycle_id, "李四")

    assert proj.get_issue(replacement)["seq"] == 1
    assert restored["seq"] == 3
    assert not proj.list_recycled_issues()
    assert any("自动改为3" in log["detail"] for log in proj.list_logs())


def test_purge_requires_explicit_recycle_entry_and_keeps_permanent_log(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="待清空")
    proj.delete_issue(issue_id, "张三")
    recycle_id = proj.list_recycled_issues()[0]["recycle_id"]

    proj.purge_recycled_issue(recycle_id, "张三")

    assert proj.get_issue(issue_id, include_deleted=True) is None
    assert not proj.list_recycled_issues()
    assert any(log["action"] == "清空回收站" for log in proj.list_logs())


def test_unit_and_file_recycle_restore_and_explicit_purge(proj, tmp_path):
    """P0：单位和未关联附件也必须可恢复，物理删除只能发生在回收站。"""
    unit_id = proj.add_unit("甲单位", "张三")
    evidence = tmp_path / "待回收证据.pdf"
    evidence.write_bytes(b"evidence")
    file = proj.add_file(unit_id, evidence, "张三")

    proj.remove_file(file["id"], "张三")
    recycled_file = proj.list_recycled_files()[0]
    assert proj.get_file(file["id"]) is None
    assert (proj.root / file["rel_path"]).exists()
    restored_file = proj.restore_recycled_file(recycled_file["recycle_id"], "李四")
    assert restored_file["id"] == file["id"]

    proj.delete_unit(unit_id, "张三")
    recycled_unit = proj.list_recycled_units()[0]
    assert not proj.list_units()
    assert (proj.root / file["rel_path"]).exists()
    restored_unit = proj.restore_recycled_unit(recycled_unit["recycle_id"], "李四")
    assert restored_unit["id"] == unit_id

    proj.delete_unit(unit_id, "张三")
    recycle_id = proj.list_recycled_units()[0]["recycle_id"]
    proj.purge_recycled_unit(recycle_id, "张三")
    assert not (proj.root / file["rel_path"]).exists()
    assert proj.get_unit(unit_id, include_deleted=True) is None
