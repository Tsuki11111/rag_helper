"""
查询运行记录（MongoDB 集合 `query_runs`）

每次 `run_query_graph` 收尾时写一条 —— **一轮问答的结局**：结局分类、耗时、成本、
来源构成（本地/联网各几条）、进度与降级清单。这是**评测轨道的输入**
（README「评估路线」的 P0）：判据全在这条记录里，不必再去解析日志文本。

与账本（`llm_usage`）分工不同、粒度不同，别混：
- `llm_usage` 记**每一次模型调用**（一次问答十来条），回答「钱花在哪」
- 本集合记**一轮问答的结局**（一次一条，确认中断那轮两条），回答「这一轮好不好」
两者共用 `trace_id`，可以互相 join。

确认中断那一轮会落**两条**：首段 `segment=start`（结局 `waiting_user`）+
恢复段 `segment=resume`（最终结局）。与账本「一段一条汇总」的做法一致 ——
单独插入没有读-改-写，恢复失败时也不会把首段记录弄丢。

用法：
    .venv/Scripts/python.exe -m app.clients.mongo_run_utils        # 最近 1 天
    .venv/Scripts/python.exe -m app.clients.mongo_run_utils 7      # 最近 7 天
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List

from pymongo import DESCENDING, MongoClient

from app.core.logger import logger

# 集合名
COLLECTION_NAME = "query_runs"

# Mongo 选不到节点时的等待上限（毫秒）。理由同账本：本集合的写入在**问答收尾的
# 关键路径**上，库挂了不能让用户多等一轮连接超时（pymongo 默认 30 秒），
# 失败快一点、只丢这一条记录。
SERVER_SELECTION_TIMEOUT_MS = 2000

# 连接失败后的熔断时长（秒）：这段时间内不再尝试连接
CONNECT_FAILURE_COOLDOWN_SEC = 60.0

_run_tool = None
# 熔断截止时间戳（单调递增的 time.time()）。0 表示没在熔断
_unavailable_until = 0.0

# 结局的中文名，只用于报表显示。**判定与取值以 query_service 的 OUTCOME_* 为准**
OUTCOME_CN = {
    "answered": "已回答",
    "no_match": "无参考内容",
    "rejected": "审核拒绝",
    "paused": "用户暂停",
    "waiting_user": "等用户确认",
    "error": "出错",
}


class QueryRunStoreUnavailable(Exception):
    """运行记录暂不可用（连接失败后的熔断期内），调用方直接跳过落库即可"""


class QueryRunTool:
    """运行记录读写工具类：封装连接、集合与索引（沿用 mongo_usage_utils 的写法）"""

    def __init__(self):
        try:
            self.mongo_url = os.getenv("MONGO_URL")
            self.db_name = os.getenv("MONGO_DB_NAME")

            # 显式给一个短超时，别用 pymongo 默认的 30 秒 —— 原因见文件顶部
            self.client = MongoClient(
                self.mongo_url,
                serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
            )
            self.db = self.client[self.db_name]
            self.collection = self.db[COLLECTION_NAME]

            # 报表默认路径：按时间倒序取最近若干条
            self.collection.create_index([("ts", DESCENDING)])
            # 与账本 join：同一个 trace_id 上既有「这一轮每次调用」也有「这一轮结局」
            self.collection.create_index([("trace_id", 1)])
            # 按会话回看
            self.collection.create_index([("session_id", 1), ("ts", DESCENDING)])
        except Exception as e:
            logger.error(f"[运行记录] 初始化失败：{e}", exc_info=True)
            raise


def get_query_run_tool() -> QueryRunTool:
    """
    获取运行记录工具单例（懒加载）

    **失败后会熔断一段时间**：`QueryRunTool()` 初始化里有建索引、会真的连库，
    连不上时异常会让单例保持为 None，于是下一轮又要重等一遍连接超时。
    与账本同款处理：失败后 60 秒内直接抛 `QueryRunStoreUnavailable`，
    让写入迅速跳过而不是反复撞墙。
    """
    global _run_tool, _unavailable_until
    if time.time() < _unavailable_until:
        raise QueryRunStoreUnavailable("运行记录连接失败后的冷却期内，跳过落库")

    if _run_tool is None:
        try:
            _run_tool = QueryRunTool()
        except Exception:
            _unavailable_until = time.time() + CONNECT_FAILURE_COOLDOWN_SEC
            raise
    return _run_tool


def save_query_run(record: Dict[str, Any]) -> None:
    """
    写一条运行记录

    **吞掉全部异常**（与记账同一条规矩）：记录写不进去是记录的事，
    绝不能让用户拿不到答案。调用方不需要 try。
    """
    try:
        doc = dict(record)
        # ts 另存一份可读时间，方便直接看库（毫秒时间戳本身不直观）
        doc["datetime"] = datetime.fromtimestamp(doc.get("ts") or time.time()).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        get_query_run_tool().collection.insert_one(doc)
    except Exception as e:
        logger.warning(f"[运行记录] 写入失败（不影响业务）：{e}")


def _since(days: float) -> float:
    """最近 N 天的时间戳下界"""
    return (datetime.now() - timedelta(days=days)).timestamp()


def list_query_runs(days: float = 1.0, limit: int = 0) -> List[Dict[str, Any]]:
    """
    取最近 N 天的运行记录（按时间倒序）

    :param days: 回看天数
    :param limit: 最多返回几条；0 表示不限（报表要全量统计）
    """
    cursor = get_query_run_tool().collection.find({"ts": {"$gte": _since(days)}}).sort(
        "ts", DESCENDING
    )
    if limit:
        cursor = cursor.limit(limit)
    rows = list(cursor)
    for r in rows:
        r["_id"] = str(r.get("_id"))
    return rows


def build_report(days: float = 1.0, recent: int = 20) -> Dict[str, Any]:
    """
    汇总最近 N 天的运行记录

    :param days: 回看天数
    :param recent: 明细里列出最近几条
    :return: {days, runs, by_outcome, cost, avg_elapsed_ms, degraded_runs, recent}
    """
    rows = list_query_runs(days)

    by_outcome: Dict[str, int] = defaultdict(int)
    cost = 0.0
    elapsed_sum = 0
    degraded_runs = 0

    for r in rows:
        by_outcome[r.get("outcome") or "unknown"] += 1
        usage = r.get("usage") or {}
        cost += usage.get("cost") or 0.0
        elapsed_sum += usage.get("elapsed_ms") or 0
        if r.get("degraded_list"):
            degraded_runs += 1

    return {
        "days": days,
        "runs": len(rows),
        "by_outcome": dict(sorted(by_outcome.items(), key=lambda kv: kv[1], reverse=True)),
        "cost": round(cost, 6),
        "avg_elapsed_ms": int(elapsed_sum / len(rows)) if rows else 0,
        "degraded_runs": degraded_runs,
        "recent": rows[:recent],
    }


def _print_report(days: float) -> None:
    """命令行报表：不引表格库，手工对齐即可"""
    report = build_report(days)
    print(f"\n=== 查询运行记录 · 最近 {days:g} 天 ===")
    if not report["runs"]:
        print("  还没有任何记录。跑一次问答就会产生。\n")
        return

    print(f"  问答轮次 : {report['runs']}（其中 {report['degraded_runs']} 轮有节点降级）")
    print(f"  估算成本 : {report['cost']:.6f} 元")
    print(f"  平均耗时 : {report['avg_elapsed_ms']} ms")

    print("\n  ── 结局分布 ──")
    for outcome, n in report["by_outcome"].items():
        share = n / report["runs"] * 100
        print(f"  {OUTCOME_CN.get(outcome, outcome):<12} {n:<5} ({share:5.1f}%)")

    print("\n  ── 最近几次 ──")
    for r in report["recent"]:
        usage = r.get("usage") or {}
        line = (
            f"  {r.get('datetime') or '—':<20} "
            f"{OUTCOME_CN.get(r.get('outcome'), r.get('outcome') or '—'):<12} "
            f"{usage.get('elapsed_ms') or 0:>7}ms "
            f"¥{usage.get('cost') or 0:.6f} "
            f"本地{r.get('topk_local') or 0}/联网{r.get('topk_web') or 0} "
            f"{(r.get('question') or '')[:26]}"
        )
        print(line)
        if r.get("error"):
            print(f"    └─ {str(r['error'])[:100]}")
    print(f"\n  按 trace_id 看某一轮的调用明细：summarize_trace('<trace_id>')\n")


def _check_run_record_roundtrip() -> List[str]:
    """
    离线自测：写一条运行记录、读回来逐字段比对（只碰 Mongo，不调模型）

    守住的是「写进去的东西必须读得出来」—— 字段名写错、类型存不进去（如 numpy 标量）
    都不会报错，只是记录悄悄缺一块。**别把字段名硬编码进断言**，
    那样换个写法会连用例一起改错。

    :return: 问题描述列表，空表示通过
    """
    problems = []
    trace_id = "selftest_query_run"
    sample = {
        "trace_id": trace_id,
        "segment": "start",
        "session_id": "selftest_query_run_session",
        "tenant_id": "t_selftest",
        "question": "自测问题：这轮记录能不能原样存回来？",
        "rewritten_query": "自测改写后的问题",
        "item_names": ["自测产品 A", "自测产品 B"],
        "enable_web_search": True,
        "is_stream": False,
        "outcome": "answered",
        "error": "",
        "answer_chars": 123,
        "images_count": 2,
        "web_only": False,
        "topk_total": 5,
        "topk_local": 4,
        "topk_web": 1,
        "topk_chunk_ids": [111, 222, 333],
        "done_list": ["确认问题产品", "生成答案"],
        "degraded_list": [],
        "usage": {"calls": 8, "cost": 0.0095, "by_node": [{"node": "node_rerank", "calls": 1}]},
        "ts": time.time(),
    }

    try:
        save_query_run(sample)
        rows = list(get_query_run_tool().collection.find({"trace_id": trace_id}))
        if len(rows) != 1:
            problems.append(f"写入后应读到 1 条，实到 {len(rows)} 条")
        else:
            row = rows[0]
            for key, expect in sample.items():
                got = row.get(key)
                if got != expect:
                    problems.append(f"字段 {key} 往返不一致：写入 {expect!r}，读回 {got!r}")
            if not row.get("datetime"):
                problems.append("缺可读的 datetime 字段")
    finally:
        get_query_run_tool().collection.delete_many({"trace_id": trace_id})
    return problems


if __name__ == '__main__':
    from pymongo.errors import PyMongoError

    days_arg = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    try:
        _print_report(days_arg)
        _problems = _check_run_record_roundtrip()
        print(f"[{'PASS' if not _problems else 'FAIL'}] 运行记录读写往返")
        for _p in _problems:
            print(f"  [FAIL] {_p}")
    except (QueryRunStoreUnavailable, PyMongoError) as e:
        # 连不上时给人话，别把 pymongo 的拓扑描述甩给用户
        print(f"\n运行记录连不上：{type(e).__name__}。确认 MongoDB 已启动：docker start mongo\n")
    except Exception as e:
        print(f"\n读取运行记录失败：{e}\n")
