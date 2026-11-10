"""主 agent 事件契约。

`run_main_agent` 产出的 `final` 事件是 HTTP SSE / gRPC / CLI 共用的原料。此前
HTTP 桥接只映射了 answer/images/web_only/forms，漏掉 products；gRPC 又各自
手写一份字段映射，新增字段很容易只补一条链路。这里把 final 的字段收敛为
一个构造函数，所有 transport 都从同一份 payload 取值。
"""
from typing import Any

# final 事件的规范字段（新增字段只改这里与两个适配器）
FINAL_FIELDS = ("text", "images", "products", "forms", "web_only")


def build_final_event(ev: dict) -> dict[str, Any]:
    """把内部 final 事件收敛成 transport 共用的规范 payload。

    :param ev: `run_main_agent` 的 `{"type": "final", ...}` 事件
    :return: 含 text/images/products/forms/web_only 的 dict（缺省为空/False）
    """
    return {
        "text": ev.get("text") or "",
        "images": [i for i in (ev.get("images") or []) if isinstance(i, dict)],
        "products": [p for p in (ev.get("products") or []) if isinstance(p, dict)],
        "forms": [f for f in (ev.get("forms") or []) if isinstance(f, dict)],
        "web_only": bool(ev.get("web_only")),
    }
