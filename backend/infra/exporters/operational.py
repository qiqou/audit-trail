"""不含业务正文的运维输出格式写入器。"""

import csv
import json
from pathlib import Path

AUDIT_LOG_FIELDS = (
    "id", "created_at", "operator", "action", "target", "detail", "event_uuid", "issue_uuid", "file_uuid",
)


def write_audit_log_csv(path: Path, rows: list[dict]) -> int:
    """按稳定字段写入已筛选日志，不读写附件或底稿正文。"""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_LOG_FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in AUDIT_LOG_FIELDS} for row in rows)
    return len(rows)


def write_diagnostics_support_package(path: Path, summary: dict) -> None:
    """写入已由服务端脱敏的诊断摘要，不在导出层补充任何项目数据。"""
    with path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
