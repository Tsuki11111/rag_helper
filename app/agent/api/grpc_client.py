"""
gRPC 测试客户端 —— 同时是**给 Java 侧的参考实现**

Java 侧开工时照它写 grpc-java 客户端即可：proto（proto/agent.proto）+ 本文件
≈ 最小接口文档。覆盖：Converse 事件流、**会话续接**（同 session 两轮）、
ListSessions、GetHistory（Stop 的用法见 grpc_service 的契约注释）。

身份走 metadata：authorization: Bearer <访问密钥> + x-user-id。

运行：
    .venv/Scripts/python.exe -m app.agent.api.grpc_client <访问密钥> ["问题"]
"""
import sys

import grpc

from app.agent.api.proto_gen import agent_pb2, agent_pb2_grpc

TARGET = "127.0.0.1:8003"


def _meta(key: str, user_id: str = "local"):
    return [("authorization", f"Bearer {key}"), ("x-user-id", user_id)]


def _print_stream(stream) -> str:
    """消费事件流并打印（返回 session_id 以便续接演示）"""
    session_id = ""
    for ev in stream:
        kind = ev.WhichOneof("kind")
        if kind == "start":
            session_id = ev.start.session_id
            print(f"  [start] session={session_id} run={ev.start.run_id}")
        elif kind == "trace":
            print(f"  [trace·{ev.trace.kind}] {ev.trace.agent or '-'} | {ev.trace.detail[:90]}")
        elif kind == "usage":
            u = ev.usage
            print(f"  [usage] calls={u.calls} tokens={u.total_tokens} "
                  f"cost=¥{u.cost:.4f} elapsed={u.elapsed_ms}ms")
        elif kind == "final":
            print(f"  [final] {ev.final.answer[:150]}")
        elif kind == "error":
            print(f"  [error] {ev.error.message}")
        else:
            print(f"  [{kind}]")
    return session_id


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python -m app.agent.api.grpc_client <访问密钥> [问题]")
        return
    key = sys.argv[1]
    question = sys.argv[2] if len(sys.argv) > 2 else "帮我查下订单 12345 到哪了"
    md = _meta(key)

    with grpc.insecure_channel(TARGET) as ch:
        stub = agent_pb2_grpc.AgentServiceStub(ch)

        # ① ListSessions
        try:
            sessions = stub.ListSessions(agent_pb2.ListSessionsRequest(limit=5), metadata=md)
            print(f"[ListSessions] {len(sessions.sessions)} 个会话（最近 5）：")
            for s in sessions.sessions:
                print(f"  - {s.session_id[:20]}  {s.title[:24]!r}  （{s.count} 条）")
        except grpc.RpcError as e:
            print("[ListSessions] 失败:", e.code(), e.details())
            return

        # ② Converse（首轮）
        print(f"[Converse] 问题：{question}")
        try:
            session_id = _print_stream(
                stub.Converse(agent_pb2.ConverseRequest(message=question), metadata=md))
        except grpc.RpcError as e:
            print("[Converse] 失败:", e.code(), e.details())
            return

        # ③ 会话续接（同 session 再问一轮 —— 上下文来自 chat_message）
        if session_id:
            print("[Converse·续接] 同一会话再问一轮（「那这个东西怎么用？」）……")
            try:
                _print_stream(stub.Converse(agent_pb2.ConverseRequest(
                    session_id=session_id, message="那这个东西怎么用？"), metadata=md))
            except grpc.RpcError as e:
                print("[Converse·续接] 失败:", e.code(), e.details())

        # ④ GetHistory
        if session_id:
            try:
                hist = stub.GetHistory(
                    agent_pb2.HistoryRequest(session_id=session_id, limit=10), metadata=md)
                print(f"[GetHistory] {len(hist.messages)} 条：")
                for m in hist.messages:
                    print(f"  - {m.role}: {m.text[:50]}")
            except grpc.RpcError as e:
                print("[GetHistory] 失败:", e.code(), e.details())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
