"""规格已识别时不再重复追问。"""
from app.agent.subagents.product_kb.application.nodes.node_item_name_confirm import (
    step_6_check_confirmation,
)


def _base():
    return {
        "confirmed_item_names": ["Brother HAK 180 烫金机"],
        "manual_id": "m1",
        "target_model": "",
        "candidates": [],
        "near": [],
    }


def test_missing_variant_asks():
    result = step_6_check_confirmation(
        {**_base(), "needs_variant": True, "variant_options": ["全幅", "半幅"]},
        "s1", "全幅烫金膜盒怎么安装", "全幅烫金膜盒怎么安装")
    assert result.get("need_confirm") is True
    assert (result.get("clarify") or {}).get("question")


def test_known_variant_goes_to_retrieval():
    result = step_6_check_confirmation(
        {**_base(), "needs_variant": False, "variant_options": ["全幅", "半幅"],
         "variant": "全幅"},
        "s1", "全幅烫金膜盒怎么安装", "全幅烫金膜盒怎么安装")
    assert not result.get("need_confirm")
    assert result.get("item_names") == ["Brother HAK 180 烫金机"]
