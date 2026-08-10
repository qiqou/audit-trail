"""生成试点样本项目（T11 WP-07）。

用法：
    python scripts/gen_sample_project.py <项目目录> [种子]

产出（可复现，种子固定则结果一致）：
    - 10 个单位（华电集团下属电厂命名）
    - 200 条底稿（每单位 20 条，状态覆盖 草稿/编制完成/复核退回/已复核/已归档）
    - 500 个附件（每底稿 2-4 个，含少量文件夹实体），全部关联
    - 版块预设（营销管理/安全生产/财务审计/工程项目/物资采购/人力资源/环保节能/燃料管理）

验收口径（TASKS.md）：
    10 单位 / 200 问题 / 500 附件，全链路测试基线。
"""

import random
import shutil
import sys
from pathlib import Path

# 固定 stdout 为 UTF-8（Windows 控制台 cp936 下中文输出乱码/UnicodeEncodeError；与 CI 脚本同模式）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from database import AuditProject

UNIT_NAMES = [
    "华电XX发电有限公司", "华电YY热电有限公司", "华电ZZ风力发电有限公司",
    "华电AA光伏发电有限公司", "华电BB水电开发有限公司", "华电CC燃气发电有限公司",
    "华电DD储能科技有限公司", "华电EE供热有限公司", "华电FF新能源有限公司",
    "华电GG售电有限公司",
]
DEPTS = ["营销管理", "安全生产", "财务审计", "工程项目", "物资采购", "人力资源", "环保节能", "燃料管理"]
DEFECT_TYPES = [
    "电费回收不及时", "安全隐患未整改", "账实不符", "招投标程序不合规",
    "物资积压", "预算执行偏差", "环保指标超标", "燃料损耗超标",
]
REGULATIONS = [
    "《华电集团电费回收管理办法》", "《安全生产责任制考核办法》",
    "《企业会计准则第14号》", "《招投标管理办法》",
    "《物资采购管理细则》", "《全面预算管理办法》",
    "《环境保护管理标准》", "《燃料管理考核办法》",
]
SUGGESTIONS = [
    "加强催收管理，建立欠费台账并定期通报", "限期整改并复查销号",
    "按权责发生制调整账务，规范核算", "严格执行招投标程序，补办审批手续",
    "清理积压物资，制定处置计划", "强化预算刚性约束，严格审批调整",
    "落实环保设施运行维护，确保达标排放", "优化燃料采购与掺烧方案，降低损耗",
]
STATUSES = ["草稿", "编制完成", "复核退回", "已复核", "已归档"]
# 状态权重：草稿少、编制完成多、归档一部分（模拟真实项目进度）
STATUS_WEIGHTS = [15, 30, 15, 25, 15]


def _mk_issue(proj, uid, operator, seq):
    dept = random.choice(DEPTS)
    dtype = random.choice(DEFECT_TYPES)
    amount = random.choice(["", "", "", f"{random.randint(1, 500) * 10000}元",
                            f"{random.randint(1, 50)}万元"])
    return proj.add_issue(
        uid, operator,
        department=dept, defect_type=dtype,
        defect_desc=f"{dtype}：{seq} 号问题，涉及{dept}环节，存在{dtype.replace('，', '')}风险，需进一步核实并整改",
        amount=amount,
        regulation_basis=random.choice(REGULATIONS),
        suggestion=random.choice(SUGGESTIONS),
        author=operator, reviewer=random.choice(["张三", "李四", "王五", "赵六"]),
    )


def _mk_attachments(proj, uid, issue_id, operator, n):
    """给底稿造 n 个附件（1-3KB 文本），返回文件 id 列表。"""
    fids = []
    for k in range(n):
        p = proj.root / f"tmp_att_{issue_id}_{k}.txt"
        p.write_text(
            f"证据文件 issue={issue_id} k={k} 内容 {random.randrange(1 << 30):x}\n"
            + "x" * random.randint(800, 2500),
            encoding="utf-8",
        )
        rec = proj.add_file(uid, p, operator, orig_name=f"证据_{issue_id}_{k}.txt")
        fids.append(rec["id"])
    return fids


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/gen_sample_project.py <项目目录> [种子]")
        return 2
    root = Path(sys.argv[1])
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20260808
    random.seed(seed)

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    proj = AuditProject(root)
    operator = "样本生成器"
    proj.set_meta("departments", __import__("json").dumps(DEPTS, ensure_ascii=False))

    units = []
    for name in UNIT_NAMES:
        uid = proj.add_unit(name, operator)
        units.append(uid)

    issue_ids = []
    file_total = 0
    folder_total = 0
    for uid in units:
        for seq in range(1, 21):  # 每单位 20 条 → 200 条
            iid = _mk_issue(proj, uid, operator, seq)
            issue_ids.append(iid)
            n = random.randint(2, 4)
            fids = _mk_attachments(proj, uid, iid, operator, n)
            for fid in fids:
                proj.link_file(iid, fid, operator)
            file_total += n
            # 每 4 条底稿造 1 个文件夹实体附件
            if seq % 4 == 0:
                d = proj.root / f"tmp_fld_{iid}"
                d.mkdir(exist_ok=True)
                entries = []
                for j in range(3):
                    inner = d / f"内页{j}.txt"
                    inner.write_text(f"folder inner {iid}-{j}", encoding="utf-8")
                    entries.append((f"内页{j}.txt", inner))
                rec = proj.add_folder(uid, entries, f"资料包_{iid}", operator)
                proj.link_file(iid, rec["id"], operator)
                folder_total += 1
            # 按权重推进状态机（模拟编制-复核-归档流程）
            status = random.choices(STATUSES, weights=STATUS_WEIGHTS, k=1)[0]
            try:
                if status in ("编制完成", "复核退回", "已复核", "已归档"):
                    proj.change_status(iid, "编制完成", operator)
                if status in ("复核退回", "已复核", "已归档"):
                    proj.change_status(iid, "已复核", operator, comment="复核通过")
                if status == "已归档":
                    proj.change_status(iid, "已归档", operator)
                if status == "复核退回":
                    proj.change_status(iid, "复核退回", operator, comment="证据不足，请补充")
            except ValueError:
                pass  # 状态流转校验失败则保持当前状态，不影响样本生成

    # 清理临时源文件（附件已复制进附件库）
    for p in root.glob("tmp_*.txt"):
        p.unlink(missing_ok=True)
    for p in root.glob("tmp_fld_*"):
        shutil.rmtree(p, ignore_errors=True)

    proj.close()
    total_att = file_total + folder_total
    print(f"样本项目生成完成：{root}")
    print(f"  单位 {len(units)} 个 | 底稿 {len(issue_ids)} 条 | 附件 {total_att} 个（文件 {file_total} + 文件夹 {folder_total}）")
    print(f"  种子 {seed}（固定种子可复现）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
