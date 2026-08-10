"""跨端一致性检查（T11 WP-07）。

用法：
    python scripts/check_cross_platform.py <项目A> <项目B>

比对两个项目目录（同一项目包在 macOS / Windows 分别编辑后）的六项关键指标：
    1. 单位数（名称集合）
    2. 底稿数（含内容字段）
    3. 附件哈希（附件库所有文件 sha256 集合）
    4. 关联数（issue_files 总数）
    5. 版本数（issue_versions 总数）
    6. 日志数（audit_log 总数）

任一项不一致 → 退出码 1 并打印差异；全部一致 → 打印 OK。
注意：跨端编辑会各自产生新的 audit_log（操作留痕），日志数允许差异
（-L/--allow-log-diff 声明），其余五项必须一致。
"""

import hashlib
import sqlite3
import sys
from pathlib import Path

# 固定 stdout 为 UTF-8（Windows 控制台 cp936 下中文输出乱码/UnicodeEncodeError；与 CI 脚本同模式）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(proj_dir: Path) -> dict:
    db = proj_dir / "audit.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        units = sorted(r["name"] for r in conn.execute("SELECT name FROM units").fetchall())
        issues = sorted(
            tuple(r)
            for r in conn.execute(
                "SELECT u.name, i.seq, i.department, i.category, i.defect_type, i.defect_desc, i.amount, "
                "i.regulation_basis, i.suggestion, i.author, i.reviewer, i.status "
                "FROM issues i JOIN units u ON u.id=i.unit_id"
            ).fetchall()
        )
        link_map = sorted(
            (r["issue_id"], r["file_id"])
            for r in conn.execute("SELECT issue_id, file_id FROM issue_files").fetchall()
        )
        version_content = sorted(
            (r["issue_id"], r["version_no"], r["snapshot"])
            for r in conn.execute(
                "SELECT issue_id, version_no, snapshot FROM issue_versions"
            ).fetchall()
        )
        logs = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
        files_rows = conn.execute("SELECT rel_path, sha256, mime FROM files").fetchall()
    finally:
        conn.close()

    # 附件哈希：普通附件优先用登记 sha；文件夹证据递归核对每个成员，不能只比记录数。
    attach_hashes = []
    for r in files_rows:
        p = proj_dir / r["rel_path"]
        if r["mime"] == "folder":
            if not p.is_dir():
                attach_hashes.append((r["rel_path"], "MISSING_FOLDER"))
                continue
            members = [member for member in p.rglob("*") if member.is_file()]
            if not members:
                attach_hashes.append((r["rel_path"] + "/", "EMPTY_FOLDER"))
            for member in members:
                logical_path = f"{r['rel_path']}/{member.relative_to(p).as_posix()}"
                attach_hashes.append((logical_path, _sha256(member)))
            continue
        if r["sha256"]:
            attach_hashes.append((r["rel_path"], r["sha256"]))
        else:
            attach_hashes.append((r["rel_path"], _sha256(p) if p.is_file() else "MISSING"))
    attach_hashes.sort()

    return {
        "units": units,
        "issues": issues,
        "links": len(link_map),
        "link_map": link_map,
        "versions": len(version_content),
        "version_content": version_content,
        "logs": logs,
        "attach_hashes": attach_hashes,
    }


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python scripts/check_cross_platform.py <项目A> <项目B> [-L]")
        print("  -L / --allow-log-diff  允许 audit_log 数差异（跨端编辑各自留痕）")
        return 2
    allow_log_diff = "-L" in sys.argv or "--allow-log-diff" in sys.argv
    dirs = [p for p in sys.argv[1:3]]
    a_dir, b_dir = Path(dirs[0]), Path(dirs[1])
    if not (a_dir / "audit.db").exists() or not (b_dir / "audit.db").exists():
        print("两个目录都必须包含 audit.db")
        return 2

    a, b = snapshot(a_dir), snapshot(b_dir)
    keys = [
        ("单位数/名称", "units", a["units"] == b["units"]),
        ("底稿数/内容", "issues", a["issues"] == b["issues"]),
        ("附件哈希", "attach_hashes", a["attach_hashes"] == b["attach_hashes"]),
        ("关联映射", "link_map", a["link_map"] == b["link_map"]),
        ("版本内容", "version_content", a["version_content"] == b["version_content"]),
    ]
    print(f"A: {a_dir}")
    print(f"  单位 {len(a['units'])} | 底稿 {len(a['issues'])} | 附件 {len(a['attach_hashes'])} | 关联 {a['links']} | 版本 {a['versions']} | 日志 {a['logs']}")
    print(f"B: {b_dir}")
    print(f"  单位 {len(b['units'])} | 底稿 {len(b['issues'])} | 附件 {len(b['attach_hashes'])} | 关联 {b['links']} | 版本 {b['versions']} | 日志 {b['logs']}")

    ok = True
    for label, key, same in keys:
        if not same:
            ok = False
            print(f"  ✗ {label} 不一致")
    if a["logs"] != b["logs"]:
        if allow_log_diff:
            print("  ~ 日志数不同（-L 允许：跨端编辑各自留痕，属预期）")
        else:
            ok = False
            print(f"  ✗ 日志数 不一致（A={a['logs']} B={b['logs']}；跨端编辑各自留痕，可用 -L 放行）")

    if ok:
        print("✅ 跨端一致性检查通过：单位/底稿/附件哈希/关联/版本全部一致"
              + ("（日志差异已放行）" if a["logs"] != b["logs"] else ""))
        return 0
    print("❌ 跨端一致性检查未通过")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
