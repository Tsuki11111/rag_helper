"""催退款执行图。

用户在平台确认催退款后，Java 通过 gRPC 内部事件 `urge-refund-confirmed` 触发本图：

    node_send_refund_message（agent 调 Java 发送工具，Java 校验并落库）
        → node_compose_refund_reply（完成 / 失败 + 代商家致歉）
        → node_save_reply（assistant 历史）

只有 REFUNDING（退款中）订单能走到这里；Java 发送工具会再次校验状态。
"""
from langgraph.constants import END
from langgraph.graph import StateGraph

from app.agent.sub_agents.order_service_agent.nodes.node_compose_refund_reply import (
    node_compose_refund_reply,
)
from app.agent.sub_agents.order_service_agent.nodes.node_save_reply import (
    node_save_reply,
)
from app.agent.sub_agents.order_service_agent.nodes.node_send_refund_message import (
    node_send_refund_message,
)
from app.agent.sub_agents.order_service_agent.state import (
    UrgeRefundState,
    create_urge_refund_state,
)

builder = StateGraph(UrgeRefundState)
builder.add_node("node_send_refund_message", node_send_refund_message)
builder.add_node("node_compose_refund_reply", node_compose_refund_reply)
builder.add_node("node_save_reply", node_save_reply)

builder.set_entry_point("node_send_refund_message")
builder.add_edge("node_send_refund_message", "node_compose_refund_reply")
builder.add_edge("node_compose_refund_reply", "node_save_reply")
builder.add_edge("node_save_reply", END)

_urge_refund_app = None


def get_urge_refund_app():
    """惰性编译；一次性内部事件，不需要 checkpointer。"""
    global _urge_refund_app
    if _urge_refund_app is None:
        _urge_refund_app = builder.compile()
    return _urge_refund_app


def run_urge_refund(session_id: str, user_id: str, tenant_id: str,
                    action_no: str) -> str:
    result = get_urge_refund_app().invoke(create_urge_refund_state(
        session_id=session_id,
        user_id=user_id or "",
        tenant_id=tenant_id or "",
        action_no=action_no or "",
    ))
    return result["reply"]


# ── 离线自测（registry 见 app/core/regression.py）──

def _check_urge_refund_graph() -> list:
    """催退款执行图：调发送工具、成功/失败文案、只写一条 assistant。"""
    import app.agent.sub_agents.order_service_agent.nodes.node_save_reply as save_node
    import app.agent.sub_agents.order_service_agent.nodes.node_send_refund_message as send_node

    problems = []
    saved = []
    calls = []
    original_call = send_node.call
    original_save = save_node.save_chat_message

    def _fake_save(*args, **kwargs):
        saved.append((args, kwargs))

    save_node.save_chat_message = _fake_save
    try:
        def _success_call(path, payload=None, method="POST"):
            calls.append((path, payload, method))
            return ({"status": "SUCCEEDED",
                     "result": {"orderNo": "NO123", "merchantName": "平台自营",
                                "messageId": 18, "refundReason": "商品有瑕疵",
                                "refundAppliedAt": "2026-10-24T10:00:00"},
                     "errorMessage": ""}, None)

        send_node.call = _success_call
        reply = run_urge_refund("sid-refund", "42", "tenant-a", "ACTION1")
        for expected in ("催退款已完成", "NO123", "平台自营", "抱歉"):
            if expected not in reply:
                problems.append(f"成功回复缺少「{expected}」：{reply!r}")
        if not calls or not calls[0][0].endswith(
                "/internal/agent/actions/urge-refund/send"):
            problems.append(f"未调用发送工具：{calls!r}")
        if not calls or calls[0][1].get("actionNo") != "ACTION1":
            problems.append(f"发送工具未带 actionNo：{calls!r}")
        if len(saved) != 1:
            problems.append(f"成功回复应只写 1 条历史，实际 {len(saved)} 条")
        elif saved[0][0][1] != "assistant":
            problems.append(f"回复写入角色不是 assistant：{saved[0][0][1]!r}")

        saved.clear()
        send_node.call = lambda path, payload=None, method="POST": (
            None, "商家已处理退款")
        reply = run_urge_refund("sid-refund", "42", "tenant-a", "ACTION2")
        if "没有成功" not in reply or "抱歉" not in reply:
            problems.append(f"失败回复不完整：{reply!r}")
        if len(saved) != 1:
            problems.append(f"失败回复应只写 1 条历史，实际 {len(saved)} 条")
    finally:
        send_node.call = original_call
        save_node.save_chat_message = original_save

    return problems
