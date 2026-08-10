"""T1 项目健康检查 + manifest.json 用例。

覆盖（对应 TASKS.md T1 验收）：
- 正常项目 → ok=True，problems 空，counts 正确
- 孤儿关联（issue_files 指向不存在底稿/附件）→ orphan_link 命中
- 底稿/附件记录指向不存在单位 → orphan_issue / orphan_filerow 命中
- files 有记录但物理缺失 → missing_file 命中（文件 + 文件夹实体）
- 附件库存在未登记文件 → orphan_phys 命中
- 伪造哈希 → hash_mismatch 命中
- manifest.json 生成与内容
"""

import json

from database import SCHEMA_VERSION


def test_health_ok_on_fresh_project(proj):
    """空项目健康检查：通过，counts 全 0。"""
    h = proj.health_check(sample_size=0)
    assert h["ok"] is True
    assert h["problems"] == []
    assert h["counts"] == {"units": 0, "issues": 0, "files": 0, "versions": 0, "logs": 0}
    assert h["sample"] == {"checked": 0, "total": 0}


def test_health_ok_with_normal_data(proj):
    """正常项目（单位+底稿+附件）健康检查通过。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理", defect_type="电费回收不及时",
                         defect_desc="正常底稿")
    # 造一个真实附件
    src = proj.root / "tmp_证据.pdf"
    src.write_bytes(b"evidence-content-001")
    f = proj.add_file(uid, src, "张三", orig_name="证据.pdf")
    proj.link_file(iid, f["id"], "张三")

    h = proj.health_check(sample_size=0)
    assert h["ok"] is True
    assert h["problems"] == []
    assert h["counts"]["units"] == 1
    assert h["counts"]["issues"] == 1
    assert h["counts"]["files"] == 1
    assert h["counts"]["versions"] == 1  # 初始 v1


def test_health_detects_orphan_link(proj):
    """关联表指向不存在的底稿/附件 → P0 orphan_link。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    iid = proj.add_issue(uid, "张三", department="营销管理", defect_type="电费回收不及时")
    src = proj.root / "tmp_a.pdf"
    src.write_bytes(b"a")
    f = proj.add_file(uid, src, "张三", orig_name="a.pdf")
    proj.link_file(iid, f["id"], "张三")

    # 直接删底稿记录（绕过业务层），留下孤儿关联
    with proj._lock, proj._conn:
        proj._conn.execute("DELETE FROM issues WHERE id=?", (iid,))
        proj._conn.commit()

    h = proj.health_check()
    types = {p["type"] for p in h["problems"]}
    assert "orphan_link" in types
    assert h["ok"] is False


def test_health_detects_orphan_issue_and_filerow(proj):
    """底稿/附件记录指向不存在单位 → orphan_issue / orphan_filerow。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    proj.add_issue(uid, "张三", department="营销管理", defect_type="电费回收不及时")
    src = proj.root / "tmp_b.pdf"
    src.write_bytes(b"b")
    proj.add_file(uid, src, "张三", orig_name="b.pdf")

    # 直接删单位记录，留下孤儿底稿 + 孤儿附件记录
    with proj._lock, proj._conn:
        proj._conn.execute("DELETE FROM units WHERE id=?", (uid,))
        proj._conn.commit()

    h = proj.health_check()
    types = {p["type"] for p in h["problems"]}
    assert "orphan_issue" in types
    assert "orphan_filerow" in types
    # 此时物理文件仍在，应同时报 orphan_phys（记录指向单位不存在但文件在库）
    # 注意：orphan_phys 判定只看文件是否登记，与单位存在性无关，所以不重复报


def test_health_detects_missing_file(proj):
    """files 有记录但物理文件缺失 → P0 missing_file。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    src = proj.root / "tmp_c.pdf"
    src.write_bytes(b"c")
    f = proj.add_file(uid, src, "张三", orig_name="c.pdf")

    # 物理删除附件
    (proj.root / f["rel_path"]).unlink()

    h = proj.health_check()
    types = {p["type"] for p in h["problems"]}
    assert "missing_file" in types
    assert any("c.pdf" in p["message"] for p in h["problems"])


def test_health_detects_missing_folder(proj):
    """文件夹实体物理目录缺失 → P0 missing_file（type 相同，message 标文件夹）。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    # 直接用 add_folder 造文件夹实体（需要临时文件）
    tmp = proj.root / "tmp_folder"
    tmp.mkdir()
    (tmp / "子文件.txt").write_text("folder-content", encoding="utf-8")
    folder = proj.add_folder(uid, [("子文件.txt", tmp / "子文件.txt")], "证据包", "张三")

    # 物理删除整个目录
    import shutil
    shutil.rmtree(proj.root / folder["rel_path"])

    h = proj.health_check()
    types = {p["type"] for p in h["problems"]}
    assert "missing_file" in types
    assert any("证据包" in p["message"] for p in h["problems"])


def test_health_detects_orphan_phys(proj):
    """附件库存在未登记文件 → P1 orphan_phys。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    # 往附件库里塞一个未登记文件
    stray = proj.root / "附件库" / f"unit_{uid}" / "stray.pdf"
    stray.write_bytes(b"stray")

    h = proj.health_check()
    types = {p["type"] for p in h["problems"]}
    assert "orphan_phys" in types
    assert any("stray.pdf" in p["message"] for p in h["problems"])


def test_health_ignores_hidden_files(proj):
    """附件库内隐藏文件（.DS_Store 等系统元数据）不报孤儿。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    (proj.root / "附件库" / ".DS_Store").write_bytes(b"meta")
    (proj.root / "附件库" / f"unit_{uid}" / ".DS_Store").write_bytes(b"meta")
    (proj.root / "附件库" / f"unit_{uid}" / ".hidden_dir").mkdir()
    (proj.root / "附件库" / f"unit_{uid}" / ".hidden_dir" / "x.pdf").write_bytes(b"x")

    h = proj.health_check()
    assert h["ok"] is True
    assert h["problems"] == []


def test_health_detects_hash_mismatch(proj):
    """附件内容被篡改 → P1 hash_mismatch（全量校验 sample_size=0）。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    src = proj.root / "tmp_d.pdf"
    src.write_bytes(b"original-content")
    f = proj.add_file(uid, src, "张三", orig_name="d.pdf")

    # 篡改物理文件内容（哈希将不一致）
    (proj.root / f["rel_path"]).write_bytes(b"tampered-content!!")

    h = proj.health_check(sample_size=0)
    types = {p["type"] for p in h["problems"]}
    assert "hash_mismatch" in types
    assert h["sample"]["checked"] == 1
    assert h["sample"]["total"] == 1


def test_health_sample_size_limits_hash_checks(proj):
    """sample_size 限制抽查数量；<=0 全量。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    for i in range(3):
        src = proj.root / f"tmp_{i}.pdf"
        src.write_bytes(f"content-{i}".encode())
        proj.add_file(uid, src, "张三", orig_name=f"f{i}.pdf")

    h = proj.health_check(sample_size=2)
    assert h["sample"]["total"] == 3
    assert h["sample"]["checked"] == 2

    h_all = proj.health_check(sample_size=0)
    assert h_all["sample"]["checked"] == 3


def test_manifest_written(proj):
    """manifest.json 生成：内容正确 + 原子写无 .tmp 残留。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    proj.add_issue(uid, "张三", department="营销管理", defect_type="电费回收不及时")

    m = proj.write_manifest()
    assert m["schema_version"] == SCHEMA_VERSION
    assert m["project_name"] == proj.root.name
    assert m["counts"]["units"] == 1
    assert m["counts"]["issues"] == 1
    assert m["created_at"]

    path = proj.root / "manifest.json"
    assert path.exists()
    assert not (proj.root / "manifest.json.tmp").exists()

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == m
