"""底稿状态流转的纯业务规则。

数据库层负责原子写入、版本和审计日志；本模块只决定一个流转是否允许以及
现场人员应看到的明确修复提示，因此可单独测试且不会让前端猜测状态语义。
"""

from collections.abc import Mapping
from dataclasses import dataclass

STATUS_DRAFT = "草稿"
STATUS_SUBMITTED = "编制完成"
STATUS_REJECTED = "复核退回"
STATUS_REVIEWED = "已复核"
STATUS_ARCHIVED = "已归档"

ISSUE_STATUSES = (STATUS_DRAFT, STATUS_SUBMITTED, STATUS_REJECTED, STATUS_REVIEWED, STATUS_ARCHIVED)
STATUS_FLOW: dict[str, frozenset[str]] = {
    STATUS_DRAFT: frozenset({STATUS_SUBMITTED}),
    STATUS_SUBMITTED: frozenset({STATUS_REJECTED, STATUS_REVIEWED}),
    STATUS_REJECTED: frozenset({STATUS_SUBMITTED}),
    STATUS_REVIEWED: frozenset({STATUS_REJECTED, STATUS_ARCHIVED}),
    STATUS_ARCHIVED: frozenset({STATUS_SUBMITTED}),
}
STATUS_HINTS = {
    (STATUS_ARCHIVED, STATUS_DRAFT): "已归档底稿如需修改，请使用『归档后编辑』（自动开新版本）",
    (STATUS_ARCHIVED, STATUS_REJECTED): "已归档底稿不能退回，请使用『归档后编辑』后重新复核",
    (STATUS_ARCHIVED, STATUS_REVIEWED): "已归档底稿已复核过，如需改动请使用『归档后编辑』",
}


@dataclass(frozen=True)
class StatusTransition:
    """通过校验的状态变更，用于数据层一次性保存版本、状态和日志。"""

    old: str
    new: str
    detail: str


def validate_status_transition(
    old_status: str,
    new_status: str,
    issue: Mapping[str, object],
    comment: str = "",
) -> StatusTransition:
    """校验底稿状态流转及提交/复核所需信息，失败时返回可行动提示。"""
    old = str(old_status or STATUS_DRAFT)
    new = str(new_status or "").strip()
    if new not in ISSUE_STATUSES:
        raise ValueError(f"未知状态：{new_status}。可选：{'、'.join(ISSUE_STATUSES)}")
    allowed = STATUS_FLOW.get(old, frozenset())
    if new not in allowed:
        hint = STATUS_HINTS.get((old, new))
        if not hint:
            hint = f"不能从「{old}」变更为「{new}」。" + (
                f"可以流转到：{'、'.join(sorted(allowed))}。" if allowed else "该状态不可再流转。"
            )
        raise ValueError(hint)

    if new == STATUS_SUBMITTED and old in (STATUS_DRAFT, STATUS_REJECTED):
        missing = [label for key, label in (
            ("defect_desc", "发现描述"), ("department", "版块"), ("defect_type", "定性"),
        ) if not str(issue.get(key) or "").strip()]
        if missing:
            raise ValueError(f"提交复核前请先填写：{'、'.join(missing)}")
    if new == STATUS_REVIEWED and not str(issue.get("reviewer") or "").strip():
        raise ValueError("复核通过前请填写审核人（reviewer）")
    if new == STATUS_REJECTED and not str(comment or "").strip():
        raise ValueError("复核退回请填写退回意见")
    if old == STATUS_ARCHIVED and not str(comment or "").strip():
        raise ValueError("归档后编辑请填写修改原因")

    detail = f"{old} → {new}"
    if str(comment or "").strip():
        label = "退回意见" if new == STATUS_REJECTED else "修改原因"
        detail += f"（{label}：{str(comment).strip()}）"
    return StatusTransition(old=old, new=new, detail=detail)
