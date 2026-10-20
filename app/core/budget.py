"""
整轮预算（wall-clock / token）

「一整轮最多跑多久、最多花多少」—— 超了就让本轮**明确失败**，而不是无限跑下去。

**只给查询图用**：导入本来就要跑几分钟、花掉很多 token，所以导入侧（`run_graph_task`）
根本不注入这两个值；没注入 = 不检查。见 `query_service.run_query_graph` 与
`file_import_service.run_graph_task` 的对比。

**检查点只能放在「节点开始前」**：LangGraph 的**同步图拦不到节点内部**
（框架的 `add_node(timeout=...)` 明确不支持同步节点，实测报
`Node timeouts are only supported for async nodes`），所以预算最坏会被超出「一个节点」的量。
真正「一次调用别等太久」由各客户端自己的超时兜住 —— 见 `app/conf/budget_config.py`。
"""
import time

from app.core.request_context import raw_context

# 上下文里的两个键名：注入方（usage_context）与读取方都从这里取，别各自写字符串
WALL_CLOCK_KEY = "wall_clock_budget"
TOKEN_KEY = "token_budget"


class BudgetExceeded(Exception):
    """本轮超出预算 —— 这是**主动中止**，不是故障，别按降级处理"""


def check_budget() -> None:
    """
    看一眼本轮还剩多少额度；超了就抛 `BudgetExceeded`

    由 `usage_tracker.tracked_node` 在每个节点**开始前**调用 —— 两张图的所有节点都过那一层，
    所以一处生效、全覆盖。

    :raises BudgetExceeded: 超出 wall-clock 或 token 预算
    """
    ctx = raw_context()
    wall_clock = ctx.get(WALL_CLOCK_KEY)
    token_budget = ctx.get(TOKEN_KEY)
    if wall_clock is None and token_budget is None:
        return                      # 没注入预算（导入侧就是这样）→ 不检查

    acc = ctx.get("acc")
    if acc is None:
        return                      # 没有累计器就无从判断

    # 注意两点：判的是 `is not None`（给了值就一律执行，包括 0 —— 把 0 当「不限制」会让
    # 一个误配静默失效，比直接失败更危险）；用的是 `>=`（预算的语义是**达到即止**，
    # 也让 0 真正等于「在第一个节点前就中止」）。想放宽就把数值调大。
    if wall_clock is not None:
        elapsed = time.time() - (acc.started_at or time.time())
        if elapsed >= float(wall_clock):
            raise BudgetExceeded(
                f"本轮已耗时 {elapsed:.0f} 秒，达到预算上限 {float(wall_clock):.0f} 秒"
            )

    if token_budget is not None:
        used = acc.summary().get("total_tokens", 0)
        if used >= float(token_budget):
            raise BudgetExceeded(f"本轮已用 {used} tokens，达到预算上限 {float(token_budget):.0f}")


def remaining_wall_clock() -> float | None:
    """
    本轮还剩多少 wall-clock 预算（秒）；没注入预算时返回 None（= 不限制）

    给「开始新一轮检索前」这种主动决策用：预算不够就别再开一轮，
    否则下一轮节点开始时的 `check_budget()` 会把整轮打成 `BudgetExceeded` ——
    本来能答的题会因为一次重试变成报错。
    """
    ctx = raw_context()
    wall_clock = ctx.get(WALL_CLOCK_KEY)
    acc = ctx.get("acc")
    if wall_clock is None or acc is None:
        return None
    # 子图（RAG subagent）会嵌套一层 usage_context、换一个新的 acc，started_at 变成
    # 子调用的开始时刻。整轮预算必须从**本轮入口**算 —— 入口把它写进 turn_started_at，
    # 子图里合并上下文时会保留（bind_context 是合并语义）
    started_at = ctx.get("turn_started_at") or acc.started_at or time.time()
    return float(wall_clock) - (time.time() - started_at)


def _check_remaining_wall_clock() -> list:
    """
    离线自测：剩余预算从**整轮起点**算，不被子图嵌套的 acc 重置（纯逻辑）

    检索循环的重试判断依赖它：子图（RAG subagent）会换一个新的 `acc`，
    它的 `started_at` 是子调用开始时刻；若这里误用 acc.started_at，
    一次已经跑了 100 秒的问答会被当成「刚刚开始」，预算保护形同虚设。

    :return: 问题描述列表，空表示通过
    """
    from app.core.request_context import bind_context, reset_context

    class _Acc:
        started_at = time.time() - 10       # 子调用「刚开始 10 秒」

    problems = []
    token = bind_context(wall_clock_budget=180,
                         turn_started_at=time.time() - 100,   # 整轮已跑 100 秒
                         acc=_Acc())
    try:
        remaining = remaining_wall_clock()
        if remaining is None:
            problems.append("注入了预算却返回 None")
        elif not (70 <= remaining <= 90):
            problems.append(f"剩余预算应从整轮起点算（约 80 秒），实得 {remaining:.1f}")
    finally:
        reset_context(token)
    return problems
