"""T3 状态机用例（database.change_status + update_issue 联动）。

覆盖（对应 TASKS.md T3 验收 + DESIGN.md 1.x）：
- 正常流转：草稿→编制完成→复核退回→重新提交→已复核→已归档
- 非法迁移被拦（已归档→草稿等）且提示可走路径
- 提交复核必填校验（发现描述/版块/定性）
- 复核通过必填（审核人）
- 复核退回意见必填 + audit_log 留痕
- 归档后编辑：原因必填、自动开新版本（change_reason 入快照）、状态回编制完成
- 已复核被编辑自动降回编制完成
- 已归档直接编辑被拒
- 零新增列：issues 表结构不变（旧库无迁移）
"""

import pytest


def _mk_issue(proj, **kw):
    """建一个已填必填字段的底稿。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    data = {"department": "营销管理", "defect_type": "电费回收不及时", "defect_desc": "测试描述"}
    data.update(kw)
    iid = proj.add_issue(uid, "张三", **data)
    return iid


def test_full_flow(proj):
    """正常流转闭环：草稿→编制完成→复核退回→重新提交→已复核→已归档。"""
    iid = _mk_issue(proj, reviewer="李四")
    assert proj.get_issue(iid)["status"] == "草稿"

    proj.change_status(iid, "编制完成", "张三")
    assert proj.get_issue(iid)["status"] == "编制完成"

    proj.change_status(iid, "复核退回", "李四", comment="证据不足，请补充")
    assert proj.get_issue(iid)["status"] == "复核退回"

    proj.change_status(iid, "编制完成", "张三")
    proj.change_status(iid, "已复核", "李四")
    assert proj.get_issue(iid)["status"] == "已复核"

    proj.change_status(iid, "已归档", "李四")
    assert proj.get_issue(iid)["status"] == "已归档"


def test_illegal_transition_blocked(proj):
    """非法迁移被拦 + 提示可走路径。"""
    iid = _mk_issue(proj, reviewer="李四")
    proj.change_status(iid, "编制完成", "张三")
    proj.change_status(iid, "已复核", "李四", comment="ok")
    proj.change_status(iid, "已归档", "李四")

    with pytest.raises(ValueError) as ei:
        proj.change_status(iid, "草稿", "张三")
    assert "归档后编辑" in str(ei.value)  # 教用户怎么做

    with pytest.raises(ValueError) as ei:
        proj.change_status(iid, "已归档", "李四", comment="x")
    assert "不能从" in str(ei.value)


def test_submit_requires_fields(proj):
    """提交复核前必填：发现描述/版块/定性。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理")  # 缺定性+描述

    with pytest.raises(ValueError) as ei:
        proj.change_status(iid, "编制完成", "张三")
    msg = str(ei.value)
    assert "发现描述" in msg and "定性" in msg


def test_review_requires_reviewer(proj):
    """复核通过前必填审核人。"""
    iid = _mk_issue(proj)  # 没填 reviewer
    proj.change_status(iid, "编制完成", "张三")
    with pytest.raises(ValueError) as ei:
        proj.change_status(iid, "已复核", "李四")
    assert "审核人" in str(ei.value)


def test_reject_requires_comment(proj):
    """复核退回必须填退回意见。"""
    iid = _mk_issue(proj)
    proj.change_status(iid, "编制完成", "张三")
    with pytest.raises(ValueError) as ei:
        proj.change_status(iid, "复核退回", "李四", comment="")
    assert "退回意见" in str(ei.value)


def test_reject_comment_in_audit_log(proj):
    """复核退回意见写入 audit_log 可查。"""
    iid = _mk_issue(proj)
    proj.change_status(iid, "编制完成", "张三")
    proj.change_status(iid, "复核退回", "李四", comment="证据不足，请补充")

    logs = proj.list_logs(limit=100)
    flow = [l for l in logs if l["action"] == "状态流转"]
    assert any("复核退回" in l["detail"] and "证据不足" in l["detail"] for l in flow)


def test_archive_edit_requires_reason(proj):
    """归档后编辑必须填修改原因。"""
    iid = _mk_issue(proj, reviewer="李四")
    proj.change_status(iid, "编制完成", "张三")
    proj.change_status(iid, "已复核", "李四")
    proj.change_status(iid, "已归档", "李四")

    with pytest.raises(ValueError) as ei:
        proj.change_status(iid, "编制完成", "张三", comment="")
    assert "修改原因" in str(ei.value)


def test_archive_edit_opens_new_version_with_reason(proj):
    """归档后编辑：自动开新版本，快照内嵌 change_reason，状态回编制完成。"""
    iid = _mk_issue(proj, reviewer="李四")
    proj.change_status(iid, "编制完成", "张三")
    proj.change_status(iid, "已复核", "李四")
    proj.change_status(iid, "已归档", "李四")
    versions_before = len(proj.list_versions(iid))

    proj.change_status(iid, "编制完成", "张三", comment="补充证据后再复核")

    assert proj.get_issue(iid)["status"] == "编制完成"
    versions = proj.list_versions(iid)
    assert len(versions) == versions_before + 1
    assert versions[-1]["snapshot"].get("change_reason") == "补充证据后再复核"


def test_reviewed_edit_demotes_to_submitted(proj):
    """已复核被编辑 → 自动降回编制完成（避免假象）。"""
    iid = _mk_issue(proj, reviewer="李四")
    proj.change_status(iid, "编制完成", "张三")
    proj.change_status(iid, "已复核", "李四")
    assert proj.get_issue(iid)["status"] == "已复核"

    proj.update_issue(iid, "张三", defect_desc="复核后补充的描述")
    assert proj.get_issue(iid)["status"] == "编制完成"


def test_archived_direct_edit_blocked(proj):
    """已归档直接编辑被拒，提示走归档后编辑。"""
    iid = _mk_issue(proj, reviewer="李四")
    proj.change_status(iid, "编制完成", "张三")
    proj.change_status(iid, "已复核", "李四")
    proj.change_status(iid, "已归档", "李四")

    with pytest.raises(ValueError) as ei:
        proj.update_issue(iid, "张三", defect_desc="直接改")
    assert "归档后编辑" in str(ei.value)


def test_issue_schema_has_optional_category(proj):
    """v4 保留问题分类，并补充稳定标识、冻结编号和结构化金额字段。"""
    cols = {r[1] for r in proj._conn.execute("PRAGMA table_info(issues)").fetchall()}
    assert {
        "id", "issue_uuid", "unit_id", "seq", "issue_code", "sort_order",
        "department", "category", "defect_type", "defect_desc", "amount", "amount_minor",
        "currency", "amount_unit", "regulation_basis", "suggestion", "author", "reviewer",
        "status", "created_at", "updated_at",
    } <= cols


def test_status_field_in_update_is_ignored(proj):
    """update_issue 直接传 status 字段不能绕过状态机（状态只走 change_status）。"""
    iid = _mk_issue(proj)
    # 尝试通过内容更新把状态改成"已归档"（绕过 change_status）
    proj.update_issue(iid, "张三", status="已归档")
    assert proj.get_issue(iid)["status"] != "已归档"


def test_restore_version_keeps_workflow_control(proj):
    """恢复历史内容不能把已复核底稿静默改回旧状态；内容变化后应重新进入复核。"""
    iid = _mk_issue(proj, reviewer="李四")
    first = proj.list_versions(iid)[0]
    proj.update_issue(iid, "张三", defect_desc="第二版")
    proj.change_status(iid, "编制完成", "张三")
    proj.change_status(iid, "已复核", "李四")

    proj.restore_version(iid, first["id"], "张三")
    restored = proj.get_issue(iid)
    assert restored["defect_desc"] == "测试描述"
    assert restored["status"] == "编制完成"

    proj.change_status(iid, "已复核", "李四")
    proj.change_status(iid, "已归档", "李四")
    with pytest.raises(ValueError, match="归档后编辑"):
        proj.restore_version(iid, first["id"], "张三")
