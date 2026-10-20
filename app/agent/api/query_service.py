"""
知识库查询 Web 服务

把询问链路（主 agent 编排 → subagent 干活）包装成 HTTP 接口，
并用 SSE 实时推送执行流与最终答案。

时序：
    POST /query（is_stream=true） → 建 SSE 队列 → 后台跑主 agent → 立即返回 session_id
    GET  /stream/{session_id}    → 前端订阅，持续收到 trace / usage / final
"""
import sys
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

from app.agent.main_agent import run_main_agent
from app.clients.mongo_history_clients import clear_history, get_recent_messages, list_sessions
from app.clients.mongo_run_clients import get_query_run_tool
from app.clients.mongo_checkpoint_clients import KIND_MONGO, get_checkpointer
from app.core.logger import logger
from app.core.request_context import new_trace_id
from app.utils.auth_utils import clear_session_cookie, current_tenant, set_session_cookie
from app.utils.sse_utils import (
    SSEEvent,
    create_sse_queue,
    push_to_session,
    sse_generator,
)
from app.utils.task_utils import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PAUSED,
    TASK_STATUS_PROCESSING,
    clear_task,
    request_stop,
    update_task_status,
)

# 服务端口：8000 被 Attu 占用，8001 被导入服务使用，故查询服务用 8002
SERVICE_PORT = 8002

# 节点名，用于日志前缀
NODE_NAME = "query_service"

app = FastAPI(
    title="买小购 Query Service",
    description="买小购客服智能体查询服务（SSE 流式推送执行流程与答案）"
)

# 跨域配置：允许前端页面独立部署时调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    """查询请求数据结构"""
    query: str = Field(..., description="用户问题")
    session_id: str = Field(None, description="会话ID，不传则自动生成")
    is_stream: bool = Field(False, description="是否流式返回")
    # 前端「联网」开关。默认 True = 与改动前一致，别让老调用方静默换行为
    enable_web_search: bool = Field(True, description="是否让联网搜索参与本轮检索")


def _run_main_agent_bridged(session_id: str, user_query: str, is_stream: bool,
                            tenant_id: str, run_id: str,
                            enable_web_search: bool = True) -> None:
    """
    后台线程里跑统一执行函数（主 agent），把事件桥接成 SSE 推送

    `run_id` 由 /query 端点生成并注入（前端拿它当暂停令牌）——
    保证「暂停令牌 = 日志 trace = 本轮登记」三者同 id。
    """
    on_usage = None
    if is_stream:
        on_usage = lambda summary: push_to_session(session_id, SSEEvent.USAGE, summary)

    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "step":
            # 统一执行流：主 agent 的调度步骤 + subagent 图内事件（前端渲染成灰色小字）
            push_to_session(session_id, SSEEvent.TRACE, {
                "kind": ev.get("kind"), "agent": ev.get("agent"),
                "detail": ev.get("detail"),
            })
        elif t == "final":
            push_to_session(session_id, SSEEvent.FINAL, {
                "answer": ev.get("text") or "", "status": "completed",
                "images": ev.get("images") or [], "web_only": bool(ev.get("web_only")),
            })
        elif t == "paused":
            push_to_session(session_id, SSEEvent.PAUSED, {})
        elif t == "error":
            push_to_session(session_id, SSEEvent.ERROR,
                            {"error": ev.get("detail") or "未知错误"})

    try:
        result = run_main_agent(session_id, user_query, on_event=on_event,
                                tenant_id=tenant_id, on_usage=on_usage, run_id=run_id,
                                enable_web_search=enable_web_search)
        outcome = result.get("outcome")
        if outcome == "error":
            update_task_status(session_id, TASK_STATUS_FAILED, False)
        elif outcome == "paused":
            update_task_status(session_id, TASK_STATUS_PAUSED, False)
        else:
            update_task_status(session_id, TASK_STATUS_COMPLETED, False)
    except Exception as e:
        # run_main_agent 自己兜异常；这里防桥接层自身出问题（与"记账绝不能弄坏主流程"同一条规矩）
        logger.error(f"[{NODE_NAME}] [_run_main_agent_bridged] 桥接层异常：{e}", exc_info=True)
        push_to_session(session_id, SSEEvent.ERROR, {"error": f"{type(e).__name__}: {e}"})
        update_task_status(session_id, TASK_STATUS_FAILED, False)


class LoginRequest(BaseModel):
    """登录请求：提交访问密钥"""
    key: str


@app.on_event("startup")
async def on_startup():
    """
    服务启动时预热用户表

    顺带检查有没有用户——一个都没有时所有数据接口都会 401，提前把话说明白。
    """
    from app.clients.mongo_user_clients import count_users, get_user_tool
    get_user_tool()
    logger.info("用户工具已就绪（MongoDB）")
    if count_users() == 0:
        logger.warning(
            "还没有任何用户，数据接口将全部返回 401。"
            "先建一个：.venv/Scripts/python.exe -m app.clients.mongo_user_clients add <称呼>"
        )


@app.post("/login", summary="登录：校验访问密钥并写入会话 Cookie")
async def login(payload: LoginRequest, response: Response):
    """
    用访问密钥换一个 HttpOnly Cookie

    走 Cookie 而不是让前端存密钥发请求头：本服务的流式接口用 `EventSource`，
    它**无法自定义请求头**，只有 Cookie 能被浏览器自动携带。
    """
    function_name = sys._getframe().f_code.co_name
    from app.clients.mongo_user_clients import verify_key

    user = verify_key(payload.key)
    if not user:
        logger.warning(f"[{NODE_NAME}] [{function_name}] 登录失败：密钥无效或已撤销")
        raise HTTPException(status_code=401, detail="访问密钥无效或已撤销")

    set_session_cookie(response, payload.key)
    logger.info(
        f"[{NODE_NAME}] [{function_name}] 登录成功：{user['name']}"
        f"（{user['role']}，租户 {user['tenant_id']}）"
    )
    return {"code": 200, "name": user["name"], "role": user["role"]}


@app.post("/logout", summary="退出登录")
async def logout(response: Response):
    """清掉会话 Cookie"""
    clear_session_cookie(response)
    return {"code": 200}


@app.get("/chat.html", summary="聊天页面")
async def chat():
    """返回前端聊天页面"""
    # 本文件位于 app/agent/api/，页面在 app/agent/page/
    page_path = Path(__file__).absolute().parent.parent / "page" / "chat.html"
    if not page_path.exists():
        logger.error(f"聊天页面不存在：{page_path}")
        raise HTTPException(status_code=404, detail=f"没有查询到页面，地址为：{page_path}！")
    return FileResponse(page_path, media_type="text/html")


@app.post("/query", summary="提交查询")
async def query(background_tasks: BackgroundTasks, request: QueryRequest,
                user: dict = Depends(current_tenant)):
    """
    统一询问入口：接收用户提问，内部一律由主 agent 编排（产品 / 订单 / 将来的新能力）

    流式模式：建 SSE 队列 → 后台跑 `_run_main_agent_bridged` → 立即返回 session_id + run_id
             （前端订阅 /stream；调度步骤以 `trace` 事件推送，M5 渲染灰字）
    非流式模式：同步跑完直接返回答案与用量

    鉴权用参数形式而非 `dependencies=[...]`：需要拿到 tenant_id 才能把这次问答的
    成本记到调用方名下。
    """
    function_name = sys._getframe().f_code.co_name
    user_query = request.query
    session_id = request.session_id or str(uuid.uuid4())
    is_stream = request.is_stream
    tenant_id = user.get("tenant_id")
    # 本轮标识：注入 run_main_agent 与日志 trace 共用一个 id，并作为「暂停令牌」下发给前端
    # （前端点暂停时原样带回，见 POST /query/{session_id}/stop）
    run_id = new_trace_id()

    logger.info(f"[{NODE_NAME}] [{function_name}] 收到查询，session={session_id}，流式={is_stream}，问题={user_query}")

    if is_stream:
        # 建队列必须在跑之前：否则事件推送时队列还不存在，会丢失
        create_sse_queue(session_id)
        update_task_status(session_id, TASK_STATUS_PROCESSING, False)
        background_tasks.add_task(_run_main_agent_bridged,
                                  session_id, user_query, True, tenant_id, run_id,
                                  request.enable_web_search)
        return {
            "message": "结果正在处理中...",
            "session_id": session_id,
            "run_id": run_id,
        }

    # 非流式：同步跑完直接返回
    update_task_status(session_id, TASK_STATUS_PROCESSING, False)
    result = run_main_agent(session_id, user_query, tenant_id=tenant_id, run_id=run_id,
                            enable_web_search=request.enable_web_search)
    clear_task(session_id)
    outcome = result.get("outcome")
    _messages = {"answered": "处理完成！", "blocked": "处理完成！",
                 "paused": "本轮已被暂停", "error": "已中止"}
    return {
        "message": _messages.get(outcome, "处理完成！"),
        "session_id": session_id,
        "run_id": run_id,
        "outcome": outcome,
        "error": result.get("error") or "" if outcome == "error" else "",
        "answer": result.get("reply") or "",
        # 结构化配图 [{"url", "caption"}]（M5 起主 agent 链路贯通）
        "images": result.get("images") or [],
        # 本轮只有联网结果、向量库一条都没搜到 —— 前端据此挂「内容来自网络」横幅
        "web_only": bool(result.get("web_only")),
        # 本次问答花了多少：调用次数 / tokens / 估算成本（明细见 llm_usage 集合）
        "usage": result.get("usage") or {},
    }


class StopRequest(BaseModel):
    """暂停请求：带上本轮 run_id，只认当前在跑的那一轮"""
    run_id: str = Field(..., description="POST /query 返回的 run_id")


@app.post("/query/{session_id}/stop", summary="暂停当前这一轮生成")
async def stop_query(session_id: str, payload: StopRequest,
                     user: dict = Depends(current_tenant)):
    """
    请求暂停该会话当前正在跑的这一轮

    在共享存储里置一个「本轮被请求停止」的标记即可，不直接杀线程 ——
    生成节点在流式循环里每收一块查一次（`node_answer_output._generate`），
    置位就跳出循环、把本轮标记为作废，流程照常走到 END。
    **不反问用户**：想让模型怎么改，用户自己重新提问。

    **必须带 run_id**：前端的 session_id 跨轮复用，用户点慢了、或网络延迟导致
    上一轮的暂停信号晚到，不带 run_id 就会误杀下一轮。

    鉴权用 `dependencies` 形式：这里不需要 tenant_id（本轮的成本在跑图那边已经记好）。
    """
    function_name = sys._getframe().f_code.co_name
    ok = request_stop(session_id, payload.run_id)
    if ok:
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 收到暂停请求，session={session_id}，"
            f"run={payload.run_id}"
        )
    else:
        # 不是错误：用户点慢了一点，这一轮已经结束。如实返回，别让前端以为暂停生效了
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 暂停请求已过期（该轮不在跑），"
            f"session={session_id}，run={payload.run_id}"
        )
    return {"stopped": ok}


@app.get("/stream/{session_id}", summary="SSE 流式获取结果", dependencies=[Depends(current_tenant)])
async def stream(session_id: str, request: Request):
    """
    建立 SSE 长连接，实时推送执行流与最终答案

    推送的事件类型（M5 起泳道退役，只剩这几种）：
    - ready    ：连接建立
    - trace    ：统一执行流 —— 主 agent 的调度步骤 + subagent 图内事件（前端渲染灰字）
    - usage    ：每记完一笔账推一次累计用量（前端「本次消耗」条）
    - paused   ：本轮被用户主动暂停（生成作废）
    - final    ：本轮结束：完整答案 + 结构化配图 + web_only
    - error    ：执行异常
    """
    logger.info(f"[{NODE_NAME}] [stream] 建立SSE连接，session={session_id}")
    return StreamingResponse(
        sse_generator(session_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 禁用Nginx等反向代理的缓冲，否则事件会被攒着一起发
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health", summary="健康检查")
async def health():
    """检查服务是否正常"""
    return {"ok": True}


@app.get("/sessions", summary="列出所有会话", dependencies=[Depends(current_tenant)])
async def get_sessions(limit: int = 50):
    """
    列出会话，按最近活跃倒序 —— 给前端左侧的会话栏用

    只有这一处「跨会话」的读取接口：其余接口都要先知道 `session_id`。
    返回的 `last_at` 取自 ObjectId 内嵌时间戳（不是 `ts`，它不可信，见 HANDOFF §4.18）。

    :param limit: 最多返回多少个会话，默认50
    """
    function_name = sys._getframe().f_code.co_name
    try:
        items = list_sessions(limit=limit)
    except Exception as e:
        logger.error(f"[{NODE_NAME}] [{function_name}] 列出会话失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"列出会话失败：{e}")

    logger.info(f"[{NODE_NAME}] [{function_name}] 会话列表查询，返回{len(items)}个")
    return {"items": items}


@app.get("/history/{session_id}", summary="查询会话历史", dependencies=[Depends(current_tenant)])
async def get_history(session_id: str, limit: int = 50):
    """
    查询指定会话的历史对话记录（时间正序）

    :param session_id: 会话ID
    :param limit: 返回条数上限，默认50
    """
    function_name = sys._getframe().f_code.co_name
    try:
        rows = get_recent_messages(session_id, limit=limit)
    except Exception as e:
        logger.error(f"[{NODE_NAME}] [{function_name}] 查询历史失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"查询历史失败：{e}")

    items = [{
        "_id": str(r.get("_id")) if r.get("_id") is not None else "",
        "session_id": r.get("session_id", ""),
        "role": r.get("role", ""),
        "text": r.get("text", ""),
        "rewritten_query": r.get("rewritten_query", ""),
        "item_names": r.get("item_names") or [],
        # 答案配图 [{"url", "caption"}] —— 前端靠它把历史里的图卡连图注一起还原
        "images": r.get("images") or [],
        # 本轮执行流 [{"kind", "agent", "detail"}]（M5.1）—— 前端还原成可展开的
        # 「执行流」块；此字段上线前的老消息没有它，按空列表下发
        "trace": r.get("trace") or [],
        "ts": r.get("ts"),
    } for r in rows]

    logger.info(f"[{NODE_NAME}] [{function_name}] 会话{session_id}历史查询，返回{len(items)}条")
    return {"session_id": session_id, "items": items}


def _delete_session_checkpoints(session_id: str) -> int:
    """
    删掉某个会话留下的检查点，返回删了几个线程（**尽力而为，不抛异常**）

    **为什么要专门做这件事**：检查点按 `run_id`（就是 thread_id）存，与 `session_id`
    没有索引关联 —— 所以「删会话」以前清不掉它们，只能等 7 天 TTL 慢慢回收。
    现在运行记录里有 `(session_id → trace_id)` 这层关系，可以反查出来逐个删。

    **依赖「每轮都写运行记录」**：收尾的四条分支（正常 / 暂停 / 中断 / 异常）都会写，
    所以不漏；但**运行记录上线之前**跑过的会话查不到，那些仍只能靠 TTL。
    """
    try:
        trace_ids = [
            r.get("trace_id")
            for r in get_query_run_tool().collection.find({"session_id": session_id}, {"trace_id": 1})
            if r.get("trace_id")
        ]
        if not trace_ids:
            return 0
        saver, kind = get_checkpointer()
        if kind != KIND_MONGO:
            return 0        # 内存版 saver 本来就不跨进程，没有需要清的东西
        deleted = 0
        for trace_id in trace_ids:
            try:
                saver.delete_thread(trace_id)
                deleted += 1
            except Exception as e:
                logger.warning(f"[{NODE_NAME}] 删除检查点失败（忽略）：{trace_id}：{e}")
        return deleted
    except Exception as e:
        # 清检查点失败不该让「清空历史」这个操作失败：历史已经清了，这点残留有 TTL 兜底
        logger.warning(f"[{NODE_NAME}] 清理会话检查点失败（忽略）：{e}")
        return 0


def _check_delete_session_checkpoints() -> list:
    """
    离线自测：删会话时**真的会去清检查点**（要 Mongo；saver 打桩，不跑图）

    守的是那个静默失效点：**「以为删干净了，其实检查点还在」** —— 不报错、界面也不变样，
    只是那 7 天里它们一直躺着。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    sid = "selftest_del_ckpt"
    col = get_query_run_tool().collection
    col.delete_many({"session_id": sid})
    import asyncio

    class _FakeSaver:
        def __init__(self):
            self.deleted = []

        def delete_thread(self, thread_id):
            self.deleted.append(thread_id)

    fake = _FakeSaver()
    real_get_checkpointer = globals()["get_checkpointer"]
    globals()["get_checkpointer"] = lambda: (fake, KIND_MONGO)
    try:
        col.insert_many([
            {"session_id": sid, "trace_id": "selftest_ckpt_1", "outcome": "answered", "ts": time.time()},
            {"session_id": sid, "trace_id": "selftest_ckpt_2", "outcome": "paused", "ts": time.time()},
            {"session_id": "别的会话", "trace_id": "selftest_ckpt_other", "outcome": "answered",
             "ts": time.time()},
        ])
        count = _delete_session_checkpoints(sid)
        if count != 2:
            problems.append(f"应删 2 个检查点线程，实为 {count}")
        if set(fake.deleted) != {"selftest_ckpt_1", "selftest_ckpt_2"}:
            problems.append(f"删的线程不对（应只删本会话的）：{fake.deleted}")
        if "selftest_ckpt_other" in fake.deleted:
            problems.append("删到了别的会话的检查点")

        # **调用点也要守**：helper 写对了但接口里没人调，照样是「以为删干净了」（§3.19 的教训）。
        # 直接 await 那个 handler —— FastAPI 的依赖是路由层加的，函数本身能直接调
        fake.deleted.clear()
        resp = asyncio.run(clear_session_history(sid))
        if resp.get("checkpoint_threads_deleted") != 2:
            problems.append(
                f"走接口时没有清本会话的检查点：返回 "
                f"{resp.get('checkpoint_threads_deleted')!r}，应为 2（helper 是不是没被调用？）")
        elif set(fake.deleted) != {"selftest_ckpt_1", "selftest_ckpt_2"}:
            problems.append(f"走接口时删的线程不对：{fake.deleted}")
    except Exception as e:
        problems.append(f"清检查点路径抛异常：{type(e).__name__}: {e}")
    finally:
        globals()["get_checkpointer"] = real_get_checkpointer
        col.delete_many({"session_id": {"$in": [sid, "别的会话"]}})
    return problems


@app.delete("/history/{session_id}", summary="清空会话历史", dependencies=[Depends(current_tenant)])
async def clear_session_history(session_id: str):
    """删除指定会话的全部历史对话记录"""
    function_name = sys._getframe().f_code.co_name
    try:
        deleted = clear_history(session_id)
    except Exception as e:
        logger.error(f"[{NODE_NAME}] [{function_name}] 清空历史失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"清空历史失败：{e}")

    # 顺带清掉这一会话留下的检查点 —— 它们按 run_id 存、与 session_id 无索引关联，
    # 以前删会话清不掉、只能等 7 天 TTL（见 _delete_session_checkpoints 的说明）
    threads = _delete_session_checkpoints(session_id)

    logger.info(f"[{NODE_NAME}] [{function_name}] 会话{session_id}历史已清空，"
                f"删除{deleted}条，检查点线程{threads}个")
    return {"message": "History cleared", "session_id": session_id,
            "deleted_count": deleted, "checkpoint_threads_deleted": threads}


if __name__ == "__main__":
    logger.info(f"[{NODE_NAME}] 查询服务启动中（端口{SERVICE_PORT}）...")
    uvicorn.run(app=app, host="127.0.0.1", port=SERVICE_PORT)
