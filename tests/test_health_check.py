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
import os

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

    # 模拟来自旧版本/外部工具的损坏库。正常业务连接由外键阻止这种状态。
    with proj._lock:
        proj._conn.commit()
        proj._conn.execute("PRAGMA foreign_keys = OFF")
        proj._conn.execute("DELETE FROM issues WHERE id=?", (iid,))
        proj._conn.commit()
        proj._conn.execute("PRAGMA foreign_keys = ON")

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

    # 模拟受损旧库；正常业务连接会因 ON DELETE RESTRICT 拒绝此删除。
    with proj._lock:
        proj._conn.commit()
        proj._conn.execute("PRAGMA foreign_keys = OFF")
        proj._conn.execute("DELETE FROM units WHERE id=?", (uid,))
        proj._conn.commit()
        proj._conn.execute("PRAGMA foreign_keys = ON")

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


def test_health_detects_folder_member_change_by_directory_digest(proj):
    """文件夹内任一成员被改动，必须命中 P0 目录摘要不一致。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    tmp = proj.root / "tmp_digest_folder"
    tmp.mkdir()
    member = tmp / "子目录" / "证据.txt"
    member.parent.mkdir()
    member.write_text("原始证据", encoding="utf-8")
    folder = proj.add_folder(uid, [("子目录/证据.txt", member)], "合同资料", "张三")

    (proj.root / folder["rel_path"] / "子目录" / "证据.txt").write_text("已被修改", encoding="utf-8")

    health = proj.health_check(sample_size=0)
    types = {problem["type"] for problem in health["problems"]}
    assert "folder_hash_mismatch" in types
    assert any(problem["severity"] == "P0" for problem in health["problems"])


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


def test_health_ignores_only_system_metadata(proj):
    """仅 .DS_Store 属系统元数据；隐藏业务文件不得被静默忽略。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    (proj.root / "附件库" / ".DS_Store").write_bytes(b"meta")
    (proj.root / "附件库" / f"unit_{uid}" / ".DS_Store").write_bytes(b"meta")
    h = proj.health_check()
    assert h["ok"] is True
    assert h["problems"] == []

    hidden_business = proj.root / "附件库" / f"unit_{uid}" / ".hidden_dir" / "合同.pdf"
    hidden_business.parent.mkdir()
    hidden_business.write_bytes(b"business-evidence")
    h = proj.health_check()
    assert any(problem["type"] == "orphan_phys" and ".hidden_dir" in problem["message"] for problem in h["problems"])


def test_health_checks_hidden_folder_member_digest(proj):
    """隐藏业务成员参与文件夹摘要，篡改后必须阻断归档。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    source = proj.root / "tmp_hidden_folder"
    hidden = source / ".保密" / "合同.pdf"
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"original")
    folder = proj.add_folder(uid, [(".保密/合同.pdf", hidden)], "保密资料", "张三")

    stored = proj.root / folder["rel_path"] / ".保密" / "合同.pdf"
    stored.write_bytes(b"tampered")
    h = proj.health_check(sample_size=0)
    assert any(problem["type"] == "folder_hash_mismatch" for problem in h["problems"])


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


def test_health_sample_reuses_unchanged_hash_but_full_check_bypasses_cache(proj, tmp_path, monkeypatch):
    """常规抽查可复用未变化摘要；归档前全量检查必须重新读取物理文件。"""
    uid = proj.add_unit("华电集团XX电厂", "张三")
    source = tmp_path / "cache.pdf"
    source.write_bytes(b"cache-content")
    evidence = proj.add_file(uid, source, "张三")
    path = proj.root / evidence["rel_path"]
    original_hash = proj._sha256
    calls = 0

    def counted_hash(target):
        nonlocal calls
        calls += 1
        return original_hash(target)

    monkeypatch.setattr(proj, "_sha256", counted_hash)
    assert proj.health_check(sample_size=1)["ok"] is True
    assert proj.health_check(sample_size=1)["ok"] is True
    assert calls == 1

    path.write_bytes(b"cache-change!!")
    os.utime(path, None)
    assert proj.health_check(sample_size=1)["ok"] is False
    assert calls == 2

    assert proj.health_check(sample_size=0)["ok"] is False
    assert calls == 3


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
