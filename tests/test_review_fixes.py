"""全面审查修复的回归用例（2026-08 审查）。

覆盖：
- merge_import 防解压炸弹（超大包拒绝）
- merge_import 防目录穿越（非法路径成员拒绝）
- version_counts 批量版本数（N+1 优化）
"""

import zipfile

from database import AuditProject
from export import merge_import, package_project


def _write_zip(zip_path, members):
    """members: [(name, content_bytes), ...]"""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in members:
            zf.writestr(name, content)


def test_merge_import_rejects_path_traversal(proj):
    """合并导入：含 ../ 路径成员的包被拒绝（防目录穿越，与 merge_backups 对齐）。"""
    p = proj.root / "evil.zip"
    _write_zip(p, [
        ("../escape.txt", b"escaped"),
        ("审计问题汇总.xlsx", b"not really xlsx"),
    ])
    r = merge_import(proj, [p], "张三")
    assert r["imported"] == 0
    assert any("非法路径" in e for e in r["errors"]), r["errors"]


def test_merge_import_rejects_oversize(proj, monkeypatch):
    """合并导入：解压总量超过上限的包被拒绝（防解压炸弹）。"""
    import limits

    p = proj.root / "big.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("big.bin", b"x" * 1024)
    # 上限设为 0：任何非空包都超限（merge_import 函数内 from limits import 每次重新读取）
    monkeypatch.setattr(limits, "MAX_EXTRACT_TOTAL", 0)
    r = merge_import(proj, [p], "张三")
    assert r["imported"] == 0
    assert any("上限" in e for e in r["errors"]), r["errors"]


def test_merge_import_rejects_too_many_archive_members(proj, monkeypatch):
    """即使成员均为空，大量 ZIP 条目也应在解压前被拒绝。"""
    import limits

    p = proj.root / "too_many.zip"
    _write_zip(p, [("a.txt", b""), ("b.txt", b"")])
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 1)
    result = merge_import(proj, [p], "张三")
    assert result["imported"] == 0
    assert any("超过 1 个" in error for error in result["errors"]), result["errors"]


def test_archive_merge_links_files_and_folders_only_to_new_issue(proj, tmp_path):
    """归档根目录、普通附件和文件夹证据均恢复，且不误挂到原有同定性底稿。"""
    target_unit = proj.add_unit("单位A", "张三")
    original_issue = proj.add_issue(target_unit, "张三", defect_type="同一问题")

    source = AuditProject(tmp_path / "来源项目")
    try:
        source_unit = source.add_unit("单位A", "李四")
        source_issue = source.add_issue(source_unit, "李四", department="版块A", defect_type="同一问题")
        evidence_path = tmp_path / "证据.txt"
        evidence_path.write_text("evidence", encoding="utf-8")
        evidence = source.add_file(source_unit, evidence_path, "李四")
        source.link_file(source_issue, evidence["id"], "李四")
        folder_member = tmp_path / "合同.pdf"
        folder_member.write_bytes(b"%PDF folder evidence")
        folder = source.add_folder(
            source_unit,
            [("子目录/合同.pdf", str(folder_member))],
            "合同资料",
            "李四",
        )
        source.link_file(source_issue, folder["id"], "李四")
        archive = package_project(source)["abs_path"]

        result = merge_import(proj, [archive], "张三")
    finally:
        source.close()

    assert result["imported"] == 1
    assert result["files"] == 2
    assert proj.files_for_issue(original_issue) == []
    imported_issue = next(issue for issue in proj.list_issues(target_unit)
                          if issue["id"] != original_issue)
    linked = proj.files_for_issue(imported_issue["id"])
    assert {item["orig_name"] for item in linked} == {"证据.txt", "合同资料"}
    restored_folder = next(item for item in linked if item["mime"] == "folder")
    assert (proj.root / restored_folder["rel_path"] / "子目录" / "合同.pdf").is_file()


def test_version_counts_batch(proj):
    """version_counts：一次查询返回全部底稿版本数（含新建底稿的 v1）。"""
    u1 = proj.add_unit("华电XX电厂", "张三")
    i1 = proj.add_issue(u1, "张三", department="营销", defect_type="电费回收不及时")
    i2 = proj.add_issue(u1, "张三", department="财务", defect_type="账实不符")
    proj.update_issue(i1, "张三", defect_desc="修改一次")

    counts = proj.version_counts()
    assert counts[i1] == 2  # v1 + 1 次修改
    assert counts[i2] == 1  # 仅 v1
    assert counts.get(i1) == len(proj.list_versions(i1))
