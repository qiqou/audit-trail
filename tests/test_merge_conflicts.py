"""T9 合并冲突报告用例。

覆盖（对应 TASKS.md T9 验收）：
- 4 类冲突均被检测并报告：同名单位不同底稿（unit_exists）、同 seq 底稿（seq_reshuffle）、
  同名附件不同内容（file_same_name）、版块预设差异（dept_merge）
- 无冲突时 conflicts 为空，合并行为与 v1.1 完全一致（数据完整导入）
"""

import sqlite3
import zipfile
from pathlib import Path

import export
import pytest
from database import AuditProject
from export import merge_backups


def _make_backup(proj, name="备份1", units=None, depts=None):
    """把项目打成 .auditbak（复制 audit.db + 附件库）。返回备份文件路径。"""
    import shutil
    import tempfile

    proj.close()
    tmp = Path(tempfile.mkdtemp(prefix="t9_bak_"))
    shutil.copy2(proj.db_path, tmp / "audit.db")
    att = proj.root / "附件库"
    if att.exists():
        shutil.copytree(att, tmp / "附件库")
    # 版块预设
    if depts:
        conn = sqlite3.connect(tmp / "audit.db")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('departments', ?)",
                     (depts,))
        conn.commit()
        conn.close()
    bak = tmp / f"{name}.auditbak"
    with zipfile.ZipFile(bak, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in tmp.rglob("*"):
            if p.is_file() and p != bak:
                zf.write(p, p.relative_to(tmp))
    return bak


def test_merge_no_conflict_identical_to_v11(proj, tmp_path):
    """无冲突：conflicts 为空，合并数据完整（行为与 v1.1 一致）。"""
    # 目标项目：1 单位 1 底稿
    u1 = proj.add_unit("华电XX电厂", "张三")
    i1 = proj.add_issue(u1, "张三", department="营销", defect_type="电费回收不及时",
                        defect_desc="问题A", author="张三")
    src = proj.root / "a.txt"
    src.write_text("aaa", encoding="utf-8")
    f1 = proj.add_file(u1, src, "张三", orig_name="a.txt")
    proj.link_file(i1, f1["id"], "张三")

    # 备份项目：不同单位（华电YY电厂），无冲突
    bak_proj = AuditProject(tmp_path / "src_bak")
    u2 = bak_proj.add_unit("华电YY电厂", "李四")
    bak_proj.add_issue(u2, "李四", department="财务", defect_type="账实不符",
                       defect_desc="问题B", author="李四")
    bak = _make_backup(bak_proj, "B")

    r = merge_backups(proj, [bak], "张三")
    assert r["conflicts"] == []
    assert r["units"] == 1
    assert r["issues"] == 1
    # 数据完整：新单位 + 新底稿存在
    names = {u["name"] for u in proj.list_units()}
    assert "华电YY电厂" in names


def test_merge_conflict_unit_exists(proj, tmp_path):
    """冲突1：同名单位不同底稿（保留两边）被报告。"""
    u1 = proj.add_unit("华电XX电厂", "张三")
    proj.add_issue(u1, "张三", department="营销", defect_type="电费回收不及时",
                   defect_desc="目标已有底稿", author="张三")

    bak_proj = AuditProject(tmp_path / "src_bak2")
    u2 = bak_proj.add_unit("华电XX电厂", "李四")  # 同名
    bak_proj.add_issue(u2, "李四", department="财务", defect_type="账实不符",
                       defect_desc="备份里的底稿", author="李四")
    bak = _make_backup(bak_proj, "C")

    r = merge_backups(proj, [bak], "张三")
    types = [c["type"] for c in r["conflicts"]]
    assert "unit_exists" in types
    unit_conf = next(c for c in r["conflicts"] if c["type"] == "unit_exists")
    assert "华电XX电厂" in unit_conf["message"]
    assert "保留两边" in unit_conf["message"]
    # 两边都保留：目标单位现在有 2 条底稿
    issues = proj.list_issues(u1)
    assert len(issues) == 2


def test_merge_conflict_seq_reshuffle(proj, tmp_path):
    """冲突2：同 seq 底稿自动重排被报告。"""
    u1 = proj.add_unit("华电XX电厂", "张三")
    proj.add_issue(u1, "张三", department="营销", defect_type="问题1", defect_desc="目标seq1")
    proj.add_issue(u1, "张三", department="营销", defect_type="问题2", defect_desc="目标seq2")

    bak_proj = AuditProject(tmp_path / "src_bak3")
    u2 = bak_proj.add_unit("华电XX电厂", "李四")  # 同名 → seq 重叠（备份也 1/2）
    bak_proj.add_issue(u2, "李四", department="财务", defect_type="账实不符", defect_desc="备份seq1")
    bak = _make_backup(bak_proj, "D")

    r = merge_backups(proj, [bak], "张三")
    types = [c["type"] for c in r["conflicts"]]
    assert "seq_reshuffle" in types
    seq_conf = next(c for c in r["conflicts"] if c["type"] == "seq_reshuffle")
    assert "自动重排" in seq_conf["message"]
    # 合并后单位内 seq 不重复
    seqs = [i["seq"] for i in proj.list_issues(u1)]
    assert len(seqs) == len(set(seqs))


def test_merge_conflict_file_same_name(proj, tmp_path):
    """冲突3：同名附件不同内容（都保留）被报告。"""
    u1 = proj.add_unit("华电XX电厂", "张三")
    i1 = proj.add_issue(u1, "张三", department="营销", defect_type="电费回收不及时",
                        defect_desc="问题A", author="张三")
    src = proj.root / "证据.txt"
    src.write_text("目标版本内容", encoding="utf-8")
    f1 = proj.add_file(u1, src, "张三", orig_name="证据.txt")
    proj.link_file(i1, f1["id"], "张三")

    bak_proj = AuditProject(tmp_path / "src_bak4")
    u2 = bak_proj.add_unit("华电XX电厂", "李四")
    i2 = bak_proj.add_issue(u2, "李四", department="财务", defect_type="账实不符",
                            defect_desc="问题B", author="李四")
    src2 = bak_proj.root / "证据.txt"
    src2.write_text("备份版本内容完全不同", encoding="utf-8")
    f2 = bak_proj.add_file(u2, src2, "李四", orig_name="证据.txt")  # 同名不同内容
    bak_proj.link_file(i2, f2["id"], "李四")
    bak = _make_backup(bak_proj, "E")

    r = merge_backups(proj, [bak], "张三")
    types = [c["type"] for c in r["conflicts"]]
    assert "file_same_name" in types
    # 两个都保留：目标单位有 2 个附件记录
    files = proj.list_files(u1)
    same_name = [f for f in files if f["orig_name"] == "证据.txt"]
    assert len(same_name) == 2


def test_merge_conflict_dept_merge(proj, tmp_path):
    """冲突4：版块预设差异去重合并被报告。"""
    proj.add_unit("华电XX电厂", "张三")
    proj.set_meta("departments", '["营销", "财务"]')

    bak_proj = AuditProject(tmp_path / "src_bak5")
    bak_proj.add_unit("华电YY电厂", "李四")
    bak = _make_backup(bak_proj, "F", depts='["营销", "安全生产"]')

    r = merge_backups(proj, [bak], "张三")
    types = [c["type"] for c in r["conflicts"]]
    assert "dept_merge" in types
    dept_conf = next(c for c in r["conflicts"] if c["type"] == "dept_merge")
    assert "安全生产" in dept_conf["message"]  # 只报告新增的
    # 合并结果：营销/财务/安全生产（去重）
    import json
    cur = json.loads(proj.get_meta("departments", "[]"))
    assert cur == ["营销", "财务", "安全生产"]


def test_merge_preserves_status_versions_and_exclusive_evidence(proj, tmp_path):
    """备份合并不能把已归档底稿降为草稿，也不能丢版本链和独占证据语义。"""
    source = AuditProject(tmp_path / "src_complete")
    unit_id = source.add_unit("来源单位", "李四")
    issue_id = source.add_issue(
        unit_id,
        "李四",
        department="经营管理",
        defect_type="收入确认不准确",
        defect_desc="原始描述",
        reviewer="王五",
    )
    source.update_issue(issue_id, "李四", defect_desc="修改后的描述")
    source.change_status(issue_id, "编制完成", "李四")
    source.change_status(issue_id, "已复核", "王五")
    source.change_status(issue_id, "已归档", "王五")
    evidence_path = tmp_path / "归档证据.pdf"
    evidence_path.write_bytes(b"%PDF archived evidence")
    evidence = source.add_file(unit_id, evidence_path, "李四")
    source.link_file_exclusive(issue_id, evidence["id"], "李四")
    request = source.create_project_request(
        "李四", title="提供发货单", responsible="财务部", due_date="2026-08-31", issue_id=issue_id,
    )
    source.update_project_request(request["request_uuid"], "李四", status="provided", provided_file_id=evidence["id"])
    expected_versions = source.list_versions(issue_id)
    backup = _make_backup(source, "完整备份")

    result = merge_backups(proj, [backup], "张三")
    imported_unit = next(unit for unit in proj.list_units() if unit["name"] == "来源单位")
    imported_issue = proj.list_issues(imported_unit["id"])[0]
    imported_versions = proj.list_versions(imported_issue["id"])
    imported_file = proj.list_files(imported_unit["id"])[0]

    assert result["errors"] == []
    assert result["versions"] == len(expected_versions)
    assert imported_issue["status"] == "已归档"
    assert [version["snapshot"] for version in imported_versions] == [
        version["snapshot"] for version in expected_versions
    ]
    assert imported_file["exclusive_to"] == imported_issue["id"]
    assert proj.linked_issue_ids_for_file(imported_file["id"]) == [imported_issue["id"]]
    imported_request = proj.list_project_requests()[0]
    assert result["requests"] == 1
    assert imported_request["title"] == "提供发货单" and imported_request["issue_id"] == imported_issue["id"]
    assert imported_request["provided_file_id"] == imported_file["id"] and imported_request["status"] == "provided"
    assert result["source_logs"] > 0
    assert any(log["action"] == "保留来源操作日志" for log in proj.list_logs())


def test_merge_failure_never_commits_partial_stage(proj, tmp_path, monkeypatch):
    """P0：任一来源处理失败时，单位、附件和数据库都不能留下半批次结果。"""
    original_unit = proj.add_unit("正式项目原单位", "张三")
    source = AuditProject(tmp_path / "source_for_atomic")
    source.add_unit("不应写入的来源单位", "李四")
    backup = _make_backup(source, "原子失败")

    def fail_after_stage_write(stage, _paths, operator):
        stage.add_unit("暂存写入", operator)
        raise OSError("模拟合并中断")

    monkeypatch.setattr(export, "_merge_backups_in_place", fail_after_stage_write)
    with pytest.raises(OSError, match="模拟合并中断"):
        merge_backups(proj, [backup], "张三")

    assert [unit["name"] for unit in proj.list_units()] == ["正式项目原单位"]
    assert proj.get_unit(original_unit)["name"] == "正式项目原单位"
