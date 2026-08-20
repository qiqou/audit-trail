import json

from database import SCHEMA_VERSION
from scripts.export_contract_snapshot import export_snapshot


def test_contract_snapshot_records_schema_export_and_audit_events(tmp_path):
    output = tmp_path / "contract.json"

    snapshot = export_snapshot(output)

    assert json.loads(output.read_text(encoding="utf-8")) == snapshot
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert "issues" in snapshot["tables"]
    assert snapshot["export"]["summary_headers"][0] == {"field": "seq", "label": "序号"}
    assert "新建单位" in snapshot["audit_log"]["observed_actions"]
    assert "附件库" in snapshot["project_tree"]
