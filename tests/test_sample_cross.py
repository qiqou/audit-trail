"""T11 样本生成 + 跨端一致性 + 全链路用例。

覆盖（对应 TASKS.md T11 验收）：
- gen_sample_project.py：10 单位 / 200 底稿 / 附件数>=500，固定种子可复现
- check_cross_platform.py：两端同操作后 单位/底稿/附件哈希/关联/版本 一致
- 全链路：编辑 → 状态流转 → 导出台账 → 打包归档 → 备份 → 健康检查（哈希全对）
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BACKEND = ROOT / "backend"


def _run(args, cwd=None):
    # 子进程固定 UTF-8 输出（脚本内 reconfigure），这里显式按 UTF-8 解码——
    # 否则 Windows locale(cp936) 解码 UTF-8 字节会乱码（中文断言失败）
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600, check=False,
                       cwd=cwd or str(ROOT))
    assert r.returncode == 0, f"命令失败: {args}\nstdout={r.stdout}\nstderr={r.stderr}"
    return r.stdout


def test_gen_sample_project(tmp_path):
    """样本生成：10 单位 / 200 底稿 / 附件>=500；固定种子复现。"""
    out1 = tmp_path / "s1"
    out2 = tmp_path / "s2"
    _run([sys.executable, str(SCRIPTS / "gen_sample_project.py"), str(out1), "20260808"])
    _run([sys.executable, str(SCRIPTS / "gen_sample_project.py"), str(out2), "20260808"])

    import sqlite3

    def counts(root):
        conn = sqlite3.connect(str(root / "audit.db"))
        try:
            units = conn.execute("SELECT COUNT(*) c FROM units").fetchone()[0]
            issues = conn.execute("SELECT COUNT(*) c FROM issues").fetchone()[0]
            files = conn.execute("SELECT COUNT(*) c FROM files").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) c FROM issue_files").fetchone()[0]
        finally:
            conn.close()
        return units, issues, files, links

    u1, i1, f1, l1 = counts(out1)
    assert u1 == 10
    assert i1 == 200
    assert f1 >= 500
    assert l1 == f1  # 所有附件已关联
    # 固定种子可复现
    assert counts(out1) == counts(out2)


def test_cross_platform_same_ops(tmp_path):
    """跨端一致性：两份同源项目做相同操作后，核心指标一致。"""
    a = tmp_path / "proj_a"
    b = tmp_path / "proj_b"
    _run([sys.executable, str(SCRIPTS / "gen_sample_project.py"), str(a), "42"])
    shutil.copytree(a, b)

    script = '''
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp936 控制台防乱码
from pathlib import Path
sys.path.insert(0, %r)
from database import AuditProject

def ops(root, operator):
    proj = AuditProject(Path(root))
    units = proj.list_units()
    issues = proj.list_issues(units[0]["id"])
    e = next(x for x in issues if (x["status"] or "草稿") != "已归档")
    proj.update_issue(e["id"], operator, defect_desc="两端统一修改描述")
    d = next((x for x in issues if (x["status"] or "草稿") == "草稿"), None)
    if d: proj.change_status(d["id"], "编制完成", operator)
    r = next((x for x in issues if (x["status"] or "") == "已复核"), None)
    if r: proj.change_status(r["id"], "已归档", operator)
    proj.close()

ops(%r, "A用户")
ops(%r, "B用户")
''' % (str(BACKEND), str(a), str(b))  # noqa: UP031 多行脚本插值用 % 最直观
    _run([sys.executable, "-c", script])

    out = _run([sys.executable, str(SCRIPTS / "check_cross_platform.py"), str(a), str(b), "-L"])
    assert "✅" in out


def test_full_chain_on_sample(tmp_path):
    """全链路：编辑 → 流转 → 台账 → 归档 → 备份 → 健康检查。"""
    proj_dir = tmp_path / "sample"
    _run([sys.executable, str(SCRIPTS / "gen_sample_project.py"), str(proj_dir), "7"])

    script = '''
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp936 控制台防乱码
from pathlib import Path
sys.path.insert(0, %r)
from database import AuditProject
from export import export_excel, package_project, create_backup

proj = AuditProject(Path(%r))
op = "全链路用户"
units = proj.list_units()
issues = proj.list_issues(units[0]["id"])
e = next(x for x in issues if (x["status"] or "草稿") != "已归档")
assert proj.update_issue(e["id"], op, defect_desc="全链路修改") is True
d = next((x for x in issues if (x["status"] or "草稿") == "草稿"), None)
if d:
    proj.change_status(d["id"], "编制完成", op)

r = export_excel(proj, scope="project", operator=op)
assert r["count"] == 200
r2 = package_project(proj, scope="all")
assert r2["units"] == 10 and r2["issues"] == 200
r3 = create_backup(proj)
assert r3["abs_path"]

h = proj.health_check(sample_size=0)
assert h["ok"], h["problems"][:3]
assert h["sample"]["checked"] == h["sample"]["total"] > 0
proj.close()
print("全链路闭环 OK")
''' % (str(BACKEND), str(proj_dir))  # noqa: UP031 多行脚本插值用 % 最直观
    out = _run([sys.executable, "-c", script])
    assert "全链路闭环 OK" in out


def test_sample_passes_health_check(tmp_path):
    """样本项目通过健康检查（数据自洽，无孤儿/缺失/哈希不符）。"""
    proj_dir = tmp_path / "sample"
    _run([sys.executable, str(SCRIPTS / "gen_sample_project.py"), str(proj_dir), "99"])

    script = '''
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows cp936 控制台防乱码
from pathlib import Path
sys.path.insert(0, %r)
from database import AuditProject

proj = AuditProject(Path(%r))
h = proj.health_check(sample_size=0)
assert h["ok"], h["problems"][:3]
assert h["sample"]["checked"] == h["sample"]["total"] > 0
proj.close()
print("样本项目健康 OK")
''' % (str(BACKEND), str(proj_dir))  # noqa: UP031 多行脚本插值用 % 最直观
    out = _run([sys.executable, "-c", script])
    assert "样本项目健康 OK" in out
