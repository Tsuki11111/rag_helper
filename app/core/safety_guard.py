"""入口安全策略：命中明确敏感主题时直接拒答，不进入检索图。

这里只放**明确的政策/违法主题**，不做泛化的“政治敏感”判断：
- 宁可漏掉模糊表达，也不把普通产品问题误判成敏感主题；
- 命中后由主 agent 入口短路，返回固定拒答并记录 ``rejected``，
  不派发 RAG 子图、不调用模型；
- ``rejected`` 与输入注入的 ``blocked`` 语义不同，不能混用。
"""

from __future__ import annotations

import re


SENSITIVE_REFUSAL_ANSWER = (
    "这个问题我没办法提供信息。我是「买小购」客服，只回答产品使用和订单相关的问题。"
)


# 只列明确主题；日期写法覆盖常见变体，避免“1989 年 6 月 4 日”被空格绕过。
_SENSITIVE_PATTERNS = (
    re.compile(r"1989\s*年?\s*6\s*月?\s*4\s*日?"),
    re.compile(r"1989[-/.]0?6[-/.]0?4"),
    re.compile(r"六四"),
    re.compile(r"天安门.{0,8}(事件|屠杀|镇压|学运|学生|民主|纪念)"),
    re.compile(r"法轮功"),
    re.compile(r"台独|藏独|疆独|港独"),
    re.compile(r"颠覆国家政权|反党反政府"),
    re.compile(r"恐怖袭击|恐怖组织"),
    re.compile(r"(制造|制作|自制).{0,4}(炸弹|枪支|毒品|炸药)"),
    re.compile(r"制毒|冰毒|海洛因.{0,4}(配方|制作)"),
)


def check_sensitive_topic(text: str) -> str:
    """返回命中的敏感主题说明；未命中返回空串（纯函数，离线可测）。"""
    value = (text or "").strip()
    if not value:
        return ""
    for pattern in _SENSITIVE_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(0)
    return ""


def _check_safety_guard() -> list:
    """离线自测：敏感主题命中、普通产品问题不误伤。"""
    problems: list = []
    must_hit = (
        "1989年6月4日的天安门",
        "1989 年 6 月 4 日发生了什么",
        "六四事件",
        "天安门事件",
        "法轮功是什么",
        "台独主张",
        "怎么制造炸弹",
    )
    for text in must_hit:
        if not check_sensitive_topic(text):
            problems.append(f"敏感主题没拦住：{text}")

    must_pass = (
        "烫金膜盒怎么安装？",
        "HAK180 打印质量不清晰怎么办",
        "天安门怎么走",  # 只有地点，没有事件语义，不应误伤
        "万用表怎么测直流电压",
        "我的订单什么时候发货",
    )
    for text in must_pass:
        reason = check_sensitive_topic(text)
        if reason:
            problems.append(f"普通问题被误伤（命中 {reason!r}）：{text}")
    return problems
