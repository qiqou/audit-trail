"""数据层基础 CRUD 用例（由 scripts/test_database.py 迁移为 pytest 风格）。

覆盖：
- 项目自包含目录结构
- 单位增删、附件目录
- 底稿增改、版本快照、恢复版本
- 附件导入/重命名/关联
- 操作日志留痕
- 删除与序号重排
"""

import pytest


def test_project_self_contained(proj):
    """项目自包含：audit.db / 附件库 / 输出 就位。"""
    assert (proj.root / "audit.db").exists()
    assert (proj.root / "附件库").is_dir()
    assert (proj.root / "输出").is_dir()


def test_add_unit_and_dir(proj):
    uid = proj.add_unit("华电集团XX电厂", "张三")
    assert proj.list_units()[0]["name"] == "华电集团XX电厂"
    # 附件目录用稳定 ID（unit_{id}），不随显示名变化（审查 F-05 修复）
    assert (proj.root / "附件库" / f"unit_{uid}").is_dir()
    assert uid == proj.list_units()[0]["id"]


def test_issue_versions_and_restore(proj):
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理", category="经营管理", defect_type="电费回收不及时")
    assert proj.get_issue(iid)["seq"] == 1
    assert proj.get_issue(iid)["category"] == "经营管理"
    assert len(proj.list_versions(iid)) == 1  # 初始 v1

    proj.update_issue(iid, "张三", department="营销管理", category="经营管理", defect_type="电费回收不及时",
                      defect_desc="第一版描述", amount="100万")
    proj.update_issue(iid, "李四", department="营销管理", category="合规管理", defect_type="电费回收不及时",
                      defect_desc="第二版描述", amount="120万")
    assert len(proj.list_versions(iid)) == 3
    assert proj.get_issue(iid)["defect_desc"] == "第二版描述"
    assert proj.get_issue(iid)["category"] == "合规管理"
    assert proj.list_versions(iid)[-1]["saved_by"] == "李四"

    # 无变化更新不产生版本
    ok = proj.update_issue(iid, "李四", department="营销管理", category="合规管理", defect_type="电费回收不及时",
                           defect_desc="第二版描述", amount="120万")
    assert ok is False
    assert len(proj.list_versions(iid)) == 3

    # 恢复 v2
    v2 = proj.list_versions(iid)[1]  # version_no=2（第一版描述）
    proj.restore_version(iid, v2["id"], "张三")
    assert proj.get_issue(iid)["defect_desc"] == "第一版描述"
    assert proj.get_issue(iid)["category"] == "经营管理"
    assert len(proj.list_versions(iid)) == 4  # 恢复前内容已留档


def test_attachment_index(proj, tmp_path):
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理", defect_type="电费回收不及时")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"%PDF-1.4 test content")

    f = proj.add_file(uid, str(src), "张三")
    assert f["orig_name"] == "证据.pdf"
    assert f["stored_name"] != "证据.pdf"  # 磁盘名 uuid
    assert (proj.root / f["rel_path"]).exists()
    assert len(f["sha256"]) == 64

    proj.link_file(iid, f["id"], "张三")
    assert proj.files_for_issue(iid)[0]["orig_name"] == "证据.pdf"
    assert proj.list_issues(uid)[0]["file_count"] == 1

    f2 = proj.add_file(uid, str(src), "张三")  # 同名再导入
    assert f2["id"] != f["id"]  # uuid 存储名不冲突
    assert any(x["id"] == f2["id"] for x in proj.unlinked_files(uid))
    proj.rename_file(f2["id"], "证据-改.pdf", "张三")
    assert proj.get_file(f2["id"])["orig_name"] == "证据-改.pdf"


def test_audit_log_trace(proj, tmp_path):
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理", defect_type="电费回收不及时")
    proj.update_issue(iid, "李四", defect_desc="第一版描述")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"x")
    f = proj.add_file(uid, str(src), "张三")
    proj.link_file(iid, f["id"], "张三")
    proj.rename_file(f["id"], "证据-改.pdf", "张三")
    v = proj.list_versions(iid)[0]
    proj.restore_version(iid, v["id"], "张三")

    logs = proj.list_logs()
    actions = [l["action"] for l in logs]
    for a in ["新建单位", "新建底稿", "修改底稿", "恢复版本", "导入附件", "关联附件", "重命名附件"]:
        assert a in actions
    assert logs[0]["operator"] == "张三"  # 最新日志
    assert any("修改字段" in l["detail"] for l in logs)


def test_delete_and_renumber(proj, tmp_path):
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三")
    proj.add_issue(uid, "张三")
    proj.delete_issue(iid, "张三")
    assert proj.list_issues(uid)[0]["seq"] == 1  # 序号重排
    assert (proj.root / "附件库" / f"unit_{uid}").is_dir()
    proj.delete_unit(uid, "张三")
    assert not (proj.root / "附件库" / f"unit_{uid}").exists()


def test_project_meta(proj):
    proj.project_name = "2026年华电专项审计"
    assert proj.project_name == "2026年华电专项审计"


def test_list_issues_by_unit_uses_project_tree_shape(proj):
    """V3 问题树按单位聚合，空单位不产生无意义键。"""
    unit_a = proj.add_unit("单位A", "张三")
    unit_b = proj.add_unit("单位B", "张三")
    proj.add_issue(unit_b, "张三", defect_type="B问题")
    proj.add_issue(unit_a, "张三", defect_type="A问题1")
    proj.add_issue(unit_a, "张三", defect_type="A问题2")

    tree = proj.list_issues_by_unit()
    assert list(tree) == [unit_a, unit_b]
    assert [item["seq"] for item in tree[unit_a]] == [1, 2]
    assert tree[unit_b][0]["defect_type"] == "B问题"


def test_exclusive_attachment_lifecycle_never_hides_evidence(proj, tmp_path):
    """独占附件只能保留一处关联；取消关联或删除底稿后必须恢复到共享资料库。"""
    unit_id = proj.add_unit("单位A", "张三")
    issue_a = proj.add_issue(unit_id, "张三", defect_type="问题A")
    issue_b = proj.add_issue(unit_id, "张三", defect_type="问题B")
    source = tmp_path / "证据.txt"
    source.write_text("evidence", encoding="utf-8")
    evidence = proj.add_file(unit_id, source, "张三")

    proj.link_file(issue_a, evidence["id"], "张三")
    proj.link_file(issue_b, evidence["id"], "张三")
    proj.link_file_exclusive(issue_a, evidence["id"], "张三")
    assert [item["id"] for item in proj.files_for_issue(issue_a)] == [evidence["id"]]
    assert proj.files_for_issue(issue_b) == []
    assert proj.get_file(evidence["id"])["exclusive_to"] == issue_a

    with pytest.raises(ValueError, match="仅关联"):
        proj.link_file(issue_b, evidence["id"], "张三")

    proj.unlink_file(issue_a, evidence["id"], "张三")
    assert proj.get_file(evidence["id"])["exclusive_to"] is None
    assert evidence["id"] in {item["id"] for item in proj.unlinked_files(unit_id)}

    proj.link_file_exclusive(issue_a, evidence["id"], "张三")
    proj.delete_issue(issue_a, "张三")
    assert proj.get_file(evidence["id"])["exclusive_to"] is None


def test_new_issue_always_starts_as_draft(proj):
    """新建底稿不能通过底层字段绕过状态机直接标记已复核/归档。"""
    unit_id = proj.add_unit("单位A", "张三")
    issue_id = proj.add_issue(unit_id, "张三", status="已归档")
    assert proj.get_issue(issue_id)["status"] == "草稿"
