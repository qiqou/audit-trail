"""P0 编号复用与结构化金额的后端回归。"""

import pytest


def test_number_reuses_gap_without_changing_existing_display_number(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    first = proj.add_issue(unit_id, "张三")
    second = proj.add_issue(unit_id, "张三")

    proj.delete_issue(first, "张三")
    third = proj.add_issue(unit_id, "张三")

    assert proj.get_issue(second)["seq"] == 2
    assert proj.get_issue(third)["seq"] == 1
    proj.set_meta("issue_number_prefix", "底稿-")
    assert proj.issue_no(proj.get_issue(second)["seq"]) == "底稿-2"


def test_structured_amount_has_two_decimal_places_and_preserves_currency_unit(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(
        unit_id, "张三", amount="-120.50", currency="usd", amount_unit="万元",
    )
    issue = proj.get_issue(issue_id)

    assert issue["amount"] == "-120.50"
    assert issue["amount_minor"] == -12050
    assert issue["currency"] == "USD"
    assert issue["amount_unit"] == "万元"

    assert proj.update_issue(
        issue_id, "张三", amount="0.00", currency="CNY", amount_unit="元",
    ) is True
    assert proj.get_issue(issue_id)["amount_minor"] == 0


@pytest.mark.parametrize("amount", ["120.123", "120万", "NaN", "Infinity"])
def test_structured_amount_rejects_non_numeric_or_more_than_two_decimals(proj, amount):
    unit_id = proj.add_unit("甲单位", "张三")
    with pytest.raises(ValueError, match="问题金额"):
        proj.add_issue(unit_id, "张三", amount=amount, currency="CNY", amount_unit="元")


def test_legacy_amount_text_is_readable_and_editable_without_forced_conversion(proj):
    """v1.1 的自由金额文本在用户主动结构化前不得被迁移逻辑擅自改写。"""
    unit_id = proj.add_unit("甲单位", "张三")
    issue_id = proj.add_issue(unit_id, "张三", amount="120万")

    assert proj.get_issue(issue_id)["amount"] == "120万"
    assert proj.update_issue(issue_id, "张三", defect_desc="补充说明") is True
    assert proj.get_issue(issue_id)["amount"] == "120万"


def test_project_amount_defaults_apply_only_to_new_structured_input(proj):
    unit_id = proj.add_unit("甲单位", "张三")
    settings = proj.save_amount_settings("张三", currency="HKD", amount_unit="万元")
    assert settings["currency"] == "HKD"
    issue_id = proj.add_issue(unit_id, "张三", amount="12", currency="", amount_unit="")
    issue = proj.get_issue(issue_id)
    assert (issue["currency"], issue["amount_unit"], issue["amount_minor"]) == ("HKD", "万元", 1200)
