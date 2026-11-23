"""终稿接地：上一轮用户说过的事实要参与核对，不能被当成无依据删掉。"""
from langchain.messages import AIMessage, HumanMessage

from app.agent.runtime.grounding import deterministic_issues
from app.agent.runtime.main_agent import _grounding_user_text


def test_grounding_user_text_keeps_history():
    messages = [
        HumanMessage(content="订单号 202611070001 到哪了"),
        AIMessage(content="正在运输中"),
        HumanMessage(content="那它什么时候到"),
    ]

    text = _grounding_user_text(messages)

    assert "202611070001" in text
    assert "那它什么时候到" in text


def test_history_fact_is_supported():
    history = "订单号 202611070001 到哪了\n那它什么时候到"
    reply = "您的订单 202611070001 预计明天送达。"

    assert deterministic_issues(reply, "（本轮没有工具返回）", history) == []
