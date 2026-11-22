"""提示词不得把示例问题当成用户原话，也不得替用户缩窄问题。"""
from app.core.load_prompt import load_prompt


def test_main_prompt_requires_user_original_question():
    text = load_prompt("main_agent")
    assert "逐字包含用户本轮原话" in text
    assert "不要替用户改写或缩窄问题" in text
    assert "「退款多久到账」" not in text


def test_policy_prompt_forbids_narrowing_and_question_quote():
    text = load_prompt("policy_agent_system")
    assert "不要自行缩窄" in text
    assert "回答里不要写「您问的" in text
