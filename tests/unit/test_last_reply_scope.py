"""终稿只取本轮新增消息，历史回答不能冒充本轮回复。"""
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.runtime.main_agent import _last_reply


def test_history_reply_is_not_reused():
    history = [HumanMessage(content="上一轮问题"), AIMessage(content="上一轮回答")]
    assert _last_reply(history, since=len(history)) == ""


def test_current_turn_reply_is_used():
    history = [HumanMessage(content="上一轮问题"), AIMessage(content="上一轮回答")]
    current = [HumanMessage(content="本轮问题"), AIMessage(content="本轮回答")]
    assert _last_reply(history + current, since=len(history)) == "本轮回答"


def test_current_turn_without_reply_does_not_fall_back():
    history = [HumanMessage(content="上一轮问题"), AIMessage(content="上一轮回答")]
    current = [HumanMessage(content="只有问题")]
    assert _last_reply(history + current, since=len(history)) == ""
