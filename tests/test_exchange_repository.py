"""交流仓储仅服务读取，恢复同一底稿时必须命中原未结束会话。"""

from repositories.exchanges import ExchangeRepository


def test_exchange_repository_finds_open_session_by_stable_issue_uuid(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", defect_type="交流测试")
    session = proj.start_exchange_session(issue_id, "李四")
    repository = ExchangeRepository(proj._conn)

    assert repository.get_session(session["session_uuid"])["issue_id"] == issue_id
    assert repository.find_open_session_for_issue_uuid(session["issue_uuid"]) == session["session_uuid"]
    proj.close_exchange_session(session["session_uuid"], "结束", "李四")
    assert repository.find_open_session_for_issue_uuid(session["issue_uuid"]) is None
