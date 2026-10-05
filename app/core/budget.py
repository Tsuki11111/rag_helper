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
