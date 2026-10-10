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
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

from app.clients.mongo_history_utils import clear_history, get_recent_messages, list_sessions
from app.clients.mongo_run_utils import get_query_run_tool, save_query_run
from app.conf.budget_config import budget_config
from app.core.budget import BudgetExceeded
from app.core.error_policy import CONTENT_REJECTED_ANSWER
from app.core.input_guard import INPUT_GUARD_ANSWER
from app.core.logger import logger
from app.core.request_context import new_trace_id
from app.core.usage_tracker import usage_context
from app.query_process.agent.main_graph import get_query_app
from app.query_process.agent.nodes.node_answer_output import FALLBACK_ANSWER
from app.clients.mongo_checkpoint_utils import (
    KIND_MONGO,
    get_checkpointer,
    graph_config,
    note_checkpointer_failure,
)
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


# ── 本轮结局（运行记录 query_runs 的核心字段）──
# `waiting_user` 不在 evaluation-plan 最初列的五个值里：图因缺信息中断、等用户确认时，
# **这一段就是到此为止**（用户不选就挂到检查点 TTL 到期），归进另外五个里任何一个都是说谎。
OUTCOME_ANSWERED = "answered"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_REJECTED = "rejected"
OUTCOME_BLOCKED = "blocked"
OUTCOME_PAUSED = "paused"
OUTCOME_WAITING_USER = "waiting_user"
OUTCOME_ERROR = "error"


def judge_outcome(state: dict) -> str:
    """
    从图的最终状态判断**这一段**的结局（纯函数，离线可测）

    判定顺序有意义：先看「有没有中断 / 被暂停」，再按答案的形态分类。依据都核实过：
    - 审核拒绝时 `node_item_name_confirm` 把 `CONTENT_REJECTED_ANSWER` 直接写进 answer
    - 输入护栏命中时同一个节点把 `INPUT_GUARD_ANSWER` 写进 answer（第三层护栏）
    - 一条参考都没有时 `node_answer_output` 用 `FALLBACK_ANSWER` 兜底，且**不调模型**
    - 认不出产品那条路**刻意不写 answer**（交给图去中断），所以「有答案但没参考」
      只可能是上面几种
    """
    state = state or {}
    if state.get("__interrupt__"):
        return OUTCOME_WAITING_USER
    if state.get("cancelled"):
        return OUTCOME_PAUSED
    answer = (state.get("answer") or "").strip()
    if answer == CONTENT_REJECTED_ANSWER:
        return OUTCOME_REJECTED
    # 输入护栏拦下的那类：答案同样是预设文本、参考切片也是空的 —— 不先判它就会被记成 no_match，
    # 而「有人往里打注入」和「库里没这个内容」是两件事，混在一起就没法统计了
    if answer == INPUT_GUARD_ANSWER:
        return OUTCOME_BLOCKED
    if not (state.get("reranked_docs") or []):
        return OUTCOME_NO_MATCH
    if not answer:
        # 有参考内容却没答案：生成那一环降级成了空答案（degraded_list 里能看到它）
        return OUTCOME_ERROR
    return OUTCOME_ANSWERED


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

    # 本段结局与错误文案：四条收尾分支各自赋值（默认按出错处理，防漏赋值时谎报成功），
    # 最后连同用量一起落进 `query_runs`（评测的输入，见 README「评估路线」P0）
    final_state = None
    outcome = OUTCOME_ERROR
    error_msg = ""

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
                    outcome = OUTCOME_WAITING_USER
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
                        outcome = OUTCOME_PAUSED
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
                        outcome = judge_outcome(final_state)
                        update_task_status(session_id, TASK_STATUS_COMPLETED, is_stream)
                        logger.info(f"[{NODE_NAME}] [{function_name}] 检索图执行完成，session={session_id}")
            except BudgetExceeded as e:
                # 超出预算 = **主动中止**，不是故障：不打 ERROR 堆栈、不触发检查点降级、
                # 也不该混进「降级」统计里。文案要能直接给用户看
                msg = f"本次问答已中止（{e}）"
                error_msg = msg
                logger.warning(f"[{NODE_NAME}] [{function_name}] {msg}")
                set_task_result(session_id, "run_error", msg)
                update_task_status(session_id, TASK_STATUS_FAILED, is_stream)
                if is_stream:
                    push_to_session(session_id, SSEEvent.ERROR, {"error": msg})
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
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

    # 运行记录落库：一轮问答一条（确认中断那轮两条，`segment` 区分），评测的输入。
    # 写在最后、且 save_query_run 自己吞异常 —— 记录写不进去绝不能影响用户拿到答案。
    # done/degraded 在这里读是安全的：非流式那两条路由要等本函数返回后才 clear_task。
    state = final_state or {}
    topk = state.get("reranked_docs") or []
    save_query_run({
        "trace_id": run_id,
        "segment": "resume" if resume is not None else "start",
        "session_id": session_id,
        "tenant_id": tenant_id or "",
        # 恢复段的入参 question 是空串，但检查点里带着首段的 original_query，照样读得到
        "question": state.get("original_query") or user_query,
        "rewritten_query": state.get("rewritten_query") or "",
        "item_names": state.get("item_names") or [],
        "enable_web_search": enable_web_search,
        "is_stream": is_stream,
        "outcome": outcome,
        "error": error_msg,
        "answer_chars": len(state.get("answer") or ""),
        "images_count": len(state.get("images") or []),
        "web_only": bool(state.get("web_only")),
        # 来源构成：评测的「本地命中率」「来源构成」直接由这三个数算
        "topk_total": len(topk),
        "topk_local": sum(1 for d in topk if (d or {}).get("source") == "local"),
        "topk_web": sum(1 for d in topk if (d or {}).get("source") == "web"),
        # 本地切片的 chunk_id：以后标了 gold 就能直接算 Recall@K / MRR，不必重跑。
        # 一律转成字符串 —— 两路召回回来的 id 有 int 也有 str（见 HANDOFF §3.18），
        # 这里是要拿去跟标注做集合比对的，类型不统一就会「明明召回了却算没命中」
        "topk_chunk_ids": [str(d["chunk_id"]) for d in topk
                           if (d or {}).get("chunk_id") is not None],
        "done_list": get_done_task_list(session_id),
        "degraded_list": get_degraded_task_list(session_id),
        "usage": summary,
        "ts": time.time(),
    })
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


@app.get("/query/{session_id}/pending", summary="该会话是否有一轮在等用户确认",
         dependencies=[Depends(current_tenant)])
async def get_pending_confirm(session_id: str):
    """
    有没有一轮**挂在那里等确认**？有就把卡片内容（含 `run_id`）给它

    **为什么需要这个接口**：图中断时 `run_id` 与卡片只推给了前端，**刷新页面就没了** ——
    用户只能重新提问（老问题：卡在卡片上时刷新，那一轮捡不回来）。

    做法不新造存储：运行记录里有 `(session_id → trace_id)`，反查出来之后
    **再拿检查点校验「确实还在等」**（`next` 里有 `node_ask_user`、带着中断、会话对得上）。
    好处是**恢复过的、或早就跑完的那些自然查不出来**，不需要任何额外的清理逻辑。

    :return: 200 + `{run_id, question, options, allow_custom, done_list, degraded_list}`；没有则 404
    """
    function_name = sys._getframe().f_code.co_name
    candidates = list(get_query_run_tool().collection.find(
        {"session_id": session_id, "outcome": OUTCOME_WAITING_USER},
        {"trace_id": 1},
    ).sort("ts", -1).limit(5))       # -1 = 倒序，最近的在前

    for doc in candidates:
        run_id = doc.get("trace_id")
        if not run_id:
            continue
        try:
            snapshot = get_query_app().get_state(graph_config(run_id))
        except Exception as e:
            logger.warning(f"[{NODE_NAME}] [{function_name}] 读检查点失败（跳过）：{run_id}：{e}")
            continue
        waiting = bool(snapshot) and "node_ask_user" in (snapshot.next or ())
        has_interrupt = bool(snapshot) and any(t.interrupts for t in (snapshot.tasks or []))
        values = (snapshot.values if snapshot else {}) or {}
        if not (waiting and has_interrupt and values.get("session_id") == session_id):
            continue        # 恢复过了 / 不属于这个会话 / 已经跑完 —— 都不是「在等」

        clarify = values.get("clarify") or {}
        logger.info(f"[{NODE_NAME}] [{function_name}] 会话{session_id}有一轮在等确认，"
                    f"run={run_id}，候选{len(clarify.get('options') or [])}个")
        return {
            "run_id": run_id,
            "question": clarify.get("question", ""),
            "options": clarify.get("options") or [],
            "allow_custom": clarify.get("allow_custom", True),
            # 进度也一起给：泳道能把「确认问题产品」之前那几站标出来
            "done_list": get_done_task_list(session_id),
            "degraded_list": get_degraded_task_list(session_id),
        }

    raise HTTPException(status_code=404, detail="这个会话没有在等待确认的一轮")


def _check_pending_confirm() -> list:
    """
    离线自测：找回「正等确认」那一轮（要 Mongo；图打桩，不跑真图）

    守两条：
    1. **在等的那一轮能被找回来**（带 run_id 与卡片内容）
    2. **已经恢复过的不算**（检查点 `next` 空了）—— 这条最要紧：判不出来就会给用户
       一张早就作废的卡片，点下去得到 409

    :return: 问题描述列表，空表示通过
    """
    import asyncio

    problems = []
    sid = "selftest_pending"
    col = get_query_run_tool().collection
    col.delete_many({"session_id": sid})

    class _Task:
        def __init__(self, interrupts):
            self.interrupts = interrupts

    class _Snapshot:
        def __init__(self, next_, values, interrupts):
            self.next = next_
            self.values = values
            self.tasks = [_Task(interrupts)]

    class _FakeApp:
        """替身图：get_state 按 run_id 返回不同的检查点快照"""

        def __init__(self, by_run):
            self.by_run = by_run

        def get_state(self, cfg):
            return self.by_run.get(cfg["configurable"]["thread_id"])

    clarify = {"question": "你要问的是哪一个？",
               "options": [{"item_name": "自测产品", "file_title": "自测手册", "score": 0.7}],
               "allow_custom": True}
    by_run = {
        # 还在等：next 里有 node_ask_user、带着中断、会话对得上
        "selftest_pending_waiting": _Snapshot(("node_ask_user",), {"session_id": sid, "clarify": clarify}, [1]),
        # 已经恢复过：next 空了 —— 不该再被找出来
        "selftest_pending_done": _Snapshot((), {"session_id": sid, "clarify": clarify}, []),
        # 属于别的会话：也不该被找出来
        "selftest_pending_other": _Snapshot(("node_ask_user",),
                                            {"session_id": "别的会话", "clarify": clarify}, [1]),
    }
    real_get_app = globals()["get_query_app"]
    globals()["get_query_app"] = lambda: _FakeApp(by_run)
    try:
        # 顺序有讲究：先插「已恢复」的（更新），确认接口不会拿旧的把新的顶掉
        for trace_id, ts in (("selftest_pending_waiting", 1.0), ("selftest_pending_done", 2.0)):
            col.insert_one({"session_id": sid, "trace_id": trace_id,
                            "outcome": OUTCOME_WAITING_USER, "ts": time.time() + ts})

        result = asyncio.run(get_pending_confirm(sid))
        if result.get("run_id") != "selftest_pending_waiting":
            problems.append(f"没找回在等的那一轮：{result.get('run_id')!r}")
        if (result.get("options") or [{}])[0].get("item_name") != "自测产品":
            problems.append(f"卡片候选没带回来：{result.get('options')!r}")

        # 把在等的那条也标成「已恢复」→ 应当 404（不能给一张作废的卡片）
        by_run["selftest_pending_waiting"] = _Snapshot((), {"session_id": sid, "clarify": clarify}, [])
        try:
            asyncio.run(get_pending_confirm(sid))
            problems.append("已经恢复过的那一轮仍被当成「在等」")
        except HTTPException as e:
            if e.status_code != 404:
                problems.append(f"应当 404，实得 {e.status_code}")
    except Exception as e:
        problems.append(f"找回挂起轮次时抛异常：{type(e).__name__}: {e}")
    finally:
        globals()["get_query_app"] = real_get_app
        col.delete_many({"session_id": sid})
    return problems


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


def _check_outcome_mapping() -> list:
    """
    离线自测：状态 → 结局的映射（纯逻辑，不碰任何服务）

    六种结局各一例，另加两条优先序。这几档一旦判错，评测拿到的就是**错的标签**，
    而且不会有任何报错 —— 报表只会安静地把通过率算错。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    cases = [
        ("被中断等确认",
         {"__interrupt__": [object()], "answer": "", "reranked_docs": []},
         OUTCOME_WAITING_USER),
        ("用户暂停",
         {"cancelled": True, "answer": "", "reranked_docs": [{"source": "local"}]},
         OUTCOME_PAUSED),
        # 审核拒绝必须**先于**「没有参考内容」判：它的 answer 是预设的，
        # 参考切片也是空的，顺序反了就会被误记成 no_match
        ("审核拒绝",
         {"answer": CONTENT_REJECTED_ANSWER, "reranked_docs": []},
         OUTCOME_REJECTED),
        # 输入护栏拦下的同理：不先判就会混进 no_match，而「有人打注入」与「库里没有」是两件事
        ("输入护栏拦下",
         {"answer": INPUT_GUARD_ANSWER, "reranked_docs": []},
         OUTCOME_BLOCKED),
        ("一条参考都没有",
         {"answer": FALLBACK_ANSWER, "reranked_docs": []},
         OUTCOME_NO_MATCH),
        ("生成降级成空答案",
         {"answer": "", "reranked_docs": [{"source": "local"}]},
         OUTCOME_ERROR),
        ("正常回答",
         {"answer": "这是一段答案", "reranked_docs": [{"source": "local"}]},
         OUTCOME_ANSWERED),
        # 只有联网结果也算「有参考、有答案」—— 判据是 reranked_docs 非空，
        # 不是本地切片非空，否则纯联网那一轮会被误判成 no_match
        ("纯联网作答",
         {"answer": "来自网络的答案", "reranked_docs": [{"source": "web"}]},
         OUTCOME_ANSWERED),
        ("中断压过暂停",
         {"__interrupt__": [object()], "cancelled": True, "answer": "",
          "reranked_docs": []},
         OUTCOME_WAITING_USER),
    ]
    for name, state, expect in cases:
        got = judge_outcome(state)
        if got != expect:
            problems.append(f"{name}：期望 {expect}，实得 {got}")

    # 空状态不能把服务炸掉（自测 / 命令行误用时可能传进来）
    try:
        judge_outcome(None)
    except Exception as e:
        problems.append(f"judge_outcome(None) 抛异常：{type(e).__name__}: {e}")
    return problems


def _check_run_recorded() -> list:
    """
    离线自测：真跑 `run_query_graph`，确认运行记录**确实落了库**（打桩图，不调模型）

    两段都验：① 正常跑完（结局 answered，来源构成与 chunk_id 归一）
    ② 恢复段又中断（结局 waiting_user —— question 只能从检查点读，入参是空串）

    这条守的是**调用点**，不是函数 —— §3.19 的教训：存储层的往返用例管不到
    「服务有没有真的把记录写出去」，那种缺口只有把这条路径真跑一遍才会暴露。

    :return: 问题描述列表，空表示通过
    """
    from app.clients.mongo_run_utils import get_query_run_tool

    problems = []
    session_id = "selftest_run_record_session"
    start_trace = "selftest_run_record_start"
    resume_trace = "selftest_run_record_resume"
    question = "自测问题：运行记录落库了吗？"

    class _FakeApp:
        """替身图：invoke 直接回一份最终状态，不碰模型、检查点与任何外部服务"""

        def __init__(self, state: dict):
            self._state = state

        def invoke(self, *_args, **_kwargs):
            return self._state

    class _FakeInterrupt:
        """替身中断：真代码只取 `.value`（LangGraph 的 Interrupt 对象就是这个形状）"""

        def __init__(self, value: dict):
            self.value = value

    answered_state = {
        "original_query": question,
        "rewritten_query": "自测改写后的问题",
        "item_names": ["自测产品"],
        "answer": "自测答案",
        "images": [{"url": "http://example.invalid/1.jpg", "caption": "自测图"}],
        "reranked_docs": [
            {"source": "local", "chunk_id": 111},
            {"source": "local", "chunk_id": "222"},   # 图谱那路回来的是字符串 id
            {"source": "web", "chunk_id": None},
        ],
        "web_only": False,
    }
    interrupted_state = {
        "original_query": question,
        "item_names": [],
        "__interrupt__": [_FakeInterrupt({
            "question": "你要问的是下面哪一个？",
            "options": [{"item_name": "自测产品", "file_title": "自测手册", "score": 0.7}],
            "allow_custom": True,
        })],
    }

    real_get_app = globals()["get_query_app"]
    # 这条用例只关心「记录有没有落库」，SSE 推送不是它的事：不打桩的话，中断分支会往
    # 不存在的队列推 confirm，打出一行 `[SSE] Warning` —— 它是 print 不是日志，
    # 连 `--verbose` 都拦不住，会漏进默认静音的结果表里
    real_push = globals()["push_to_session"]
    try:
        globals()["push_to_session"] = lambda *a, **k: None
        globals()["get_query_app"] = lambda: _FakeApp(answered_state)
        run_query_graph(session_id, question, is_stream=False,
                        tenant_id="t_selftest", run_id=start_trace)
        # 恢复段的 user_query 是空串：记录里的 question 必须从检查点的状态里读回来，
        # 否则这一轮的记录会说「问的是空问题」，评测按问题聚合时就对不上了
        globals()["get_query_app"] = lambda: _FakeApp(interrupted_state)
        run_query_graph(session_id, "", is_stream=False, tenant_id="t_selftest",
                        run_id=resume_trace, resume={"choice": "自测产品"})
    except Exception as e:
        problems.append(f"跑 run_query_graph 时抛异常：{type(e).__name__}: {e}")
    finally:
        globals()["get_query_app"] = real_get_app
        globals()["push_to_session"] = real_push
        clear_task(session_id)

    try:
        rows = list(get_query_run_tool().collection.find(
            {"trace_id": {"$in": [start_trace, resume_trace]}}))
        by_trace = {r.get("trace_id"): r for r in rows}
        if len(rows) != 2:
            problems.append(f"两段应各落 1 条运行记录，实到 {len(rows)} 条")

        start = by_trace.get(start_trace)
        if start is None:
            problems.append("正常跑完那一段没有记录")
        else:
            expect = {
                "segment": "start",
                "session_id": session_id,
                "tenant_id": "t_selftest",
                "question": question,
                "outcome": OUTCOME_ANSWERED,
                "error": "",
                "answer_chars": len("自测答案"),
                "images_count": 1,
                "topk_total": 3,
                "topk_local": 2,
                "topk_web": 1,
                # 两种类型的 chunk_id 都要归一成字符串，否则以后跟 gold 比集会漏掉字符串那条
                "topk_chunk_ids": ["111", "222"],
                "item_names": ["自测产品"],
            }
            for key, want in expect.items():
                got = start.get(key)
                if got != want:
                    problems.append(f"{key} 不对：期望 {want!r}，实得 {got!r}")
            usage = start.get("usage") or {}
            if "elapsed_ms" not in usage:
                problems.append(f"usage 不完整（缺 elapsed_ms）：{sorted(usage)[:8]}")
            if start.get("done_list") is None or start.get("degraded_list") is None:
                problems.append("done_list / degraded_list 没写进记录（应为列表，哪怕是空的）")

        resumed = by_trace.get(resume_trace)
        if resumed is None:
            problems.append("恢复那一段没有记录")
        else:
            expect2 = {
                "segment": "resume",
                "outcome": OUTCOME_WAITING_USER,
                "question": question,     # 入参是空串，只能来自检查点里的 original_query
                "answer_chars": 0,
                "topk_total": 0,
            }
            for key, want in expect2.items():
                got = resumed.get(key)
                if got != want:
                    problems.append(f"恢复段 {key} 不对：期望 {want!r}，实得 {got!r}")
    finally:
        get_query_run_tool().collection.delete_many(
            {"trace_id": {"$in": [start_trace, resume_trace]}})
    return problems


if __name__ == "__main__":
    logger.info(f"[{NODE_NAME}] 查询服务启动中（端口{SERVICE_PORT}）...")
    uvicorn.run(app=app, host="127.0.0.1", port=SERVICE_PORT)
