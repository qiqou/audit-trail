"""回归用例 — 对应《项目审查报告》F-01 ~ F-08。

设计：每个用例先复现审查发现的问题（当前应 RED），修复后变 GREEN。
用例名带 F-xx 编号，便于对照报告定位。
"""

import zipfile
from pathlib import Path

import pytest

# ───────────────────────── F-02 部分更新清空未提交字段 ─────────────────────────

def test_f02_partial_update_keeps_unsubmitted_fields(proj):
    """只更新 amount，其余字段必须保持原值（不得被默认空串覆盖）。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理", defect_type="电费回收不及时",
                         defect_desc="原始描述", amount="100万", author="张三")
    proj.update_issue(iid, "李四", amount="120万")
    got = proj.get_issue(iid)
    assert got["department"] == "营销管理"
    assert got["defect_type"] == "电费回收不及时"
    assert got["defect_desc"] == "原始描述"
    assert got["author"] == "张三"
    assert got["amount"] == "120万"


# ───────────────────────── F-01 跨单位共享附件误删 ─────────────────────────

def test_f01_delete_unit_keeps_cross_unit_attachment(proj, tmp_path):
    """附件关联到其他单位底稿时，删除归属单位必须被拒绝（不得删除证据）。"""
    ua = proj.add_unit("单位A", "张三")
    ub = proj.add_unit("单位B", "张三")
    ib = proj.add_issue(ub, "张三", department="营销管理", defect_type="问题B")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"%PDF shared evidence")
    f = proj.add_file(ua, str(src), "张三")
    # B 单位的底稿关联 A 单位的附件（跨单位共享，工作区一份）
    proj.link_file(ib, f["id"], "张三")

    # 删除 A：必须被拒绝（附件正被 B 引用），不能连带删除物理文件
    with pytest.raises(ValueError):
        proj.delete_unit(ua, "张三")

    phys = proj.root / f["rel_path"]
    assert phys.exists(), "跨单位引用的附件物理文件不应被删除"
    assert proj.files_for_issue(ib), "B 单位底稿不应失去附件关联"
    # 删除后无孤儿关联
    orphans = proj._conn.execute(
        "SELECT COUNT(*) FROM issue_files l LEFT JOIN files f ON f.id=l.file_id "
        "LEFT JOIN issues i ON i.id=l.issue_id WHERE f.id IS NULL OR i.id IS NULL"
    ).fetchone()[0]
    assert orphans == 0, "issue_files 不应残留孤儿关联"


def test_f01_delete_unit_ok_without_cross_refs(proj, tmp_path):
    """无跨单位引用时，删除单位仍正常（同单位底稿引用随单位一起删）。"""
    ua = proj.add_unit("单位A", "张三")
    ia = proj.add_issue(ua, "张三", department="营销管理", defect_type="问题A")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"%PDF same unit")
    f = proj.add_file(ua, str(src), "张三")
    proj.link_file(ia, f["id"], "张三")  # 同单位引用，允许删除

    proj.delete_unit(ua, "张三")
    assert not (proj.root / f["rel_path"]).exists()
    assert proj.list_units() == []


# ───────────────────────── F-05 单位名清洗目录碰撞 ─────────────────────────

def test_f05_safe_dirname_no_collision(proj):
    """不同单位名（清洗后曾碰撞）必须物理隔离目录。"""
    proj.add_unit("A/B", "张三")
    proj.add_unit("A:B", "张三")
    dirs = sorted(p.name for p in (proj.root / "附件库").iterdir() if p.is_dir())
    assert len(dirs) == 2, f"两个单位应有各自附件目录，实际: {dirs}"


def test_f05_rename_unit_keeps_dir(proj, tmp_path):
    """重命名单位不搬物理目录（稳定 ID 隔离）。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"stable dir")
    f = proj.add_file(uid, str(src), "张三")
    old_rel = f["rel_path"]

    proj.rename_unit(uid, "华电集团YY热电厂", "张三")
    assert (proj.root / old_rel).exists(), "重命名单位后附件物理路径不变"


def test_f05_migrate_old_name_dirs(tmp_path):
    """旧版项目（附件库/单位名）打开时自动迁移到 unit_{id}。"""
    from database import ATTACH_DIR, AuditProject

    root = tmp_path / "旧项目"
    # 1) 建新结构项目并塞一个单位+附件
    p = AuditProject(root)
    uid = p.add_unit("华电集团XX电厂", "张三")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"migrate me")
    f = p.add_file(uid, str(src), "张三")
    iid = p.add_issue(uid, "张三", department="营销管理", defect_type="问题A")
    p.link_file(iid, f["id"], "张三")
    p.close()

    # 2) 手工退化回旧结构：unit_{id} 目录改回单位名，rel_path 也改回旧前缀
    (root / ATTACH_DIR / f"unit_{uid}").rename(root / ATTACH_DIR / "华电集团XX电厂")
    import sqlite3
    conn = sqlite3.connect(root / "audit.db")
    conn.execute(
        "UPDATE files SET rel_path=? WHERE id=?",
        (f"{ATTACH_DIR}/华电集团XX电厂/{f['stored_name']}", f["id"]),
    )
    conn.commit()
    conn.close()

    # 3) 重新打开 → 自动迁移
    p2 = AuditProject(root)
    try:
        assert (root / ATTACH_DIR / f"unit_{uid}").is_dir(), "迁移后使用 unit_{id} 目录"
        assert not (root / ATTACH_DIR / "华电集团XX电厂").exists(), "旧目录已移除"
        f2 = p2.get_file(f["id"])
        assert f2["rel_path"].startswith(f"{ATTACH_DIR}/unit_{uid}/"), "rel_path 已更新"
        assert (root / f2["rel_path"]).exists(), "物理文件可经新 rel_path 访问"
        # 关联完整
        assert p2.files_for_issue(iid), "迁移后底稿附件关联完整"
    finally:
        p2.close()


# ───────────────────────── F-04 备份/导出命名与一致性 ─────────────────────────

def test_f04_export_same_second_unique_filename(proj, tmp_path, monkeypatch):
    """同一秒内连续导出不得产生相同文件名（不覆盖旧输出）。"""
    from export import export_excel

    uid = proj.add_unit("华电集团XX电厂", "张三")
    proj.add_issue(uid, "张三", department="营销管理", defect_type="问题A")

    monkeypatch.setattr("export._now_ts", lambda: "20260808_120000")
    r1 = export_excel(proj, scope="project", operator="张三")
    r2 = export_excel(proj, scope="project", operator="张三")
    assert r1["filename"] != r2["filename"], "同秒导出文件名必须唯一"


def test_f04_backup_same_second_unique_filename(proj, monkeypatch):
    """同一秒内连续备份不得产生相同文件名。"""
    from export import create_backup

    proj.add_unit("华电集团XX电厂", "张三")
    monkeypatch.setattr("export._now_ts", lambda: "20260808_120000")
    b1 = create_backup(proj)
    b2 = create_backup(proj)
    assert b1["filename"] != b2["filename"]


def test_f04_backup_restorable(proj, tmp_path, monkeypatch):
    """备份包含 audit.db 且可恢复（一致性快照可回读）。"""
    from export import create_backup, restore_backup

    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理", defect_type="问题A",
                         defect_desc="备份前的描述")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"backup evidence")
    f = proj.add_file(uid, str(src), "张三")
    proj.link_file(iid, f["id"], "张三")

    monkeypatch.setattr("export._now_ts", lambda: "20260808_120000")
    info = create_backup(proj)
    with zipfile.ZipFile(info["abs_path"]) as zf:
        assert "audit.db" in zf.namelist()
        assert any(n.startswith("附件库/") for n in zf.namelist())

    target = tmp_path / "恢复项目"
    info2 = restore_backup(info["abs_path"], str(target))
    restored = Path(info2["path"])
    # V3.2：恢复目标自动加 .auditproj 后缀（与新建项目一致）
    assert restored.name == "恢复项目" + ".auditproj"
    assert (restored / "audit.db").exists()
    assert (restored / "附件库").is_dir()


def test_f04_restore_failure_no_partial_dir(proj, tmp_path):
    """恢复失败不得留下半成品目标目录（先校验后落盘）。"""
    from export import restore_backup

    bad = tmp_path / "bad.auditbak"
    bad.write_bytes(b"not a zip")
    target = tmp_path / "目标项目"
    with pytest.raises(ValueError):
        restore_backup(str(bad), str(target))
    # 校验失败时目标目录不应被创建（先完整校验再原子落盘）
    assert not target.exists(), "校验失败不应创建目标目录"


# ───────────────────────── F-08 附件相对路径边界 ─────────────────────────

def test_f08_folder_import_rejects_path_traversal(proj, tmp_path):
    """文件夹成员路径含 .. 时必须拒绝，不能写到项目目录之外。"""
    unit_id = proj.add_unit("单位A", "张三")
    source = tmp_path / "证据.txt"
    source.write_text("evidence", encoding="utf-8")
    escaped = proj.root.parent / "escaped.txt"

    with pytest.raises(ValueError, match="非法相对路径"):
        proj.add_folder(unit_id, [("../../../../escaped.txt", str(source))], "资料", "张三")

    assert not escaped.exists(), "恶意相对路径不得在项目外创建文件"
    assert proj.unlinked_files(unit_id) == [], "失败导入不得留下附件记录"


def test_f08_rejects_project_with_tampered_attachment_path(tmp_path):
    """项目数据库被篡改为项目外路径时，打开即失败，不允许继续读写。"""
    from database import AuditProject

    root = tmp_path / "不安全项目"
    project = AuditProject(root)
    try:
        unit_id = project.add_unit("单位A", "张三")
        source = tmp_path / "证据.txt"
        source.write_text("evidence", encoding="utf-8")
        record = project.add_file(unit_id, str(source), "张三")
        with project._lock, project._conn:
            project._conn.execute("UPDATE files SET rel_path=? WHERE id=?", ("../../外部文件.txt", record["id"]))
    finally:
        project.close()

    with pytest.raises(ValueError, match="不安全的附件路径"):
        AuditProject(root)


# ───────────────────────── F-07 上传资源边界（数据层侧） ─────────────────────────

def test_f07_add_file_sets_size_and_sha(proj, tmp_path):
    """附件入库必须记录 size 与 sha256（资源边界校验的前提字段）。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"x" * 2048)
    f = proj.add_file(uid, str(src), "张三")
    assert f["size"] == 2048
    assert len(f["sha256"]) == 64


# ───────────────────────── 其他回归基线 ─────────────────────────

def test_remove_file_protected_when_linked(proj, tmp_path):
    """被底稿引用的附件不允许直接删除（必须先解除关联）。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理", defect_type="问题A")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"protected")
    f = proj.add_file(uid, str(src), "张三")
    proj.link_file(iid, f["id"], "张三")
    with pytest.raises(ValueError):
        proj.remove_file(f["id"], "张三")
    assert (proj.root / f["rel_path"]).exists()


# ───────────────────────── F-06 批量重命名 / 移动（补齐） ─────────────────────────

def test_f06_batch_rename_with_conflict(proj, tmp_path):
    """批量重命名：冲突跳过、其余成功、留痕。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    src1 = tmp_path / "a.pdf"; src1.write_bytes(b"a")
    src2 = tmp_path / "b.pdf"; src2.write_bytes(b"b")
    src3 = tmp_path / "c.pdf"; src3.write_bytes(b"c")
    f1 = proj.add_file(uid, str(src1), "张三")
    f2 = proj.add_file(uid, str(src2), "张三")
    f3 = proj.add_file(uid, str(src3), "张三")

    # f1→新名A、f2→新名B、f3 试图改成与 f1 相同的新名A（冲突）
    r = proj.batch_rename_files([
        {"id": f1["id"], "name": "新名A.pdf"},
        {"id": f2["id"], "name": "新名B.pdf"},
        {"id": f3["id"], "name": "新名A.pdf"},
    ], "张三")
    assert r["renamed"] == 2
    assert len(r["conflicts"]) == 1 and "同名" in r["conflicts"][0]["reason"]
    assert proj.get_file(f1["id"])["orig_name"] == "新名A.pdf"
    assert proj.get_file(f2["id"])["orig_name"] == "新名B.pdf"
    assert proj.get_file(f3["id"])["orig_name"] == "c.pdf"
    # 留痕
    assert any(l["action"] == "批量重命名附件" for l in proj.list_logs())


def test_f06_move_file_to_unit(proj, tmp_path):
    """移动附件到其他单位：物理文件迁移 + 归属更新 + 引用保持。"""
    ua = proj.add_unit("单位A", "张三")
    ub = proj.add_unit("单位B", "张三")
    ia = proj.add_issue(ua, "张三", department="营销管理", defect_type="问题A")
    src = tmp_path / "证据.pdf"
    src.write_bytes(b"move me")
    f = proj.add_file(ua, str(src), "张三")
    proj.link_file(ia, f["id"], "张三")
    old_rel = f["rel_path"]

    moved = proj.move_file_to_unit(f["id"], ub, "张三")
    assert moved["unit_id"] == ub
    assert not (proj.root / old_rel).exists(), "旧路径文件已移走"
    assert (proj.root / moved["rel_path"]).exists(), "新路径文件存在"
    # 引用保持（跨单位引用形成，F-01 保护兜底：B 单位附件被 A 单位底稿引用）
    assert proj.files_for_issue(ia)[0]["id"] == f["id"]
    assert proj.cross_unit_refs(ub), "B 单位附件被 A 单位底稿引用应被检测"
    # 留痕
    assert any(l["action"] == "移动附件" for l in proj.list_logs())
