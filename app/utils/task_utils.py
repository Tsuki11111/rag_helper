from typing import Dict, List
from .sse_utils import push_to_session

# ---------------------------
# 内存态任务追踪（单进程）
# ---------------------------
# key: task_id
# value: 节点名列表（原始英文/节点ID）
_tasks_running_list: Dict[str, List[str]] = {}
_tasks_done_list: Dict[str, List[str]] = {}

# key: task_id
# value: status 字符串（如 pending/processing/completed/failed）
_tasks_status: Dict[str, str] = {}

# key: task_id
# value: 任务结果（例如 query 的 answer）
_tasks_result: Dict[str, Dict[str, str]] = {}

# key: task_id
# value: **降级过**的节点名列表（原始英文/节点ID）
# 由 error_policy 的两条降级路径写入。为什么要单独记：节点降级时返回空结果、**照常算完成**，
# 于是在泳道上跟「正常取到内容」长得一模一样 —— 「停了 Neo4j 却还能看到查询知识图谱 ✓」
# 就是这么来的。记下来，前端才能把那几个节点标出来。
_tasks_degraded_list: Dict[str, List[str]] = {}

# ---------------------------
# 运行标识与取消标志（单进程）
# ---------------------------
# 供「用户主动暂停」使用。
# 为什么用进程级 dict 而不是 ContextVar：归因上下文（request_context）在 LangGraph
# copy_context 到子线程后，子线程内的修改不会回流父上下文；何况暂停信号来自另一个
# HTTP 请求（/stop），隔着请求更读不到。
#
# key: session_id -> 该会话当前正在跑的 run_id（一轮问答一个）
_active_run: Dict[str, str] = {}
# 已被请求停止的 run_id 集合
_stop_runs: set = set()

TASK_STATUS_PENDING = "pending"
TASK_STATUS_PROCESSING = "processing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
# 本轮被用户主动暂停（生成作废、已进入询问，本轮结束）
TASK_STATUS_PAUSED = "paused"
# 本轮挂起、在等用户确认产品（图主动中断，状态留在检查点里）
TASK_STATUS_WAITING_USER = "waiting_user"

# 节点名 -> 中文名映射（用于前端展示）
# 说明：这里的 key 应与 LangGraph 的 add_node("xxx", ...) 中的节点名一致。
_NODE_NAME_TO_CN: Dict[str, str] = {
    "upload_file": "开始上传文件",
    "node_entry": "检查文件",
    "node_pdf_to_md": "PDF转Markdown",
    "node_md_img": "Markdown图片处理",
    "node_item_name_recognition": "主体名称识别",
    "node_document_split": "文档切分",
    "node_bge_embedding": "向量生成",
    "node_dashscope_embedding": "向量生成",
    "node_import_kg": "导入知识图谱",
    "node_import_milvus": "导入向量库",
    "__end__": "处理完成",
    "END": "处理完成",
    # --- Query 流程节点（kb/query_process/main_graph.py）---
    "node_item_name_confirm": "确认问题产品",
    "node_answer_output": "生成答案",
    "node_rerank": "重排序",
    "node_rrf": "倒排融合",
    "node_web_search_mcp": "网络搜索",
    "node_search_embedding": "切片搜索",
    "node_search_embedding_hyde": "切片搜索(假设性文档)",
    "node_multi_search": "多路搜索",
    "node_query_kg": "查询知识图谱",
    "node_join": "多路搜索合并",
    "node_ask_user": "等待用户确认",
}


def _ensure_task(task_id: str) -> None:
    """确保 task_id 对应的数据结构已初始化。"""
    if task_id not in _tasks_running_list:
        _tasks_running_list[task_id] = []
    if task_id not in _tasks_done_list:
        _tasks_done_list[task_id] = []
    if task_id not in _tasks_result:
        _tasks_result[task_id] = {}
    if task_id not in _tasks_degraded_list:
        _tasks_degraded_list[task_id] = []


def _to_cn(node_name: str) -> str:
    """将节点名转换为中文展示名；若无映射则返回原名。"""
    return _NODE_NAME_TO_CN.get(node_name, node_name)


def add_running_task(task_id: str, node_name: str, is_stream: bool = False) -> None:
    """
    添加“正在运行”的节点任务。

    参数：
    - task_id: 任务ID
    - node_name: 节点名称(节点ID)
    """
    _ensure_task(task_id)
    running = _tasks_running_list[task_id]
    # 避免重复追加
    if node_name not in running:
        running.append(node_name)

    if is_stream:
        task_push_queue(task_id)


def add_done_task(task_id: str, node_name: str, is_stream: bool = False) -> None:
    """
    添加“已完成”的节点任务。

    注意：添加已完成任务时，会把同名的“正在运行”任务删除。

    参数：
    - task_id: 任务ID
    - node_name: 节点名称(节点ID)
    """
    _ensure_task(task_id)

    # 1) 从 running 中移除同名节点（可能出现重复，移除所有）
    running = _tasks_running_list[task_id]
    _tasks_running_list[task_id] = [n for n in running if n != node_name]

    # 2) 追加到 done（保持完成顺序），避免重复
    done = _tasks_done_list[task_id]
    if node_name not in done:
        done.append(node_name)

    if is_stream:
        task_push_queue(task_id)


def set_task_result(task_id: str, key: str, value: str) -> None:
    """
    存储任务结果字段（如 answer / error）。
    """
    _ensure_task(task_id)
    _tasks_result[task_id][key] = value


def get_task_result(task_id: str, key: str, default: str = "") -> str:
    """
    获取任务结果字段（如 answer / error）。
    """
    _ensure_task(task_id)
    return _tasks_result.get(task_id, {}).get(key, default)


def get_task_status(task_id: str) -> str:
    """
    获取当前任务状态。

    参数：
    - task_id: 任务ID

    返回：
    - str: 状态名称；如果未设置过则返回空字符串
    """
    return _tasks_status.get(task_id, "")


def get_done_task_list(task_id: str) -> List[str]:
    """
    获取已完成节点列表（中文展示）。


    """
    _ensure_task(task_id)
    done = _tasks_done_list.get(task_id, [])
    return [_to_cn(n) for n in done]


def get_running_task_list(task_id: str) -> List[str]:
    """
    获取正在运行节点列表（中文展示）。

    """
    _ensure_task(task_id)
    running = _tasks_running_list.get(task_id, [])
    return [_to_cn(n) for n in running]


def add_degraded_task(task_id: str, node_name: str) -> None:
    """
    记一个「降级过」的节点（同一个节点只记一次）

    由 `error_policy` 的两条降级路径调用（异常降级 / 前置检查降级）。
    这里**不推送**事件：让节点结束时那次进度推送自然把它带上，少一条噪音。
    """
    if not task_id or not node_name:
        return          # 没有任务上下文（命令行单跑节点）就不记
    _ensure_task(task_id)
    degraded = _tasks_degraded_list[task_id]
    if node_name not in degraded:
        degraded.append(node_name)


def get_degraded_task_list(task_id: str) -> List[str]:
    """获取降级过的节点列表（中文展示）"""
    _ensure_task(task_id)
    return [_to_cn(n) for n in _tasks_degraded_list.get(task_id, [])]


def reset_task_progress(task_id: str) -> None:
    """
    重置**本轮**的进度记录（done / running / degraded）与结果字段

    **为什么必须清**：这些记录是按 `session_id` 存的，而一个会话有很多轮。不清的话上一轮的
    记录会带到下一轮 —— 实测踩过两次：
    - 上一轮 Neo4j 停着、图谱那一路降级；这一轮 Neo4j 恢复了，泳道**仍然**把它标成降级
    - 上一轮跑全了 8 个节点、这一轮短路只跑了 2 个，泳道会把 8 个都点亮

    在**新开一轮**时调（`run_query_graph` 里 `resume is None` 时）；
    **续跑不要调** —— 恢复的那一段要接着前一段累积。
    """
    _ensure_task(task_id)
    _tasks_running_list[task_id] = []
    _tasks_done_list[task_id] = []
    _tasks_degraded_list[task_id] = []
    # 结果字段也要清：上一轮留下的 need_confirm / run_error 会让这一轮的非流式响应
    # 报出过期信息（比如明明正常跑完了却说「需要确认产品」）
    for key in ("answer", "images", "need_confirm", "clarify", "run_error"):
        _tasks_result[task_id].pop(key, None)


def update_task_status(task_id: str, status_name: str, push_queue: bool = False) -> None:
    """
    更新任务状态。

    参数：
    - task_id: 任务ID
    - status_name: 状态名称（字符串）
    """
    _tasks_status[task_id] = status_name
    if push_queue:
        task_push_queue(task_id)


def task_push_queue(task_id: str):
    push_to_session(task_id, "progress", {
        "status": get_task_status(task_id),
        "done_list": get_done_task_list(task_id),
        "running_list": get_running_task_list(task_id),
        # 降级过的节点也推给前端 —— 否则「返回空」和「正常取到」在泳道上分不出来
        "degraded_list": get_degraded_task_list(task_id),
    })


# ---------------------------
# 轮次标识与「用户主动暂停」
# ---------------------------
def set_active_run(session_id: str, run_id: str) -> None:
    """登记该会话当前正在跑的轮次（run_id 同时用作日志 trace 与暂停令牌）"""
    _active_run[session_id] = run_id


def clear_active_run(session_id: str, run_id: str = None) -> None:
    """
    一轮结束时清理登记。

    :param run_id: 传了的话，只在该轮仍是当前登记时才清 —— 防止上一轮迟到的收尾
        把下一轮刚登记好的 run_id 抹掉
    """
    if run_id is not None and _active_run.get(session_id) != run_id:
        return
    _active_run.pop(session_id, None)
    if run_id:
        _stop_runs.discard(run_id)


def request_stop(session_id: str, run_id: str) -> bool:
    """
    请求暂停当前这一轮。

    **必须带上 run_id 且与当前登记一致才生效**：前端的 session_id 是跨轮复用的，
    用户点慢了、或网络延迟导致上一轮的暂停信号在下一轮开跑之后才打到，
    按 session 置位就会误杀新一轮。

    :return: 是否真的置位；False 表示这一轮已经结束了，信号作废
    """
    if _active_run.get(session_id) != run_id:
        return False
    _stop_runs.add(run_id)
    return True


def is_stop_requested(session_id: str) -> bool:
    """
    本轮是否被请求暂停。

    节点只按 session_id 问，不必知道 run_id —— 登记表里查得到当前轮次。
    从未登记过的会话（例如单节点自测直接调用节点函数）一律返回 False。
    """
    run_id = _active_run.get(session_id)
    return bool(run_id) and run_id in _stop_runs


def clear_task(task_id: str):
    _tasks_running_list.pop(task_id, None)
    _tasks_done_list.pop(task_id, None)
    _tasks_status.pop(task_id, None)
    _tasks_result.pop(task_id, None)
    _tasks_degraded_list.pop(task_id, None)
    # 轮次登记与取消标志一并清掉，免得标志常驻
    run_id = _active_run.pop(task_id, None)
    if run_id:
        _stop_runs.discard(run_id)