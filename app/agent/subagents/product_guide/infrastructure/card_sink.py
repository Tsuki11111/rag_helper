"""商品卡片 ContextVar 通道；工具只负责把卡片交给主 agent 收尾。"""
from contextvars import ContextVar

PRODUCT_CARDS: ContextVar = ContextVar("product_cards", default=None)


def set_product_cards(bucket: list):
    return PRODUCT_CARDS.set(bucket)


def reset_product_cards(token) -> None:
    PRODUCT_CARDS.reset(token)


class ContextVarCardSink:
    def add(self, cards: list[dict]) -> None:
        bucket = PRODUCT_CARDS.get()
        if bucket is not None:
            bucket.extend(c for c in cards if isinstance(c, dict))
