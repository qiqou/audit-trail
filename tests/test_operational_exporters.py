"""运维导出格式必须稳定，且导出器只写入调用方提供的数据。"""

import json

from infra.exporters.operational import AUDIT_LOG_FIELDS, write_audit_log_csv, write_diagnostics_support_package


def test_operational_exporters_write_stable_log_fields_and_explicit_diagnostics(tmp_path):
    log_path = tmp_path / "操作日志.csv"
    assert write_audit_log_csv(log_path, [{"id": 1, "operator": "张三", "action": "测试", "extra": "不得输出"}]) == 1
    assert log_path.read_text(encoding="utf-8-sig").splitlines() == [
        ",".join(AUDIT_LOG_FIELDS), "1,,张三,测试,,,,,",
    ]

    diagnostic_path = tmp_path / "诊断.json"
    write_diagnostics_support_package(diagnostic_path, {"schema_version": 18, "privacy": {"excluded": ["issue_content"]}})
    assert json.loads(diagnostic_path.read_text(encoding="utf-8"))["privacy"]["excluded"] == ["issue_content"]
