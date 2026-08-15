"""P0 自动备份内核：策略校验、内容寻址去重和恢复点清理。"""

from database import AuditProject
from export import (
    AUTO_BACKUP_STORE_NAME,
    create_incremental_recovery_point,
    restore_incremental_recovery_point,
)


def test_auto_backup_settings_default_off_and_requires_external_target(proj):
    settings = proj.get_backup_settings()
    assert settings["enabled"] is False
    assert settings["interval_minutes"] == 360
    assert settings["retention_days"] == 7

    try:
        proj.save_backup_settings(
            "张三", enabled=True, target_dir=str(proj.root), interval_minutes=360,
            retention_days=7, max_bytes=1024,
        )
    except ValueError as error:
        assert "不能位于当前项目内" in str(error)
    else:
        raise AssertionError("项目内目录不得作为自动备份目标")


def test_incremental_recovery_point_reuses_unchanged_attachment_object(proj, tmp_path):
    unit_id = proj.add_unit("甲单位", "张三")
    source = tmp_path / "证据.txt"
    source.write_text("同一份证据", encoding="utf-8")
    evidence = proj.add_file(unit_id, source, "张三")
    target = tmp_path / "自动备份目标"
    target.mkdir()

    first = create_incremental_recovery_point(proj, target_dir=target, max_bytes=10 * 1024 * 1024)
    second = create_incremental_recovery_point(proj, target_dir=target, max_bytes=10 * 1024 * 1024)

    assert first["copied_objects"] == 1
    assert second["copied_objects"] == 0
    assert second["reused_objects"] == 1
    store = target / AUTO_BACKUP_STORE_NAME / proj.project_uuid
    assert len(list((store / "objects").iterdir())) == 1
    assert len(list((store / "recovery-points").iterdir())) == 2
    assert evidence["sha256"] == next((store / "objects").iterdir()).name


def test_incremental_recovery_point_restores_independent_project(proj, tmp_path):
    unit_id = proj.add_unit("甲单位", "张三")
    source = tmp_path / "取证资料.txt"
    source.write_text("恢复校验内容", encoding="utf-8")
    evidence = proj.add_file(unit_id, source, "张三")
    target = tmp_path / "自动备份目标"
    target.mkdir()
    point = create_incremental_recovery_point(proj, target_dir=target, max_bytes=10 * 1024 * 1024)

    restored = restore_incremental_recovery_point(
        project_uuid=proj.project_uuid,
        backup_target_dir=target,
        recovery_point_id=point["recovery_point"],
        target_dir=tmp_path / "恢复项目",
    )

    restored_project = AuditProject(restored["path"])
    try:
        assert restored_project.list_units()[0]["name"] == "甲单位"
        assert restored_project.attachment_path(evidence["rel_path"]).read_text(encoding="utf-8") == "恢复校验内容"
    finally:
        restored_project.close()
