"""
知识库查询 Web 服务

把 LangGraph 检索流程（产品名确认 → 四路检索 → RRF 融合 → 重排 → 生成答案）
包装成 HTTP 接口，并用 SSE 实时推送执行流程与流式答案。

四个核心模块协同：
1. Web 服务层（本文件）：接收请求、建立 SSE 连接
2. SSE 工具层（app/utils/sse_utils.py）：消息队列、打包、推送事件
3. 任务状态层（app/utils/task_utils.py）：记录节点执行进度，并触发 SSE 推送
4. 图节点执行层（app/query_process/agent/）：业务节点，更新状态驱动进度

时序：
    POST /query（is_stream=true） → 建 SSE 队列 → 后台跑图 → 立即返回 session_id
    GET  /stream/{session_id}    → 前端订阅，持续收到 progress / delta / final
"""
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

from app.clients.mongo_history_utils import clear_history, get_recent_messages, list_sessions
from app.conf.budget_config import budget_config
from app.core.budget import BudgetExceeded
from app.core.logger import logger
from app.core.request_context import new_trace_id
from app.core.usage_tracker import usage_context
from app.query_process.agent.main_graph import get_query_app
from app.clients.mongo_checkpoint_utils import graph_config, note_checkpointer_failure
from app.utils.auth_utils import clear_session_cookie, current_tenant, set_session_cookie
from app.utils.sse_utils import (
    SSEEvent,
    create_sse_queue,
    get_sse_queue,
    push_to_session,
    sse_generator,
)
from app.utils.task_utils import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PAUSED,
    TASK_STATUS_PROCESSING,
    TASK_STATUS_WAITING_USER,
    clear_active_run,
    clear_task,
    get_done_task_list,
    get_degraded_task_list,
    get_task_result,
    request_stop,
    reset_task_progress,
    set_active_run,
    set_task_result,
    update_task_status,
)

# 服务端口：8000 被 Attu 占用，8001 被导入服务使用，故查询服务用 8002
SERVICE_PORT = 8002

# 节点名，用于日志前缀
NODE_NAME = "query_service"

app = FastAPI(
    title="Query Service",
    description="掌柜智库知识库查询服务（SSE 流式推送执行流程与答案）"
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


def run_query_graph(session_id: str, user_query: str, is_stream: bool = True,
                    tenant_id: str = None, run_id: str = None,
                    resume: dict = None, enable_web_search: bool = True) -> dict:
    """
    后台执行检索图

    由 BackgroundTasks 触发，不阻塞 HTTP 响应。图内各节点会自行更新任务进度，
    而 task_utils 的进度更新会通过 push_to_session 推给 SSE 连接（仅流式模式）。

    整个执行过程包在 `usage_context` 里：一次问答 = 一个 trace，图内所有模型调用
    （含四路并发检索，各自在不同线程）都归到这条 trace 与调用方租户上。

    :param session_id: 会话ID，同时作为 SSE 队列的 key
    :param user_query: 用户原始问题（恢复那一轮用不上，传空串即可）
    :param is_stream: 是否流式推送
    :param tenant_id: 调用方租户，来自访问密钥
    :param run_id: 本轮标识。与日志 trace **共用同一个 id**，同时充当「暂停令牌」——
        前端点暂停时把它带回来，task_utils 只认当前登记的那一轮，从而挡掉迟到的
        暂停信号误杀下一轮。它还兼作 LangGraph 的 **`thread_id`**：一轮问答一个 thread，
        多轮之间不会串味。不传则本函数自行生成（命令行等无暂停需求的调用）。
    :param resume: 非 None 表示**恢复**一轮被中断的图（用户确认产品之后），内容形如
        `{"choice": "用户选的产品名"}`，会包成 `Command(resume=...)`。
        恢复**必须复用原来的 `run_id`**（它就是 thread_id），否则接不上那个断点。
    :param enable_web_search: 联网搜索是否参与本轮。关掉时联网那一路返回空，
        最终参考内容只可能来自知识库与图谱 —— 注意**它不是「摘掉节点」**，
        四路并发汇到 `node_join` 的 fan-in 一条都不能少
    :return: 本次问答的用量汇总（调用次数 / tokens / 估算成本）
    """
    function_name = sys._getframe().f_code.co_name
    run_id = run_id or new_trace_id()

    # 只有新开一轮才用得上初始状态；恢复那一轮的状态在检查点里
    init_state = {
        "original_query": user_query,
        "session_id": session_id,
        "is_stream": is_stream,
        "enable_web_search": enable_web_search,
    }
    # 流式模式下把用量实时推给前端（「本次消耗」那一行）：
    # 每记完一笔调一次，前端边看答案边看钱。
    # 非流式没有 SSE 队列，不注册回调，避免 push_to_session 刷「没有队列」的告警。
    on_usage = None
    if is_stream:
        on_usage = lambda summary: push_to_session(session_id, SSEEvent.USAGE, summary)

    # 新开一轮：先把上一轮留下的进度与结果清掉（done/running/degraded/need_confirm…）。
    # **续跑不调** —— 那一段要接着前一段累积（见 task_utils.reset_task_progress 的说明）
    if resume is None:
        reset_task_progress(session_id)

    set_active_run(session_id, run_id)
    try:
        # 整轮预算经上下文注入：**只有查询图注了这两个值**，导入侧不注 ——
        # `check_budget` 在节点开始前读它们，读不到就直接跳过（见 app/core/budget.py）
        with usage_context(trace_id=run_id, session_id=session_id, tenant_id=tenant_id,
                           node="query_graph", on_usage=on_usage,
                           wall_clock_budget=budget_config.query_wall_clock_budget,
                           token_budget=budget_config.query_token_budget) as acc:
            logger.info(
                f"[{NODE_NAME}] [{function_name}] {'恢复' if resume is not None else '开始'}"
                f"执行检索图，session={session_id}，trace={run_id}"
            )
            try:
                # 带上 thread_id（= 本轮 run_id）：一轮问答一个 thread，多轮之间不串味
                invoke_input = Command(resume=resume) if resume is not None else init_state
                final_state = get_query_app().invoke(invoke_input, graph_config(run_id))

                # 把最终答案存入任务结果，供非流式模式取用
                answer = (final_state or {}).get("answer", "")
                set_task_result(session_id, "answer", answer)
                # 配图同样要带出去，否则非流式模式拿不到
                set_task_result(session_id, "images", (final_state or {}).get("images") or [])
                # 本轮是不是「只有联网结果」（向量库一条都没搜到）——
                # 非流式响应也要带上，前端据此挂「内容来自网络」的横幅
                set_task_result(session_id, "web_only", bool((final_state or {}).get("web_only")))

                # ① **图主动中断**：认不出产品、在等用户确认。这既不是「完成」也不是「暂停」，
                #    状态留在检查点里，等 /resume 接着跑。
                #    必须单独判这一条 —— 掉进下面的「完成」分支会推一条 completed，
                #    泳道就谎报「已完成」，前端也不会去弹卡片。
                interrupts = (final_state or {}).get("__interrupt__")
                if interrupts:
                    value = (interrupts[0].value if interrupts else {}) or {}
                    push_to_session(session_id, SSEEvent.CONFIRM, {
                        "question": value.get("question", ""),
                        "options": value.get("options") or [],
                        "allow_custom": value.get("allow_custom", True),
                        # 真实进度：这一轮还没跑完，「生成答案」不该被点亮；
                        # 降级过的节点也要标出来（返回空 ≠ 正常取到）
                        "done_list": get_done_task_list(session_id),
                        "degraded_list": get_degraded_task_list(session_id),
                    })
                    # 非流式路由靠这两个结果拼响应
                    set_task_result(session_id, "need_confirm", True)
                    set_task_result(session_id, "clarify", value)
                    # 不推 progress：前端收到 confirm 就去弹卡片了，这条进度只会搅乱
                    update_task_status(session_id, TASK_STATUS_WAITING_USER, False)
                    logger.info(
                        f"[{NODE_NAME}] [{function_name}] 图在等用户确认产品，"
                        f"候选 {len(value.get('options') or [])} 个，session={session_id}"
                    )
                else:
                    # ② 被用户主动暂停：生成已作废，本轮就此结束（**不反问用户**）。
                    #    推 paused 事件告诉前端「这是被暂停、不是跑完了」——这一轮没有 final，
                    #    前端得靠它把光标、消耗条、泳道、标题一并收尾。
                    cancelled = bool((final_state or {}).get("cancelled"))
                    if cancelled:
                        push_to_session(session_id, SSEEvent.PAUSED, {
                            "done_list": get_done_task_list(session_id),
                            "degraded_list": get_degraded_task_list(session_id),
                        })
                        # 不推 progress —— 前端收到 paused 就会关掉 SSE 连接，
                        # 再推只会刷「No queue found」的告警噪音
                        update_task_status(session_id, TASK_STATUS_PAUSED, False)
                        logger.info(f"[{NODE_NAME}] [{function_name}] 本轮被用户暂停，session={session_id}")
                    else:
                        # ③ 正常跑完。push_queue=is_stream：只有流式模式才推进度，
                        #    避免无连接时产生告警噪音
                        update_task_status(session_id, TASK_STATUS_COMPLETED, is_stream)
                        logger.info(f"[{NODE_NAME}] [{function_name}] 检索图执行完成，session={session_id}")
            except BudgetExceeded as e:
                # 超出预算 = **主动中止**，不是故障：不打 ERROR 堆栈、不触发检查点降级、
                # 也不该混进「降级」统计里。文案要能直接给用户看
                msg = f"本次问答已中止（{e}）"
                logger.warning(f"[{NODE_NAME}] [{function_name}] {msg}")
                set_task_result(session_id, "run_error", msg)
                update_task_status(session_id, TASK_STATUS_FAILED, is_stream)
                if is_stream:
                    push_to_session(session_id, SSEEvent.ERROR, {"error": msg})
            except Exception as e:
                logger.error(f"[{NODE_NAME}] [{function_name}] 检索图执行失败：{e}", exc_info=True)
                # 若失败源于检查点写不进去（Mongo 中途挂了），让**下一次**运行改用内存 saver，
                # 免得每一轮都撞同一堵墙直到进程重启
                note_checkpointer_failure(e)
                update_task_status(session_id, TASK_STATUS_FAILED, is_stream)
                if is_stream:
                    push_to_session(session_id, SSEEvent.ERROR, {"error": str(e)})
    finally:
        # 本轮结束：撤销登记并丢弃暂停标志。带上 run_id，避免把下一轮刚登记的抹掉
        clear_active_run(session_id, run_id)

    # 记账汇总放在 with 之外：退出上下文只是清掉归因，累计器还能读
    summary = acc.summary()
    logger.info(f"[{NODE_NAME}] [{function_name}] 本次问答记账：{acc.text()}")
    return summary


class LoginRequest(BaseModel):
    """登录请求：提交访问密钥"""
    key: str


@app.on_event("startup")
async def on_startup():
    """
    服务启动时预热用户表

    顺带检查有没有用户——一个都没有时所有数据接口都会 401，提前把话说明白。
    """
    from app.clients.mongo_user_utils import count_users, get_user_tool
    get_user_tool()
    logger.info("用户工具已就绪（MongoDB）")
    if count_users() == 0:
        logger.warning(
            "还没有任何用户，数据接口将全部返回 401。"
            "先建一个：.venv/Scripts/python.exe -m app.clients.mongo_user_utils add <称呼>"
        )


@app.post("/login", summary="登录：校验访问密钥并写入会话 Cookie")
async def login(payload: LoginRequest, response: Response):
    """
    用访问密钥换一个 HttpOnly Cookie

    走 Cookie 而不是让前端存密钥发请求头：本服务的流式接口用 `EventSource`，
    它**无法自定义请求头**，只有 Cookie 能被浏览器自动携带。
    """
    function_name = sys._getframe().f_code.co_name
    from app.clients.mongo_user_utils import verify_key

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
    # 本文件位于 app/query_process/api/，页面在 app/query_process/page/
    page_path = Path(__file__).absolute().parent.parent / "page" / "chat.html"
    if not page_path.exists():
        logger.error(f"聊天页面不存在：{page_path}")
        raise HTTPException(status_code=404, detail=f"没有查询到页面，地址为：{page_path}！")
    return FileResponse(page_path, media_type="text/html")


@app.post("/query", summary="提交查询")
async def query(background_tasks: BackgroundTasks, request: QueryRequest,
                user: dict = Depends(current_tenant)):
    """
    接收用户提问并启动后台检索流程

    流式模式：建 SSE 队列 → 后台跑图 → 立即返回 session_id（前端随即订阅 /stream）
    非流式模式：同步跑完图后直接返回答案

    鉴权用参数形式而非 `dependencies=[...]`：需要拿到 tenant_id 才能把这次问答的
    成本记到调用方名下。
    """
    function_name = sys._getframe().f_code.co_name
    user_query = request.query
    session_id = request.session_id or str(uuid.uuid4())
    is_stream = request.is_stream
    tenant_id = user.get("tenant_id")
    # 本轮标识：与日志 trace 共用一个 id，并作为「暂停令牌」下发给前端
    # （前端点暂停时原样带回，见 POST /query/{session_id}/stop）
    run_id = new_trace_id()

    logger.info(f"[{NODE_NAME}] [{function_name}] 收到查询，session={session_id}，流式={is_stream}，问题={user_query}")

    if is_stream:
        # 建队列必须在跑图之前：否则节点推送时队列还不存在，事件会丢失
        create_sse_queue(session_id)
        update_task_status(session_id, TASK_STATUS_PROCESSING, is_stream)

        background_tasks.add_task(run_query_graph, session_id, user_query, is_stream,
                                  tenant_id, run_id,
                                  enable_web_search=request.enable_web_search)
        return {
            "message": "结果正在处理中...",
            "session_id": session_id,
            "run_id": run_id,
        }

    # 非流式：同步执行，直接返回答案
    update_task_status(session_id, TASK_STATUS_PROCESSING, is_stream)
    usage = run_query_graph(session_id, user_query, is_stream, tenant_id, run_id,
                            enable_web_search=request.enable_web_search)
    answer = get_task_result(session_id, "answer", "")
    images = get_task_result(session_id, "images", [])
    # 认不出产品时这一轮会**挂起**（图主动中断），要告诉前端去弹卡片，
    # 用户选完再走 /query/{session_id}/resume 接着跑
    need_confirm = get_task_result(session_id, "need_confirm", False)
    clarify = get_task_result(session_id, "clarify", {})
    # 超预算中止那类「主动结束」的说明；正常跑完时是空串
    run_error = get_task_result(session_id, "run_error", "")
    done_list = get_done_task_list(session_id)
    # 降级过的节点（返回空 ≠ 正常取到），让非流式的前端也能标出来
    degraded_list = get_degraded_task_list(session_id)
    # 本轮是不是「只有联网结果」（向量库一条都没搜到）—— 前端据此挂「内容来自网络」横幅
    web_only = bool(get_task_result(session_id, "web_only", False))
    clear_task(session_id)
    return {
        "message": "需要确认产品" if need_confirm else ("已中止" if run_error else "处理完成！"),
        "session_id": session_id,
        "run_id": run_id,
        "need_confirm": need_confirm,
        "clarify": clarify,
        "error": run_error,
        "answer": answer,
        "images": images,
        "web_only": web_only,
        "done_list": done_list,
        "degraded_list": degraded_list,
        # 本次问答花了多少：调用次数 / tokens / 估算成本（明细见 llm_usage 集合）
        "usage": usage,
    }


class StopRequest(BaseModel):
    """暂停请求：带上本轮 run_id，只认当前在跑的那一轮"""
    run_id: str = Field(..., description="POST /query 返回的 run_id")


@app.post("/query/{session_id}/stop", summary="暂停当前这一轮生成")
async def stop_query(session_id: str, payload: StopRequest,
                     user: dict = Depends(current_tenant)):
    """
    请求暂停该会话当前正在跑的这一轮

    置一个进程级标志即可，不直接杀线程 —— 生成节点在流式循环里每收一块查一次
    （`node_answer_output._generate`），置位就跳出循环、把本轮标记为作废，流程照常走到 END。
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


class ResumeRequest(BaseModel):
    """恢复请求：用户确认产品后接着跑"""
    run_id: str = Field(..., description="POST /query 返回的 run_id（也就是那一轮的 thread_id）")
    choice: str = Field(..., description="用户选中的产品名，或自己填的型号")
    is_stream: bool = Field(False, description="是否流式返回，要与前端订阅方式一致")


@app.post("/query/{session_id}/resume", summary="恢复被中断的查询")
async def resume_query(background_tasks: BackgroundTasks, session_id: str,
                       payload: ResumeRequest, user: dict = Depends(current_tenant)):
    """
    用户确认了产品之后，从断点继续跑

    图因认不出产品而**主动中断**，状态留在检查点里；这里带着用户的选择把它恢复。

    **恢复前必须校验**（三件事缺一不可）：该 thread 还在等（`next` 里有 node_ask_user）、
    确实带着中断、且它属于**路径里这个会话** —— 少了最后一条，凭一个 run_id 就能跨会话
    恢复别人的上下文。校验不过就 409，而不是让 LangGraph 拿空 state 起跑
    （那样只会抛一个莫名其妙的 KeyError）。
    """
    function_name = sys._getframe().f_code.co_name
    run_id = payload.run_id
    cfg = graph_config(run_id)

    try:
        snapshot = get_query_app().get_state(cfg)
    except Exception as e:
        snapshot = None
        logger.warning(f"[{NODE_NAME}] [{function_name}] 读取检查点失败：{e}")

    waiting = bool(snapshot) and "node_ask_user" in (snapshot.next or ())
    has_interrupt = bool(snapshot) and any(t.interrupts for t in (snapshot.tasks or []))
    same_session = bool(snapshot) and (snapshot.values or {}).get("session_id") == session_id
    if not (waiting and has_interrupt and same_session):
        logger.info(
            f"[{NODE_NAME}] [{function_name}] 拒绝恢复：waiting={waiting}，"
            f"interrupt={has_interrupt}，same_session={same_session}，run={run_id}"
        )
        raise HTTPException(status_code=409, detail="这一轮已经不在等待确认了，请重新提问。")

    tenant_id = user.get("tenant_id")
    logger.info(
        f"[{NODE_NAME}] [{function_name}] 恢复被中断的查询，session={session_id}，"
        f"run={run_id}，选择={payload.choice!r}"
    )

    if payload.is_stream:
        # 队列**存在就复用**：挂起时前端那条 SSE 还开着；这里若覆盖写，
        # 旧生成器会一直读那个被换掉的孤儿队列，恢复之后的事件就全丢了
        if get_sse_queue(session_id) is None:
            create_sse_queue(session_id)
        update_task_status(session_id, TASK_STATUS_PROCESSING, True)
        background_tasks.add_task(
            run_query_graph, session_id, "", True, tenant_id, run_id,
            {"choice": payload.choice},
        )
        return {"message": "正在恢复…", "session_id": session_id, "run_id": run_id}

    # 非流式：同步跑完直接给答案
    update_task_status(session_id, TASK_STATUS_PROCESSING, False)
    usage = run_query_graph(session_id, "", False, tenant_id, run_id,
                            {"choice": payload.choice})
    answer = get_task_result(session_id, "answer", "")
    images = get_task_result(session_id, "images", [])
    done_list = get_done_task_list(session_id)
    degraded_list = get_degraded_task_list(session_id)
    clear_task(session_id)
    return {
        "message": "处理完成！",
        "session_id": session_id,
        "run_id": run_id,
        "answer": answer,
        "images": images,
        "done_list": done_list,
        "degraded_list": degraded_list,
        "usage": usage,
    }


@app.get("/stream/{session_id}", summary="SSE 流式获取结果", dependencies=[Depends(current_tenant)])
async def stream(session_id: str, request: Request):
    """
    建立 SSE 长连接，实时推送任务进度与生成文本

    推送的事件类型：
    - ready    ：连接建立
    - progress ：节点进度（status / done_list / running_list）
    - delta    ：答案的增量字符（打字机效果）
    - paused   ：本轮被用户主动暂停（生成作废，附真实进度）
    - confirm  ：图主动中断、在等用户确认产品（附卡片内容与选项）
    - final    ：完整答案与状态
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
        "ts": r.get("ts"),
    } for r in rows]

    logger.info(f"[{NODE_NAME}] [{function_name}] 会话{session_id}历史查询，返回{len(items)}条")
    return {"session_id": session_id, "items": items}


@app.delete("/history/{session_id}", summary="清空会话历史", dependencies=[Depends(current_tenant)])
async def clear_session_history(session_id: str):
    """删除指定会话的全部历史对话记录"""
    function_name = sys._getframe().f_code.co_name
    try:
        deleted = clear_history(session_id)
    except Exception as e:
        logger.error(f"[{NODE_NAME}] [{function_name}] 清空历史失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"清空历史失败：{e}")

    logger.info(f"[{NODE_NAME}] [{function_name}] 会话{session_id}历史已清空，删除{deleted}条")
    return {"message": "History cleared", "session_id": session_id, "deleted_count": deleted}


if __name__ == "__main__":
    logger.info(f"[{NODE_NAME}] 查询服务启动中（端口{SERVICE_PORT}）...")
    uvicorn.run(app=app, host="127.0.0.1", port=SERVICE_PORT)
