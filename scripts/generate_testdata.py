"""生成可重复、完全脱敏的审迹项目样本。

不启动服务、不访问页面。样本仅用于开发、性能和导入导出验证，不能替代真实项目验收。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from database import SCHEMA_VERSION, AuditProject

PRESETS = {
    "small": {"units": 3, "issues": 12, "files": 18, "exchange_sessions": 2},
    "medium": {"units": 20, "issues": 200, "files": 400, "exchange_sessions": 10},
    "large": {"units": 100, "issues": 5_000, "files": 20_000, "exchange_sessions": 100},
}


def _issue_fields(index: int) -> dict[str, str]:
    return {
        "department": f"测试版块{index % 8 + 1:02d}",
        "category": f"测试分类{index % 6 + 1:02d}",
        "defect_type": f"测试定性{index % 10 + 1:02d}",
        "defect_desc": f"第 {index:05d} 条脱敏测试底稿，用于验证排序、版本和导出。",
        "amount": str((index % 97 + 1) * 1000),
        "regulation_basis": "脱敏测试制度依据",
        "suggestion": "脱敏测试整改建议",
        "author": "测试编制人",
        "reviewer": "测试复核人",
    }


def generate(size: str, output: Path) -> dict:
    """在一个不存在的目录下生成样本并返回实际计数。"""
    if size not in PRESETS:
        raise ValueError(f"不支持的样本规模：{size}")
    output = output.expanduser().resolve(strict=False)
    if output.exists():
        raise FileExistsError(f"目标目录已存在，拒绝覆盖：{output}")
    config = PRESETS[size]
    source_file = output / ".audit-trail-sample-source.txt"
    project: AuditProject | None = None
    try:
        output.mkdir(parents=True)
        source_file.write_text("审迹脱敏样本附件\n", encoding="utf-8")
        project = AuditProject(output)
        project.set_meta("project_name", f"脱敏{size}样本")
        units = [project.add_unit(f"脱敏被审计单位{index:03d}", "样本生成器")
                 for index in range(1, config["units"] + 1)]
        issues = [
            project.add_issue(units[index % len(units)], "样本生成器", **_issue_fields(index + 1))
            for index in range(config["issues"])
        ]
        for index in range(config["files"]):
            unit_id = units[index % len(units)]
            record = project.add_file(
                unit_id,
                source_file,
                "样本生成器",
                orig_name=f"脱敏附件_{index + 1:05d}.txt",
                folder_path=f"证据包{index % 5 + 1:02d}",
            )
            matching_issue = issues[index % len(issues)]
            project.link_file(matching_issue, int(record["id"]), "样本生成器")
        for issue_id in issues[:config["exchange_sessions"]]:
            session = project.start_exchange_session(issue_id, "样本生成器")
            project.add_exchange_comment(
                str(session["session_uuid"]),
                "脱敏交流批注，用于验证历史记录数量。",
                "defect_desc",
                "",
                "样本生成器",
            )
        tables = ("units", "issues", "files", "issue_versions", "issue_files", "exchange_sessions", "exchange_comments", "audit_log")
        counts = {table: int(project._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}
        manifest = {
            "format": "audit-trail-sample-manifest/v1",
            "sample_size": size,
            "schema_version": SCHEMA_VERSION,
            "sensitive_data": False,
            "counts": counts,
        }
        (output / "sample_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    finally:
        if project is not None:
            project.close()
        source_file.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成审迹脱敏项目样本")
    parser.add_argument("--size", choices=sorted(PRESETS), required=True, help="样本规模")
    parser.add_argument("--output", type=Path, required=True, help="必须不存在的输出目录")
    args = parser.parse_args(argv)
    try:
        manifest = generate(args.size, args.output)
    except (FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
