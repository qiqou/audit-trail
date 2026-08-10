"""导出 / 打包 / 备份 功能模块。

输出约定：
- 所有导出文件带时间戳后缀（YYYYMMDD_HHMMSS），绝不覆盖旧输出
- 导出文件落在项目 输出/ 目录（随项目走，自包含）
- 备份 .auditbak 落在项目上级目录（备份不应混入项目数据）
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from config import PROJECT_EXT
from database import ATTACH_DIR, OUT_DIR, AuditProject, _now, _safe
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from platform_adapter import harden_project


def _sha256_of_file(path) -> str:
    """计算文件 sha256（归档清单核对用）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extracted_attachment_path(extract_root: Path, rel_path: str) -> Path:
    """从不可信备份数据库读取附件路径时，仍限制在已解压的附件库内。"""
    candidate = (extract_root / PurePosixPath(str(rel_path or "").replace("\\", "/"))).resolve()
    attachment_root = (extract_root / ATTACH_DIR).resolve()
    if candidate == attachment_root or not candidate.is_relative_to(attachment_root):
        raise ValueError("备份数据库包含非法附件路径")
    return candidate


def _validate_archive_size(zf: zipfile.ZipFile, label: str) -> list[zipfile.ZipInfo]:
    """校验不可信压缩包的成员数与解压总量，返回已读取的成员列表。"""
    from limits import MAX_ARCHIVE_MEMBERS, MAX_EXTRACT_TOTAL, human_size

    infos = zf.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"{label}包含超过 {MAX_ARCHIVE_MEMBERS} 个文件或目录，拒绝处理")
    total = sum(info.file_size for info in infos)
    if total > MAX_EXTRACT_TOTAL:
        raise ValueError(f"{label}解压总量超过上限 {human_size(MAX_EXTRACT_TOTAL)}，拒绝处理")
    return infos

# 汇总表列定义：字段名 → (表头, 列宽)
SUMMARY_HEADERS = [
    ("seq",             "序号",       6),
    ("unit_name",       "被审计单位", 16),
    ("department",      "所属版块",   14),
    ("category",        "问题分类",   14),
    ("defect_type",     "缺陷定性",   14),
    ("defect_desc",     "缺陷描述",   42),
    ("amount",          "问题金额",   10),
    ("regulation_basis","制度依据",   30),
    ("suggestion",      "审计建议",   30),
    ("author",          "编写人",     10),
    ("reviewer",        "审核人",     10),
    ("status",          "状态",       8),
    ("version_no",      "版本数",     8),
    ("file_count",      "附件数",     8),
    ("evidence",        "证据提示",   14),
]

HF = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
HFILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
HA = Alignment(horizontal="center", vertical="center", wrap_text=True)
THIN = Border(left=Side(style="thin"), right=Side(style="thin"),
              top=Side(style="thin"), bottom=Side(style="thin"))
DF = Font(name="微软雅黑", size=10)
DA = Alignment(vertical="center", wrap_text=True)
AF = PatternFill(start_color="F2F7FB", end_color="F2F7FB", fill_type="solid")
TITLE_F = Font(name="微软雅黑", size=12, bold=True)


def _now_ts() -> str:
    """时间戳到毫秒，配合 _unique_path 保证输出文件名不重复（审查 F-04 修复）。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _unique_path(out_dir: Path, base_name: str) -> Path:
    """输出路径防覆盖（审查 F-04 修复）：已存在则追加 _1/_2/_3... 序号。

    返回的路径确保当前不存在，绝不覆盖旧输出。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / base_name
    if not p.exists():
        return p
    stem = p.stem
    for i in range(1, 10000):
        cand = out_dir / f"{stem}_{i}{p.suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"无法生成唯一文件名：{base_name}")




def _formula_like(value) -> bool:
    """识别可能被电子表格程序解释为公式的用户文本。"""
    return isinstance(value, str) and value.startswith(("=", "+", "-", "@"))


def _collect_rows(proj: AuditProject, scope: str = "project", unit_id: int = None,
                   unit_ids: list[int] = None) -> tuple[list[dict], str]:
    """收集汇总行。scope: unit(单单位)/selected(多单位)/project(全部)。返回 (rows, 范围说明)。"""
    rows = []
    if scope == "unit" and unit_id:
        units = [u for u in proj.list_units() if u["id"] == unit_id]
        scope_desc = f"单位：{units[0]['name']}" if units else "单位"
    elif scope == "selected" and unit_ids:
        units = [u for u in proj.list_units() if u["id"] in unit_ids]
        scope_desc = f"勾选单位 {len(units)} 个"
    else:
        units = proj.list_units()
        scope_desc = "全部单位"
    # T8 台账 N+1 优化：一次查询全部版本数，不再每行调 list_versions（审查）
    ver_counts = proj.version_counts()
    for u in units:
        for iss in proj.list_issues(u["id"]):
            row = {k: iss.get(k, "") for k, _v, _w in SUMMARY_HEADERS if k not in ("unit_name", "file_count", "version_no", "evidence")}
            # 序号列 = 底稿编号（前缀+序号+后缀），与树/详情一致，作为唯一识别码
            row["seq"] = proj.issue_no(iss["seq"])
            row["unit_name"] = u["name"]
            row["file_count"] = iss.get("file_count", 0)
            # 版本数：初始 v1 + 每次修改留版本
            row["version_no"] = ver_counts.get(iss["id"], 1)
            # 证据提示：无附件 → 提示缺证据（T8 台账增强）
            row["evidence"] = "" if row["file_count"] else "缺证据"
            rows.append(row)
    return rows, scope_desc


def export_excel(proj: AuditProject, scope: str = "project", operator: str = "",
                   unit_id: int = None, unit_ids: list[int] = None) -> dict:
    """生成问题汇总表 Excel 到项目 输出/。scope: unit/selected(需 unit_ids) / project。"""
    if scope not in ("unit", "selected", "project"):
        raise ValueError("导出范围无效")
    if scope == "unit" and not unit_id:
        raise ValueError("导出当前单位需指定单位")
    if scope == "selected" and not unit_ids:
        raise ValueError("导出勾选单位需指定单位列表")
    rows, scope_desc = _collect_rows(proj, scope, unit_id, unit_ids)

    wb = Workbook()
    ws = wb.active
    ws.title = "审计问题汇总"

    # 标题行
    ncol = len(SUMMARY_HEADERS)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    tc = ws.cell(row=1, column=1)
    tc.value = f"审计项目：{proj.project_name}    {scope_desc}    导出时间：{_now()}    导出人：{operator}"
    tc.font = TITLE_F
    tc.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 28

    # 表头
    for ci, (_k, label, width) in enumerate(SUMMARY_HEADERS, 1):
        c = ws.cell(row=2, column=ci, value=label)
        c.font, c.fill, c.alignment, c.border = HF, HFILL, HA, THIN
        ws.column_dimensions[chr(64 + ci)].width = width

    # 数据（斑马纹）
    for ri, row in enumerate(rows, 3):
        for ci, (k, _label, _w) in enumerate(SUMMARY_HEADERS, 1):
            v = row.get(k, "")
            c = ws.cell(row=ri, column=ci, value=v)
            if _formula_like(v):
                # 明确写为字符串，既阻止公式执行，也避免前导单引号污染归档回导后的原始文本。
                c.data_type = "s"
            c.font, c.alignment, c.border = DF, DA, THIN
            if ri % 2 == 0:
                c.fill = AF

    ws.freeze_panes = "A3"

    out_dir = proj.root / OUT_DIR
    out_dir.mkdir(exist_ok=True)
    suffix = "全部单位" if scope == "project" else ("勾选单位" if scope == "selected" else "当前单位")
    filename = f"问题汇总_{_safe(proj.project_name)}_{suffix}_{_now_ts()}.xlsx"
    # 防覆盖：同秒/同名已存在时自动追加序号（审查 F-04 修复）
    out_path = _unique_path(out_dir, filename)
    wb.save(out_path)
    return {"filename": out_path.name, "abs_path": str(out_path), "count": len(rows)}


def _package_project_in_temp(proj: AuditProject, tmp: Path, scope: str = "all",
                             unit_ids: list[int] = None,
                             group_by_dept: bool = False) -> dict:
    """按项目结构打包 ZIP（含汇总表 + 附件），保留完整目录层级。

    目录结构：
      项目名称/                      一级
      附件-单位名称/                 二级
      版块分类/                      （三级，group_by_dept 勾选时启用）
      序号.问题定性/                  末级
    scope: all=全部单位 / selected=勾选单位（unit_ids）
    """
    if scope not in {"all", "selected"}:
        raise ValueError("归档范围无效，请选择全部单位或勾选单位")
    selected_ids = list(dict.fromkeys(unit_ids or []))
    if scope == "selected" and not selected_ids:
        raise ValueError("勾选单位归档时请至少选择一个单位，系统不会自动改为全部单位")
    known_ids = {unit["id"] for unit in proj.list_units()}
    missing_ids = [unit_id for unit_id in selected_ids if unit_id not in known_ids]
    if missing_ids:
        raise ValueError(f"勾选单位不存在或已被删除：{missing_ids}")

    root = tmp / _safe(proj.project_name)
    root.mkdir()

    # 汇总表放根（范围与打包一致）
    if scope == "selected":
        summary = export_excel(proj, scope="selected", unit_ids=selected_ids)
    else:
        summary = export_excel(proj, scope="project")
    shutil.copy2(summary["abs_path"], root / "审计问题汇总.xlsx")
    # 汇总表只是归档包的中间产物，不应额外残留一份到“输出”目录。
    Path(summary["abs_path"]).unlink(missing_ok=True)

    # 单位 → [版块] → 序号.问题定性 → 附件
    units = proj.list_units()
    if scope == "selected":
        units = [u for u in units if u["id"] in selected_ids]
    issue_count = 0
    for u in units:
        udir = root / f"附件-{_safe(u['name'])}"
        issues = proj.list_issues(u["id"])
        if group_by_dept:
            # 按版块分类：每个版块内序号重新从 1 编号
            by_dept = {}
            for iss in issues:
                d = _safe(iss.get("department") or "未分版块")
                by_dept.setdefault(d, []).append(iss)
            for d, iss_list in by_dept.items():
                for i, iss in enumerate(iss_list, 1):
                    # 分组视图内编号同样应用规则（分类内序号）
                    label = f"{proj.issue_no(i)}.{_safe(iss.get('defect_type') or '未定性')}"
                    idir = udir / d / label
                    idir.mkdir(parents=True, exist_ok=True)
                    for f in proj.files_for_issue(iss["id"]):
                        src_f = proj.attachment_path(f["rel_path"])
                        if f.get("mime") == "folder" and src_f.is_dir():
                            # 文件夹实体：目录原样复制（不打包）
                            shutil.copytree(src_f, _unique_path(idir, _safe(f["orig_name"])))
                        elif src_f.exists():
                            shutil.copy2(src_f, _unique_path(idir, _safe(f["orig_name"])))
                    issue_count += 1
        else:
            for iss in issues:
                # 目录名用底稿编号（前缀+序号+后缀），与台账/树一致
                label = f"{proj.issue_no(iss['seq'])}.{_safe(iss.get('defect_type') or '未定性')}"
                idir = udir / label
                idir.mkdir(parents=True, exist_ok=True)
                for f in proj.files_for_issue(iss["id"]):
                    src_f = proj.attachment_path(f["rel_path"])
                    if f.get("mime") == "folder" and src_f.is_dir():
                        shutil.copytree(src_f, _unique_path(idir, _safe(f["orig_name"])))
                    elif src_f.exists():
                        shutil.copy2(src_f, _unique_path(idir, _safe(f["orig_name"])))
                issue_count += 1

    out_dir = proj.root / OUT_DIR
    out_dir.mkdir(exist_ok=True)
    filename = f"归档_{_safe(proj.project_name)}_{_now_ts()}.zip"
    # 防覆盖：同秒/同名已存在时自动追加序号（审查 F-04 修复）
    out_path = _unique_path(out_dir, filename)
    # 归档清单（T8）：ZIP 内所有文件相对路径 + 大小 + sha256，供接收方核对
    manifest_lines = [
        "归档清单",
        f"项目：{proj.project_name}",
        f"生成时间：{_now()}",
        f"单位数：{len(units)}    底稿数：{issue_count}",
        "",
        "文件清单（相对路径 / 大小 / sha256）：",
    ]
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 空目录也写入（无附件的问题夹保持目录结构，方便对照）
        for dirpath, dirs, _files in os.walk(tmp):
            # 忽略隐藏目录（.DS_Store 等 macOS 元数据）
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            rel = Path(dirpath).relative_to(tmp)
            # ZIP 内路径统一正斜杠（as_posix）：Windows 上 str(Path) 是反斜杠，会让 ZIP 目录分隔符不一致
            zf.writestr(rel.as_posix() + "/", "")
            for fn in _files:
                if fn.startswith("."):
                    continue   # 忽略隐藏文件
                fp = Path(dirpath) / fn
                zf.write(fp, fp.relative_to(tmp).as_posix())
                # 在归档清单里登记：相对路径 / 大小 / sha256（读取的是打包前的源文件）
                sha = _sha256_of_file(fp)
                manifest_lines.append(f"{fp.relative_to(tmp).as_posix()}\t{fp.stat().st_size}\t{sha}")
        manifest_lines.append(f"\n共 {len(manifest_lines) - 6} 个文件")
        zf.writestr("归档清单.txt", "\n".join(manifest_lines), compress_type=zipfile.ZIP_DEFLATED)
    return {"filename": out_path.name, "abs_path": str(out_path), "units": len(units),
            "issues": issue_count, "files": len(manifest_lines) - 7}


def package_project(proj: AuditProject, scope: str = "all", unit_ids: list[int] = None,
                    group_by_dept: bool = False) -> dict:
    """在受管临时目录中生成归档，成功或失败都会清理中间文件。"""
    with tempfile.TemporaryDirectory(prefix="audit_pkg_") as temp_dir:
        return _package_project_in_temp(
            proj,
            Path(temp_dir),
            scope=scope,
            unit_ids=unit_ids,
            group_by_dept=group_by_dept,
        )


def create_backup(proj: AuditProject) -> dict:
    """备份：audit.db + 附件库 打包为 .auditbak，存项目上级目录。

    原子性（审查 F-04 修复）：
    - 用 sqlite3 backup API 生成一致性数据库快照（不直接复制正在使用的 db）
    - ZIP 先写同目录 .tmp 文件，完成后 os.replace 原子落盘（不会出现半成品备份）
    """
    import sqlite3

    from limits import MAX_EXTRACT_TOTAL, human_size

    # 恢复端会拒绝解压后超过该上限的备份；创建端也提前校验，避免用户拿到
    # 一个本工具无法恢复的 .auditbak。忽略隐藏文件的规则与 copytree 保持一致。
    source_total = proj.db_path.stat().st_size if proj.db_path.exists() else 0
    attachment_root = proj.root / ATTACH_DIR
    if attachment_root.exists():
        for source in attachment_root.rglob("*"):
            if source.is_file() and not any(part.startswith(".") for part in source.relative_to(attachment_root).parts):
                source_total += source.stat().st_size
    if source_total > MAX_EXTRACT_TOTAL:
        raise ValueError(
            f"项目备份内容约 {human_size(source_total)}，超过可恢复上限 "
            f"{human_size(MAX_EXTRACT_TOTAL)}；请拆分或清理附件后再备份"
        )

    bak_name = f"{_safe(proj.project_name)}_备份_{_now_ts()}.auditbak"
    # 防覆盖：同秒/同名已存在时自动追加序号（审查 F-04 修复）
    bak_path = _unique_path(proj.root.parent, bak_name)
    tmp_bak = bak_path.with_suffix(bak_path.suffix + ".tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="audit_bak_") as td:
            td_path = Path(td)
            # 1) 一致性数据库快照
            snap_path = td_path / "audit.db"
            src = proj._conn
            dst = sqlite3.connect(str(snap_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
            # 2) 附件库拷入临时目录
            att_src = proj.root / ATTACH_DIR
            att_tmp = td_path / ATTACH_DIR
            if att_src.exists():
                shutil.copytree(att_src, att_tmp,
                                ignore=shutil.ignore_patterns(".*", ".DS_Store"))
            # 3) 打包到同目录 .tmp，完成后再原子改名
            with zipfile.ZipFile(tmp_bak, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(snap_path, "audit.db")
                if att_tmp.exists():
                    for fp in att_tmp.rglob("*"):
                        if fp.is_file():
                            zf.write(fp, f"{ATTACH_DIR}/{fp.relative_to(att_tmp).as_posix()}")
            db_size = snap_path.stat().st_size
        os.replace(tmp_bak, bak_path)
    except Exception:
        tmp_bak.unlink(missing_ok=True)
        raise
    return {"filename": bak_path.name, "abs_path": str(bak_path),
            "db_size": db_size}


def restore_backup(bak_path, target_dir) -> dict:
    """恢复备份：解包 .auditbak 到目标目录（必须为空或不存在）。

    原子性（审查 F-04 修复）：
    - 先解包到临时目录，校验 ZIP 完整、路径安全、audit.db 存在且 integrity_check 通过
    - 全部校验通过后才创建目标目录并迁移内容；失败不留下半成品目标目录
    目录伪装（V3.2）：恢复目标与新建项目一致，自动追加 .auditproj 后缀并隐藏；
    用户选择已有空文件夹时消费该空壳，不留残留。
    """
    import sqlite3

    bak = Path(bak_path)
    target = Path(target_dir).expanduser()
    if not bak.is_file():
        raise ValueError(f"备份文件不存在：{bak}")
    # 目录伪装：恢复目标自动追加 .auditproj 后缀（已带则不重复加），
    # 落位后 harden_project 隐藏，与新建项目行为一致。
    final = target
    if target.name and not target.name.endswith(PROJECT_EXT):
        final = target.with_name(target.name + PROJECT_EXT)
    target_existed = target.exists()
    if target_existed:
        if not target.is_dir():
            raise ValueError(f"恢复目标不是文件夹：{target}")
        if any(target.iterdir()):
            raise ValueError(f"目标目录非空，请选择空文件夹或新目录：{target}")
    if final != target and final.exists():
        raise ValueError(f"恢复目标已存在同名项目目录：{final}。请更换目标目录")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 临时目录放在目标同一文件系统，校验通过后用 os.replace 原子落位。
        stage = Path(tempfile.mkdtemp(prefix=".audit_restore_", dir=target.parent))
    except OSError as e:
        raise ValueError(f"无法准备恢复目录：{e}") from e
    try:
        try:
            with zipfile.ZipFile(bak) as zf:
                bad = zf.testzip()
                if bad:
                    raise ValueError(f"备份文件损坏：{bad}")
                # 防解压炸弹：同时限制成员数与解压总量。
                _validate_archive_size(zf, "备份包")
                # 防目录穿越：校验所有成员名（恶意包可用 ../ 逃逸目标目录）
                target_res = stage.resolve()
                for name in zf.namelist():
                    member = (target_res / name).resolve()
                    if member != target_res and not member.is_relative_to(target_res):
                        raise ValueError(f"备份包包含非法路径：{name}")
                zf.extractall(stage)
        except zipfile.BadZipFile:
            raise ValueError("文件不是有效的备份包（.auditbak）")
        if not (stage / "audit.db").exists():
            raise ValueError("备份包缺少 audit.db，恢复失败")
        # 校验数据库完整性和附件相对路径。files.rel_path 来自不可信备份，
        # 必须在移动到目标目录之前拒绝越界记录，确保失败不落半成品项目。
        try:
            conn = sqlite3.connect(str(stage / "audit.db"))
            try:
                status = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if status == "ok":
                    try:
                        rows = conn.execute("SELECT rel_path FROM files").fetchall()
                    except sqlite3.OperationalError:
                        rows = []  # 兼容极早期无附件表的项目，后续打开时会迁移
                    for (rel_path,) in rows:
                        _extracted_attachment_path(stage, rel_path)
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"备份数据库无法读取：{e}")
        if status != "ok":
            raise ValueError(f"备份数据库完整性校验未通过（{status}），拒绝恢复")
        # 校验全部通过 → 原子迁移到 final；已有空目录仅在最后一刻移除。
        if target_existed:
            target.rmdir()
        try:
            os.replace(stage, final)
        except OSError:
            if target_existed:
                target.mkdir(exist_ok=True)
            raise
    except ValueError:
        raise
    except OSError as e:
        raise ValueError(f"恢复备份失败：{e}。请检查目标目录权限和剩余空间") from e
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    harden_project(final)  # 隐藏目录（失败不阻断，与新建项目一致）
    return {"path": str(final), "project_name": final.name}


# ═══════════════════════════════════════════════
# 导入问题汇总（Excel）
# ═══════════════════════════════════════════════

IMPORT_HEADERS = ["被审计单位*", "所属版块*", "问题分类", "缺陷定性*", "缺陷描述",
                  "问题金额", "制度依据", "审计建议", "编写人", "审核人"]


def build_import_template(path):
    """生成导入模板 xlsx：Sheet1 表头，Sheet2 填写说明。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "导入模板"
    ws.append(IMPORT_HEADERS)
    fill = PatternFill("solid", fgColor="4472C4")
    for cell in ws[1]:
        cell.fill = fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center")
    widths = [16, 14, 14, 18, 46, 12, 32, 32, 10, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = "A2"
    # 示例行（灰色，用户可整行删除）
    ws.append(["华电集团XX电厂", "营销管理", "经营管理", "电费回收不及时",
               "示例：某用户电费长期拖欠未回收，金额约120万元。", "120万",
               "《供电营业规则》第XX条", "建议加强催收并建立台账。", "张三", "李四"])
    for cell in ws[2]:
        cell.font = Font(color="808080", italic=True)

    ws2 = wb.create_sheet("填写说明")
    notes = [
        ["问题汇总导入 · 填写说明"],
        [""],
        ["1. 在【导入模板】表逐行填写，每行一条底稿；带 * 的列为必填"],
        ["   - 被审计单位：填写单位名称，不存在的单位会自动创建"],
        ["   - 所属版块、缺陷定性：必填，与程序中版块预设对应"],
        ["2. 可选列：问题分类 / 缺陷描述 / 问题金额 / 制度依据 / 审计建议 / 编写人 / 审核人"],
        ["3. 序号、附件数由程序自动生成，无需填写"],
        ["4. 示例行（灰色斜体）可删除，也可覆盖为自己的数据"],
        ["5. 导入前请勿修改表头文字，删除列会导致导入失败"],
        ["6. 保存为 .xlsx 后，在程序中【导入问题汇总】选择该文件"],
    ]
    for row in notes:
        ws2.append(row)
    ws2.column_dimensions["A"].width = 70
    wb.save(path)


def import_from_excel(proj, file_path, operator):
    """解析 xlsx 并导入底稿。返回 {imported, skipped, new_units, errors}。"""
    from limits import MAX_IMPORT_ROWS
    from openpyxl import load_workbook

    # 只读模式按行流式解析，避免 500 MB 工作簿在校验行数前先占满内存。
    wb = load_workbook(file_path, data_only=True, read_only=True)
    rows_to_import = []
    skipped = 0
    errors = []
    try:
        ws = wb["导入模板"] if "导入模板" in wb.sheetnames else wb.active
        required_headers = {"被审计单位", "所属版块", "缺陷定性"}
        header = {}
        header_row = 0
        # 手工模板的表头在第 1 行，程序导出的归档汇总表在标题下方第 2 行；
        # 扫描前 10 行兼容两者，同时避免把项目标题误当成表头。
        for row_number in range(1, 11):
            candidate = {
                str(cell.value).strip().rstrip("*"): cell.column
                for cell in ws[row_number]
                if cell.value
            }
            if required_headers.issubset(candidate):
                header = candidate
                header_row = row_number
                break
        if not header_row:
            raise ValueError("模板缺少必填列：被审计单位、所属版块、缺陷定性（请使用程序提供的模板）")

        def gv(row, name):
            col = header.get(name)
            if col is None:
                return ""
            value = row[col - 1].value
            return "" if value is None else str(value).strip()

        # 先完成资源上限与行级校验，再写项目库，避免第 10001 行才发现超限时
        # 前 10000 行已经部分导入。
        for row in ws.iter_rows(min_row=header_row + 1):
            if row[0].row - header_row > MAX_IMPORT_ROWS:
                raise ValueError(f"导入行数超过上限 {MAX_IMPORT_ROWS} 行，请拆分后再导入")
            unit_name = gv(row, "被审计单位")
            department = gv(row, "所属版块")
            defect_type = gv(row, "缺陷定性")
            if not unit_name and not department and not defect_type:
                continue
            if not unit_name:
                skipped += 1
                errors.append(f"第{row[0].row}行：缺少被审计单位")
                continue
            if not department or not defect_type:
                skipped += 1
                errors.append(f"第{row[0].row}行：所属版块/缺陷定性为必填")
                continue
            rows_to_import.append({
                "unit_name": unit_name,
                "department": department,
                "category": gv(row, "问题分类"),
                "defect_type": defect_type,
                "defect_desc": gv(row, "缺陷描述"),
                "amount": gv(row, "问题金额"),
                "regulation_basis": gv(row, "制度依据"),
                "suggestion": gv(row, "审计建议"),
                "author": gv(row, "编写人") or operator,
                "reviewer": gv(row, "审核人"),
            })
    finally:
        wb.close()

    unit_cache = {u["name"]: u["id"] for u in proj.list_units()}
    imported = new_units = 0
    for item in rows_to_import:
        unit_name = item.pop("unit_name")
        if unit_name not in unit_cache:
            uid = proj.add_unit(unit_name, operator)
            unit_cache[unit_name] = uid
            new_units += 1
        proj.add_issue(unit_cache[unit_name], operator=operator, **item)
        imported += 1
    proj.log(operator, "导入问题汇总",
             f"成功 {imported} 条，跳过 {skipped} 条，新建单位 {new_units} 个")
    # errors 完整返回（T7：前端可导出完整导入报告）；超大导入最多也就上限行数条，可控
    return {"imported": imported, "skipped": skipped,
            "new_units": new_units, "errors": errors}


def merge_import(proj, zip_paths, operator):
    """合并导入：审计经理汇总多个归档 ZIP 到当前项目。

    每个 ZIP：审计问题汇总.xlsx（建单位/底稿）+ 附件-单位/... 目录（还原附件）。
    附件匹配：单位名 + 缺陷定性（同定性多条按单位内创建顺序依次分配）。
    返回 {imported, skipped, files, new_units, errors}。
    """
    import tempfile
    import zipfile as _zipfile

    unit_cache = {u["name"]: u["id"] for u in proj.list_units()}
    imported = skipped = files = new_units = 0
    errors = []

    for zp in zip_paths:
        tmp = Path(tempfile.mkdtemp(prefix="audit_merge_"))
        try:
            with _zipfile.ZipFile(zp) as zf:
                # 防解压炸弹：同时限制成员数与解压总量。
                _validate_archive_size(zf, "归档包")
                # 找汇总表
                xlsx_name = next((n for n in zf.namelist()
                                  if n.endswith("审计问题汇总.xlsx")), None)
                if not xlsx_name:
                    errors.append(f"{Path(zp).name}：未找到 审计问题汇总.xlsx，跳过")
                    skipped += 1
                    shutil.rmtree(tmp, ignore_errors=True)
                    continue
                # 防目录穿越
                target_res = tmp.resolve()
                for name in zf.namelist():
                    member = (target_res / name).resolve()
                    if member != target_res and not member.is_relative_to(target_res):
                        raise ValueError(f"导入包包含非法路径：{name}")
                zf.extractall(tmp)
        except Exception as e:
            errors.append(f"{Path(zp).name}：{e}")
            skipped += 1
            shutil.rmtree(tmp, ignore_errors=True)
            continue

        try:
            # 1) 建单位/底稿（复用 Excel 导入）
            existing_issue_ids = {
                issue["id"]
                for unit in proj.list_units()
                for issue in proj.list_issues(unit["id"])
            }
            r = import_from_excel(proj, tmp / xlsx_name, operator)
            imported += r["imported"]
            skipped += r["skipped"]
            new_units += r["new_units"]
            errors.extend(r["errors"])
            unit_cache = {u["name"]: u["id"] for u in proj.list_units()}

            # 2) 附件还原：附件-单位/.../序号.定性/文件
            # 归档 ZIP 以“项目名称”为根目录，附件目录与汇总表位于同一级。
            root = (tmp / xlsx_name).parent
            for udir in root.glob("附件-*"):
                unit_name = udir.name[len("附件-"):]
                uid = unit_cache.get(unit_name)
                if uid is None:
                    continue
                # 单位内底稿按创建顺序（= 汇总表行序 = 原序号序）
                # 只匹配本次导入生成的底稿，绝不能把归档证据误挂到目标项目原有的
                # 同名单位/同定性底稿。
                issues = [issue for issue in proj.list_issues(uid)
                          if issue["id"] not in existing_issue_ids]
                used = set()
                for idir in sorted(udir.rglob("*"), key=lambda p: str(p)):
                    if not idir.is_dir():
                        continue
                    m = re.match(r"(\d+)\.(.+)", idir.name)
                    if not m or m.group(2) in ("审计问题汇总",):
                        continue
                    defect = m.group(2)
                    # 匹配：同单位同定性、尚未分配附件的底稿
                    cand = [i for i in issues
                            if i["defect_type"] == defect and i["id"] not in used]
                    if not cand:
                        continue
                    iss = cand[0]
                    used.add(iss["id"])
                    for fp in sorted(idir.iterdir()):
                        if fp.name.startswith("."):
                            continue
                        try:
                            if fp.is_dir():
                                members = [
                                    (member.relative_to(fp).as_posix(), str(member))
                                    for member in sorted(fp.rglob("*"))
                                    if member.is_file() and not any(
                                        part.startswith(".")
                                        for part in member.relative_to(fp).parts
                                    )
                                ]
                                rec = proj.add_folder(uid, members, fp.name, operator)
                            elif fp.is_file():
                                rec = proj.add_file(uid, str(fp), operator, orig_name=fp.name)
                            else:
                                continue
                            proj.link_file(iss["id"], rec["id"], operator)
                            files += 1
                        except Exception as e:
                            errors.append(f"{unit_name}/{idir.name}/{fp.name}：{e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    proj.log(operator, "合并导入",
             f"成功 {imported} 条底稿、附件 {files} 个，跳过 {skipped}，新建单位 {new_units} 个")
    return {"imported": imported, "skipped": skipped, "files": files,
            "new_units": new_units, "errors": errors[:30]}


def merge_backups(proj, bak_paths, operator):
    """合并导入：审计经理汇总多个 .auditbak 备份到当前项目。

    每个备份：解压出 audit.db（单位/底稿/文件/关联）+ 附件库/，
    按名称合并单位、复制底稿与附件并重建关联、合并版块预设。
    返回 {units, issues, files, folders, depts, errors}。
    """
    import sqlite3
    import tempfile
    import zipfile as _zipfile

    unit_cache = {u["name"]: u["id"] for u in proj.list_units()}
    # T9：合并前的初始单位集合（检测"同名已存在"必须用它，避免把刚创建的单位误判）
    initial_unit_names = set(unit_cache)
    units = issues = files = folders = depts = versions = 0
    errors = []
    # T9 冲突清单：合并行为与 v1.1 一致（复用/重排/都保留/去重合并），
    # 但把每一处"冲突被正确处理"的情况报告出来，供审计经理核对。
    conflicts = []

    for zp in bak_paths:
        tmp = Path(tempfile.mkdtemp(prefix="audit_merge_"))
        try:
            with _zipfile.ZipFile(zp) as zf:
                bad = zf.testzip()
                if bad:
                    errors.append(f"{Path(zp).name}：备份损坏（{bad}）")
                    continue
                # 防解压炸弹：同时限制成员数与解压总量。
                _validate_archive_size(zf, "备份包")
                # 防目录穿越
                target_res = tmp.resolve()
                for name in zf.namelist():
                    member = (target_res / name).resolve()
                    if member != target_res and not member.is_relative_to(target_res):
                        raise ValueError(f"备份包包含非法路径：{name}")
                zf.extractall(tmp)
            db = tmp / "audit.db"
            if not db.exists():
                errors.append(f"{Path(zp).name}：缺少 audit.db，跳过")
                continue

            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            try:
                # 单位
                b_units = conn.execute("SELECT id, name FROM units ORDER BY sort_order, id").fetchall()
                unit_map = {}
                for bu in b_units:
                    name = bu["name"]
                    if name not in unit_cache:
                        uid = proj.add_unit(name, operator)
                        unit_cache[name] = uid
                        units += 1
                    unit_map[bu["id"]] = unit_cache[name]

                # 底稿
                b_issues = conn.execute(
                    "SELECT * FROM issues ORDER BY unit_id, seq, id").fetchall()
                try:
                    source_versions = conn.execute(
                        "SELECT issue_id, version_no, snapshot, saved_by, created_at "
                        "FROM issue_versions ORDER BY issue_id, version_no"
                    ).fetchall()
                except sqlite3.OperationalError:
                    source_versions = []
                versions_by_issue: dict[int, list[sqlite3.Row]] = {}
                for version in source_versions:
                    versions_by_issue.setdefault(version["issue_id"], []).append(version)
                issue_map = {}
                # T9 冲突检测 1/2：同名单位不同底稿（保留两边）+ 同 seq 底稿（自动重排）
                b_by_unit: dict[int, list] = {}
                for bi in b_issues:
                    b_by_unit.setdefault(bi["unit_id"], []).append(bi)
                for bu in b_units:
                    if bu["name"] not in initial_unit_names:
                        continue  # 全新单位无冲突
                    target_uid = unit_cache[bu["name"]]
                    b_list = b_by_unit.get(bu["id"], [])
                    if b_list:
                        conflicts.append({
                            "type": "unit_exists",
                            "message": f"单位「{bu['name']}」已存在，其 {len(b_list)} 条底稿将并入（保留两边）",
                        })
                    # 同 seq 冲突：备份底稿的 seq 与目标单位已有 seq 重叠 → 自动重排
                    existing_seqs = {iss["seq"] for iss in proj.list_issues(target_uid)}
                    overlap = [bi["seq"] for bi in b_list if bi["seq"] in existing_seqs]
                    if overlap:
                        conflicts.append({
                            "type": "seq_reshuffle",
                            "message": f"单位「{bu['name']}」底稿序号与现有重叠（{sorted(overlap)}），将自动重排",
                        })
                for bi in b_issues:
                    bi = dict(bi)  # 旧备份可没有 v3 的 category 列，统一用 dict.get 兼容
                    if bi["unit_id"] not in unit_map:
                        continue
                    nid = proj.add_issue(
                        unit_map[bi["unit_id"]], operator=operator,
                        department=bi["department"] or "",
                        category=bi.get("category", "") or "",
                        defect_type=bi["defect_type"] or "",
                        defect_desc=bi["defect_desc"] or "", amount=bi["amount"] or "",
                        regulation_basis=bi["regulation_basis"] or "",
                        suggestion=bi["suggestion"] or "",
                        author=bi["author"] or operator, reviewer=bi["reviewer"] or "",
                    )
                    issue_map[bi["id"]] = nid
                    issues += 1
                    source_status = bi.get("status") or AuditProject.STATUS_DRAFT
                    if source_status not in AuditProject.STATUSES:
                        errors.append(
                            f"{Path(zp).name}：底稿 {bi['id']} 的状态“{source_status}”无效，已按草稿导入"
                        )
                        source_status = AuditProject.STATUS_DRAFT
                    imported_versions = versions_by_issue.get(bi["id"], [])
                    current = proj.get_issue(nid)
                    with proj._lock, proj._conn:
                        proj._conn.execute(
                            "UPDATE issues SET status=?, created_at=?, updated_at=? WHERE id=?",
                            (
                                source_status,
                                bi.get("created_at") or current["created_at"],
                                bi.get("updated_at") or current["updated_at"],
                                nid,
                            ),
                        )
                        if imported_versions:
                            # add_issue 自动生成的 v1 仅是导入占位；来源版本存在时用完整
                            # 来源版本链替换，保留保存人、时间和历史快照。
                            proj._conn.execute("DELETE FROM issue_versions WHERE issue_id=?", (nid,))
                            proj._conn.executemany(
                                "INSERT INTO issue_versions(issue_id, version_no, snapshot, saved_by, created_at) "
                                "VALUES(?,?,?,?,?)",
                                [
                                    (nid, version["version_no"], version["snapshot"],
                                     version["saved_by"], version["created_at"])
                                    for version in imported_versions
                                ],
                            )
                            versions += len(imported_versions)

                # 文件（含文件夹实体；关联与未关联证据均需导入）
                b_files = conn.execute("SELECT * FROM files ORDER BY id").fetchall()
                file_map = {}
                # T9 冲突检测 3：同名附件不同内容（都保留）
                # 目标单位已有附件（orig_name → sha256），用于识别同名不同内容
                target_names: dict[tuple[int, str], str] = {}
                for fu in proj.list_units():
                    for f in proj.list_files(fu["id"]):
                        if f.get("mime") != "folder":
                            target_names[(fu["id"], f.get("orig_name", ""))] = f.get("sha256", "")
                for bf in b_files:
                    if bf["unit_id"] not in unit_map:
                        continue
                    bf = dict(bf)  # sqlite3.Row 无 .get()，转 dict（T9 冲突检测需要）
                    src = _extracted_attachment_path(tmp, bf["rel_path"])
                    if bf["mime"] == "folder" and src.is_dir():
                        rec = _import_folder_dir(proj, unit_map[bf["unit_id"]],
                                                 src, bf["orig_name"], operator)
                        if rec:
                            file_map[bf["id"]] = rec["id"]
                            folders += 1
                    elif src.is_file():
                        # 同名但内容（sha256）不同 → 冲突：两个都保留（stored_name 用 uuid 不冲突）
                        key = (unit_map[bf["unit_id"]], bf.get("orig_name", ""))
                        if key in target_names and target_names[key] != bf.get("sha256", ""):
                            conflicts.append({
                                "type": "file_same_name",
                                "message": f"附件「{bf['orig_name']}」与现有附件同名但内容不同，将同时保留",
                            })
                        rec = proj.add_file(unit_map[bf["unit_id"]], str(src), operator,
                                            orig_name=bf["orig_name"])
                        file_map[bf["id"]] = rec["id"]
                        files += 1

                # 关联
                for link in conn.execute("SELECT issue_id, file_id FROM issue_files").fetchall():
                    if link["issue_id"] in issue_map and link["file_id"] in file_map:
                        try:
                            proj.link_file(issue_map[link["issue_id"]],
                                           file_map[link["file_id"]], operator)
                        except (KeyError, ValueError) as exc:
                            errors.append(
                                f"{Path(zp).name}：附件关联 {link['issue_id']}/{link['file_id']} 导入失败：{exc}"
                            )
                # 独占证据语义随备份迁移：目标附件只能保留来源指定底稿这一条关联。
                for bf in b_files:
                    bf = dict(bf)
                    exclusive_to = bf.get("exclusive_to")
                    if not exclusive_to:
                        continue
                    if bf["id"] not in file_map or exclusive_to not in issue_map:
                        errors.append(
                            f"{Path(zp).name}：附件 {bf['id']} 的独占底稿不存在，已按普通附件导入"
                        )
                        continue
                    try:
                        proj.link_file_exclusive(
                            issue_map[exclusive_to], file_map[bf["id"]], operator
                        )
                    except (KeyError, ValueError) as exc:
                        errors.append(f"{Path(zp).name}：附件 {bf['id']} 独占关系导入失败：{exc}")

                # 版块预设合并
                try:
                    meta = conn.execute("SELECT value FROM meta WHERE key='departments'").fetchone()
                    if meta and meta["value"]:
                        cur_depts = json.loads(proj.get_meta("departments", "[]"))
                        new_depts = []
                        for d in json.loads(meta["value"]):
                            if d and d not in cur_depts:
                                cur_depts.append(d)
                                new_depts.append(d)
                                depts += 1
                        if new_depts:
                            # T9 冲突检测 4：版块预设差异（去重合并）
                            conflicts.append({
                                "type": "dept_merge",
                                "message": f"新增版块预设 {len(new_depts)} 个：{'、'.join(new_depts)}",
                            })
                        proj.set_meta("departments", json.dumps(cur_depts, ensure_ascii=False))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    errors.append(f"{Path(zp).name}：版块预设导入失败：{exc}")
                # 问题分类预设合并（v3 新增；旧备份无该键时自然跳过）。
                try:
                    meta = conn.execute("SELECT value FROM meta WHERE key='categories'").fetchone()
                    if meta and meta["value"]:
                        current_categories = json.loads(proj.get_meta("categories", "[]"))
                        new_categories = []
                        for category in json.loads(meta["value"]):
                            if category and category not in current_categories:
                                current_categories.append(category)
                                new_categories.append(category)
                        if new_categories:
                            conflicts.append({
                                "type": "category_merge",
                                "message": f"新增问题分类预设 {len(new_categories)} 个：{'、'.join(new_categories)}",
                            })
                        proj.set_meta("categories", json.dumps(current_categories, ensure_ascii=False))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    errors.append(f"{Path(zp).name}：问题分类预设导入失败：{exc}")
            finally:
                conn.close()
        except Exception as e:
            errors.append(f"{Path(zp).name}：{e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    proj.log(operator, "合并导入备份",
             f"单位 {units} 个、底稿 {issues} 条、版本 {versions} 个、附件 {files} 个、文件夹 {folders} 个、版块预设 {depts} 个"
             + (f"、冲突 {len(conflicts)} 处" if conflicts else ""))
    return {"units": units, "issues": issues, "files": files,
            "folders": folders, "depts": depts, "versions": versions, "errors": errors[:30],
            "conflicts": conflicts}


def _import_folder_dir(proj, unit_id, src_dir, folder_name, operator):
    """把备份中的文件夹实体（目录）复制进当前项目附件库并登记记录。"""
    import uuid as _uuid

    from database import ATTACH_DIR
    unit = proj.get_unit(unit_id)
    if not unit:
        return None
    dirname = f"{_safe(folder_name)}_{_uuid.uuid4().hex[:8]}"
    dest = proj.root / ATTACH_DIR / proj.unit_dir_name(unit_id) / dirname
    try:
        shutil.copytree(src_dir, dest)
        total = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
        rel = f"{ATTACH_DIR}/{proj.unit_dir_name(unit_id)}/{dirname}"
        conn = proj._conn
        with proj._lock, conn:
            cur = conn.execute(
                "INSERT INTO files(unit_id, stored_name, orig_name, rel_path, size, sha256, mime, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (unit_id, dirname, folder_name, rel, total, "", "folder",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            fid = cur.lastrowid
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    proj.log(operator, "导入文件夹", f"{unit['name']} · {folder_name}", "合并导入")
    return proj.get_file(fid)
