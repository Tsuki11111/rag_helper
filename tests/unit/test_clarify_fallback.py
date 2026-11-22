"""规格追问兜底：优先透传 clarify.question。"""
from app.agent.subagents.product_kb.domain.outcome import clarify_fallback_text


def test_variant_question_is_passed_through():
    question = "您用的是全幅还是半幅？"
    assert clarify_fallback_text({"question": question, "options": []}) == question


def test_candidate_fallback_lists_names():
    text = clarify_fallback_text({"options": [{"item_name": "A"}, {"item_name": "B"}]})
    assert "A / B" in text
