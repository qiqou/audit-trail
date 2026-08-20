"""单位仓储仅承接读取，不能绕过项目事务或删除过滤。"""

from repositories.units import UnitRepository


def test_unit_repository_reads_active_units_and_hides_deleted(proj):
    first = proj.add_unit("甲单位", "张三")
    second = proj.add_unit("乙单位", "张三")
    with proj._lock, proj._conn:
        proj._conn.execute("UPDATE units SET deleted_at='2026-08-21 00:00:00' WHERE id=?", (second,))
    repository = UnitRepository(proj._conn)
    assert [unit["id"] for unit in repository.list_active()] == [first]
    assert repository.get(second) is None
    assert repository.get(second, include_deleted=True)["name"] == "乙单位"
    assert repository.get_active_by_name("甲单位")["id"] == first
