"""不依赖数据库的底稿状态机测试向量。"""

import pytest
from domain.issue_workflow import (
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_REJECTED,
    STATUS_REVIEWED,
    STATUS_SUBMITTED,
    validate_status_transition,
)


def _complete_issue(**changes):
    issue = {"defect_desc": "已发现问题", "department": "销售", "defect_type": "收入"}
    issue.update(changes)
    return issue


def test_issue_workflow_rejects_illegal_transition_with_actionable_hint():
    with pytest.raises(ValueError, match="归档后编辑"):
        validate_status_transition(STATUS_ARCHIVED, STATUS_DRAFT, _complete_issue())


def test_issue_workflow_requires_review_information_and_comment():
    with pytest.raises(ValueError, match="审核人"):
        validate_status_transition(STATUS_SUBMITTED, STATUS_REVIEWED, _complete_issue())
    with pytest.raises(ValueError, match="退回意见"):
        validate_status_transition(STATUS_SUBMITTED, STATUS_REJECTED, _complete_issue(reviewer="李四"))


def test_issue_workflow_returns_persistable_transition_detail():
    result = validate_status_transition(
        STATUS_REVIEWED, STATUS_ARCHIVED, _complete_issue(reviewer="李四"), "归档依据已补全",
    )
    assert result.old == STATUS_REVIEWED
    assert result.new == STATUS_ARCHIVED
    assert result.detail == "已复核 → 已归档（修改原因：归档依据已补全）"
