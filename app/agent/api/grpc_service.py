"""
询问链路 gRPC 服务端（端口 8003 —— 8001 导入 / 8002 HTTP 询问 / 8003 gRPC 询问）

与 HTTP /query **复用同一个 `run_main_agent`**（统一询问入口，evolution-plan §1）。
契约见 proto/agent.proto（Java 侧并行开发的接口凭据）；生成命令：

    .venv/Scripts/python.exe -m grpc_tools.protoc -Iproto \
        --python_out=app/agent/api/proto_gen --grpc_python_out=app/agent/api/proto_gen \
        --pyi_out=app/agent/api/proto_gen proto/agent.proto

身份走 metadata（等价 HTTP 头）：authorization: Bearer <服务端密钥> + x-user-id。
缺密钥 / 无效 → UNAUTHENTICATED；缺 x-user-id **本机放行**（落 \"local\"）——
生产环境由 Java 侧保证注入（§6「内网信任 + X-User-Id」）。

实现说明：**同步 grpc + 线程池**（不是 grpc.aio）——业务函数 `run_main_agent`
是同步阻塞的，线程池与现有 FastAPI / BackgroundTasks 的模型一致；
aio 反而要线程桥接，纯增复杂度。内网明文（TLS 按需再加）。
"""
import queue
import sys
import threading
import uuid

import grpc
from concurrent import futures

from app.agent.api.proto_gen import agent_pb2, agent_pb2_grpc
from app.agent.main_agent import run_main_agent
from app.clients.mongo_history_clients import (
    ensure_session_owner,
    get_recent_messages,
    list_sessions,
)
from app.clients.mongo_user_clients import verify_key
from app.core.logger import logger
from app.utils.task_utils import request_stop

NODE_NAME = "grpc_service"
GRPC_PORT = 8003


class AgentServiceServicer(agent_pb2_grpc.AgentServiceServicer):
    """AgentService 实现：Converse 事件流 / Stop / ListSessions / GetHistory"""

    # ── 身份 ──
    @staticmethod
    def _ident(context) -> dict:
        meta = {kv.key: kv.value for kv in context.invocation_metadata()}
        auth = meta.get("authorization") or ""
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        user = verify_key(token) if token else None
        if not user:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "访问密钥无效或缺失")
        user_id = (meta.get("x-user-id") or "").strip() or "local"
        # 本轮工具票据：agent 回调 Java 内部接口时带回，只放 ContextVar，不进模型上下文
        tool_ticket = (meta.get("x-agent-tool-ticket") or "").strip()
        return {"tenant_id": user.get("tenant_id"), "user_id": user_id,
                "tool_ticket": tool_ticket}

    # ── 事件映射：run_main_agent 的事件 → proto Event ──
    @staticmethod
    def _to_event(ev: dict):
        t = ev.get("type")
        if t == "step":
            return agent_pb2.Event(trace=agent_pb2.Trace(
                kind=ev.get("kind") or "", agent=ev.get("agent") or "",
                detail=ev.get("detail") or ""))
        if t == "final":
            return agent_pb2.Event(final=agent_pb2.FinalEvent(
                answer=ev.get("text") or "",
                images=[agent_pb2.Image(url=i.get("url") or "", caption=i.get("caption") or "")
                        for i in (ev.get("images") or []) if isinstance(i, dict)],
                web_only=bool(ev.get("web_only"))))
        if t == "error":
            return agent_pb2.Event(error=agent_pb2.ErrorEvent(
                message=ev.get("detail") or "未知错误"))
        if t == "usage":
            s = ev.get("summary") or {}
            return agent_pb2.Event(usage=agent_pb2.Usage(
                calls=int(s.get("calls") or 0), failed=int(s.get("failed") or 0),
                total_tokens=int(s.get("total_tokens") or 0),
                cost=float(s.get("cost") or 0.0),
                elapsed_ms=int(s.get("elapsed_ms") or 0),
                unpriced_calls=int(s.get("unpriced_calls") or 0)))
        # paused（用户点了 Stop）：proto 无单独事件 —— 不推事件、流正常终止，
        # Java 侧以 Stop 的返回 + 流结束（无 Final）判定（写进契约注释，见 proto）
        return None

    # ── RPC ──
    def Converse(self, request, context):
        ident = self._ident(context)
        session_id = request.session_id or str(uuid.uuid4())
        # 会话归属：同一 session_id 不能被另一个 x-user-id 接管
        try:
            ensure_session_owner(session_id, ident["user_id"], ident["tenant_id"])
        except PermissionError as e:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, str(e))
        except Exception as e:
            logger.error(f"[{NODE_NAME}] 会话归属校验失败：{e}", exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, "会话归属校验失败")
        run_id = uuid.uuid4().hex[:16]   # 注入执行函数：暂停令牌 = 日志 trace = 本轮登记
        logger.info(f"[{NODE_NAME}] Converse：session={session_id} run={run_id} "
                    f"user={ident['user_id']} tool_ticket="
                    f"{'on' if ident['tool_ticket'] else 'off'} 问题={request.message[:60]!r}")
        yield agent_pb2.Event(start=agent_pb2.SessionStart(
            session_id=session_id, run_id=run_id))

        # run_main_agent 是同步的：工作线程里跑，事件经 queue（线程安全）回到本生成器
        q: "queue.Queue[dict]" = queue.Queue()

        def worker():
            try:
                result = run_main_agent(
                    session_id, request.message,
                    on_event=q.put,
                    on_usage=lambda summary: q.put({"type": "usage", "summary": summary}),
                    tenant_id=ident["tenant_id"], user_id=ident["user_id"], run_id=run_id,
                    tool_ticket=ident["tool_ticket"])
                q.put({"_signal": "done", "result": result})
            except Exception as e:
                # run_main_agent 自带异常兜底；这里防桥接层自身出问题
                q.put({"_signal": "crash", "detail": f"{type(e).__name__}: {e}"})

        threading.Thread(target=worker, daemon=True).start()
        while True:
            ev = q.get()
            signal = ev.get("_signal")
            if signal == "done":
                break                       # final / paused 事件已在流中发过
            if signal == "crash":
                yield agent_pb2.Event(error=agent_pb2.ErrorEvent(message=ev["detail"]))
                break
            mapped = self._to_event(ev)
            if mapped is not None:
                yield mapped

    def Stop(self, request, context):
        ident = self._ident(context)
        try:
            ensure_session_owner(request.session_id, ident["user_id"], ident["tenant_id"])
        except PermissionError as e:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, str(e))
        ok = request_stop(request.session_id, request.run_id)
        logger.info(f"[{NODE_NAME}] Stop：session={request.session_id} "
                    f"run={request.run_id} → {ok}")
        return agent_pb2.StopReply(stopped=bool(ok))

    def ListSessions(self, request, context):
        ident = self._ident(context)
        rows = list_sessions(limit=request.limit or 50, user_id=ident["user_id"])
        return agent_pb2.ListSessionsReply(sessions=[
            agent_pb2.SessionInfo(
                session_id=r.get("session_id") or "", title=r.get("title") or "",
                count=int(r.get("count") or 0), last_at=str(r.get("last_at") or ""))
            for r in rows])

    def GetHistory(self, request, context):
        ident = self._ident(context)
        try:
            ensure_session_owner(request.session_id, ident["user_id"], ident["tenant_id"])
        except PermissionError as e:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, str(e))
        msgs = get_recent_messages(request.session_id, limit=request.limit or 10,
                                   user_id=ident["user_id"])
        return agent_pb2.HistoryReply(messages=[
            agent_pb2.HistoryMessage(
                role=m.get("role") or "", text=m.get("text") or "",
                images=[agent_pb2.Image(url=i.get("url") or "", caption=i.get("caption") or "")
                        for i in (m.get("images") or []) if isinstance(i, dict)],
                ts=str(m.get("ts") or ""),
                trace=[agent_pb2.Trace(
                    kind=t.get("kind") or "", agent=t.get("agent") or "",
                    detail=t.get("detail") or "")
                    for t in (m.get("trace") or []) if isinstance(t, dict)])
            for m in msgs])


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    agent_pb2_grpc.add_AgentServiceServicer_to_server(AgentServiceServicer(), server)
    server.add_insecure_port(f"[::]:{GRPC_PORT}")   # 内网明文；TLS 按需再加（§6）
    server.start()
    logger.info(f"[{NODE_NAME}] gRPC 服务已启动，端口 {GRPC_PORT}（内网）")
    server.wait_for_termination()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    serve()
