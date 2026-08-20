"""导出 v1.3 的数据库、导出字段和审计事件契约快照。

不启动 HTTP 服务、不驱动页面；使用临时空项目读取 SQLite 结构，并以脱敏最小操作
采集可实际产生的审计事件名称，供后续契约测试对比。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from database import AuditProject, SCHEMA_VERSION  # noqa: E402
from export import IMPORT_HEADERS, SUMMARY_HEADERS  # noqa: E402


def _table_contract(project: AuditProject) -> dict[str, dict]:
    rows = project._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    output: dict[str, dict] = {}
    for row in rows:
        table = str(row[0])
        columns = [
            {
                "name": str(item[1]),
                "type": str(item[2]),
                "not_null": bool(item[3]),
                "default": item[4],
                "primary_key_order": int(item[5]),
            }
            for item in project._conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        foreign_keys = [
            {
                "from": str(item[3]),
                "table": str(item[2]),
                "to": str(item[4]),
                "on_update": str(item[5]),
                "on_delete": str(item[6]),
            }
            for item in project._conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        ]
        output[table] = {"columns": columns, "foreign_keys": foreign_keys}
    return output


def export_snapshot(output_path: Path) -> dict:
    output_path = output_path.expanduser().resolve(strict=False)
    with tempfile.TemporaryDirectory(prefix="audit-trail-contract-") as temp_dir:
        root = Path(temp_dir) / "contract-project"
        source_file = Path(temp_dir) / "脱敏附件.txt"
        source_file.write_text("脱敏契约附件\n", encoding="utf-8")
        project = AuditProject(root)
        try:
            unit_id = project.add_unit("脱敏契约单位", "契约快照")
            issue_id = project.add_issue(
                unit_id,
                "契约快照",
                department="测试版块",
                defect_type="测试定性",
                defect_desc="脱敏契约底稿",
            )
            file_record = project.add_file(unit_id, source_file, "契约快照", orig_name="脱敏附件.txt")
            project.link_file(issue_id, int(file_record["id"]), "契约快照")
            session = project.start_exchange_session(issue_id, "契约快照")
            project.add_exchange_comment(
                str(session["session_uuid"]), "脱敏契约批注", "defect_desc", "", "契约快照"
            )
            audit_events = [
                str(row[0])
                for row in project._conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()
            ]
            snapshot = {
                "format": "audit-trail-contract-snapshot/v1",
                "schema_version": SCHEMA_VERSION,
                "tables": _table_contract(project),
                "indexes": [
                    {"name": str(row[0]), "table": str(row[1]), "sql": str(row[2] or "")}
                    for row in project._conn.execute(
                        "SELECT name, tbl_name, sql FROM sqlite_master "
                        "WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                ],
                "export": {
                    "summary_headers": [{"field": field, "label": label} for field, label, _width in SUMMARY_HEADERS],
                    "import_headers": IMPORT_HEADERS,
                },
                "audit_log": {
                    "columns": [item["name"] for item in _table_contract(project)["audit_log"]["columns"]],
                    "observed_actions": audit_events,
                },
                "project_tree": sorted(path.relative_to(root).as_posix() for path in root.iterdir()),
            }
        finally:
            project.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return snapshot


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法: python scripts/export_contract_snapshot.py <输出路径>", file=sys.stderr)
        return 2
    export_snapshot(Path(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
