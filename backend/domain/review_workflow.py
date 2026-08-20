"""内部复核意见的不可变事件规则。"""

from collections.abc import Iterable

from domain.errors import ConflictError

EVENT_CREATED = "created"
EVENT_REPLIED = "replied"
EVENT_RESOLVED = "resolved"
EVENT_REOPENED = "reopened"
EVENT_TYPES = (EVENT_CREATED, EVENT_REPLIED, EVENT_RESOLVED, EVENT_REOPENED)


def note_state(events: Iterable[str]) -> str:
    """由事件序列推导当前状态，不维护可被覆盖的状态列。"""
    state = "open"
    for event in events:
        if event == EVENT_RESOLVED:
            state = "resolved"
        elif event == EVENT_REOPENED:
            state = "open"
    return state


def validate_review_event(current_state: str, event_type: str, body: str = "") -> None:
    """阻断无意义或非法的意见事件，避免前端按显示文字猜状态。"""
    event = str(event_type or "").strip()
    message = str(body or "").strip()
    if event not in EVENT_TYPES:
        raise ValueError(f"未知复核事件：{event_type}")
    if event == EVENT_CREATED and not message:
        raise ValueError("复核意见不能为空")
    if event == EVENT_REPLIED and not message:
        raise ValueError("回复内容不能为空")
    if event == EVENT_RESOLVED and current_state != "open":
        raise ConflictError("复核意见已清除，请先重新打开后再处理")
    if event == EVENT_REOPENED and current_state != "resolved":
        raise ConflictError("仅已清除的复核意见可以重新打开")
