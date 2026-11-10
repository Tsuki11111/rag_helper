"""一次询问内收集导购推荐的商品卡片（供 final 事件 / 历史存档用）。

与 RAG 配图的 `_SUB_IMAGES` 同一个模式：工具把结构化商品放进 ContextVar，
主 agent 收尾时合并去重、随 final 与 chat_message 一起下发。
模型只看工具返回的文本，卡片由平台渲染。
"""
from contextvars import ContextVar

PRODUCT_CARDS: ContextVar = ContextVar("product_cards", default=None)


def set_product_cards(bucket: list):
    return PRODUCT_CARDS.set(bucket)


def reset_product_cards(token) -> None:
    PRODUCT_CARDS.reset(token)


def add_product_cards(cards: list) -> None:
    bucket = PRODUCT_CARDS.get()
    if bucket is not None:
        bucket.extend(c for c in cards if isinstance(c, dict))
