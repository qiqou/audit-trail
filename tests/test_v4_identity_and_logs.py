"""v4 项目身份、显示编号与操作日志链回归用例。"""

import hashlib
import uuid

import pytest


def _assert_uuid(value: str) -> None:
    assert str(uuid.UUID(value)) == value


def test_v4_entities_get_stable_uuid_and_issue_code(proj, tmp_path):
    """内部标识永不复用；显示编号保留创建时的可读快照。"""
    unit_id = proj.add_unit("甲单位", "测试账户")
    issue_id = proj.add_issue(unit_id, "测试账户", defect_type="采购问题")
    source = tmp_path / "证据.txt"
    source.write_text("evidence", encoding="utf-8")
    evidence = proj.add_file(unit_id, source, "测试账户")

    _assert_uuid(proj.project_uuid)
    _assert_uuid(proj.get_unit(unit_id)["unit_uuid"])
    issue = proj.get_issue(issue_id)
    _assert_uuid(issue["issue_uuid"])
    assert issue["issue_code"] == "1"
    _assert_uuid(evidence["file_uuid"])

    proj.set_meta("issue_number_prefix", "WP-")
    proj.set_meta("issue_number_suffix", "-A")
    assert proj.get_issue(issue_id)["issue_code"] == "1"
    second_id = proj.add_issue(unit_id, "测试账户", defect_type="销售问题")
    assert proj.get_issue(second_id)["issue_code"] == "WP-2-A"


def test_v4_backfill_is_idempotent_and_log_chain_is_verifiable(proj):
    """迁移只补空值；重新打开项目不会重写已完成的日志链。"""
    unit_id = proj.add_unit("甲单位", "测试账户")
    issue_id = proj.add_issue(unit_id, "测试账户", defect_type="采购问题")
    proj.update_issue(issue_id, "测试账户", defect_desc="已补充描述")
    before = [dict(row) for row in proj._conn.execute("SELECT * FROM audit_log ORDER BY id")]
    project_uuid = proj.project_uuid

    proj._backfill_v4_identity_fields()
    after = [dict(row) for row in proj._conn.execute("SELECT * FROM audit_log ORDER BY id")]
    assert after == before

    previous = ""
    for event in after:
        assert event["project_uuid"] == project_uuid
        assert event["prev_hash"] == previous
        payload = proj._audit_event_payload(
            event_uuid=event["event_uuid"], project_uuid=event["project_uuid"],
            issue_uuid=event["issue_uuid"], file_uuid=event["file_uuid"],
            actor_account=event["actor_account"], actor_uid=event["actor_uid"],
            device_id=event["device_id"], action=event["action"], target=event["target"],
            detail=event["detail"], created_at=event["created_at"], prev_hash=event["prev_hash"],
        )
        assert event["event_hash"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()
        previous = event["event_hash"]


def test_new_issue_rolls_back_when_audit_log_write_fails(proj, monkeypatch):
    """业务数据和对应日志必须同一事务提交，避免无日志的孤儿底稿。"""
    unit_id = proj.add_unit("甲单位", "测试账户")

    def fail_log(*args, **kwargs):
        raise RuntimeError("模拟日志磁盘写入失败")

    monkeypatch.setattr(proj, "_log_in_transaction", fail_log)
    with pytest.raises(RuntimeError, match="日志磁盘"):
        proj.add_issue(unit_id, "测试账户", defect_type="不应落库")

    assert proj.list_issues(unit_id) == []
    assert not proj.list_versions(1)
