"""附件仓储只承担读取，且必须延续共享与软删除边界。"""

from repositories.evidence import EvidenceRepository


def test_evidence_repository_reads_active_and_shareable_files(proj, tmp_path):
    unit_id = proj.add_unit("甲单位", "张三")
    source = tmp_path / "证据.txt"
    source.write_text("audit evidence", encoding="utf-8")
    first = proj.add_file(unit_id, source, "张三")
    second = proj.add_file(unit_id, source, "张三", orig_name="独占证据.txt")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="附件范围测试")
    with proj._lock, proj._conn:
        proj._conn.execute("UPDATE files SET exclusive_to=? WHERE id=?", (issue_id, second["id"]))

    repository = EvidenceRepository(proj._conn)
    assert [row["id"] for row in repository.list_active_for_unit(unit_id)] == [second["id"], first["id"]]
    assert [row["id"] for row in repository.list_shareable_for_unit(unit_id)] == [first["id"]]
    assert repository.get(first["id"])["orig_name"] == "证据.txt"
    assert repository.find_file_by_sha(first["sha256"])["id"] == first["id"]
