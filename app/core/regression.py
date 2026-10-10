"""
回归用例统一入口

2026-10-06 一天修掉了 **8 个 bug**，它们有个共同特征：**全是静默的** —— 不报错、不崩溃、
界面也不变样，只是功能悄悄失效（本地检索归零、有图变没图、多轮上下文为空……）。
所以它们当初一个都不是测试发现的，全是靠**真实提问 + 人肉读日志**挖出来的。

这个模块把那批不变量汇成一条命令。**它发现不了新问题** —— 作用只有一个：
**别让已经修好的问题悄悄复活**。

用例本身**贴在各自守护的代码旁边**（项目既有约定：`_check_xxx() -> list[str]`），
这里只负责找到它们、跑、汇总。清单是**显式列出**的，不自动发现 ——
「这几个曾经坏过」这件事本身就是清单的意义，扫出来的函数没有这份记忆。

```bash
.venv/Scripts/python.exe -m app.core.regression            # 只看结果表
.venv/Scripts/python.exe -m app.core.regression --verbose  # 连日志一起看
```

**耗时**：冷启动实测 **11~16 秒**，其中 10 秒以上是**一次性开销** —— 把节点模块 import 进来
（langchain / transformers 那一串）＋ 首个碰 Milvus 的用例要初始化 embedding 客户端。
其余 9 条加起来 **不到 1 秒**。所以它适合「改完跑一次」，不适合塞进每次保存的钩子。

**默认静音**：有几条用例是**故意触发错误**的（审核拒绝、降级），它们会打出 ERROR 堆栈 ——
那是预期行为，却让结果表看起来像跑挂了。所以默认把日志 sink 摘掉，只看表格；
失败信息由表格自己给全（每条用例返回的就是问题描述）。要看日志加 `--verbose`。

依赖不可用时（Mongo / Milvus 没起）对应用例标 `SKIP` 而**不是** `FAIL`，
退出码仍是 0 —— 否则容器抖一下看起来就像回归了，这张网很快就会没人信。
有 `FAIL` 时退出码为 1，便于以后接 CI。
"""
import argparse
import importlib
import os
import sys
import time

from app.core.logger import logger

# 用例清单：(显示名, 模块路径, 函数名, 依赖)
# 依赖 ∈ {"", "mongo", "milvus"}；空串表示纯逻辑、不碰任何外部服务
CASES = [
    ("手填型号对齐到标准名",
     "app.query_process.agent.nodes.node_ask_user", "_check_normalize_choice", "milvus"),
    ("重排：本地必在榜、联网封顶",
     "app.query_process.agent.nodes.node_rerank", "_check_local_priority", ""),
    ("RRF 融合不重复、多路命中累加",
     "app.query_process.agent.nodes.node_rrf", "_check_fusion", ""),
    ("审核拒绝短路、一轮只存一条助手消息",
     "app.query_process.agent.nodes.node_item_name_confirm", "_check_rejection_shortcut", "mongo"),
    ("历史按插入顺序、不按 ts",
     "app.clients.mongo_history_utils", "_check_history_order", "mongo"),
    ("配图连图注往返",
     "app.clients.mongo_history_utils", "_check_images_roundtrip", "mongo"),
    ("配图经答案节点落库",
     "app.query_process.agent.nodes.node_answer_output", "_check_images_persisted", "mongo"),
    ("会话列表按最近活跃、标题取首问",
     "app.clients.mongo_history_utils", "_check_list_sessions", "mongo"),
    ("生成答案时历史非空",
     "app.query_process.agent.nodes.node_answer_output", "_check_build_history", "mongo"),
    ("流式图片标记边界",
     "app.query_process.agent.nodes.node_answer_output", "_check_stream_boundary", ""),
    ("切分不丢第一个标题前的内容",
     "app.import_process.agent.nodes.node_document_split", "_check_preamble_kept", ""),
    ("中断→恢复三条不变量",
     "app.query_process.agent.nodes.node_ask_user", "_check_interrupt_resume", "mongo"),
    # 任务状态搬到 Redis 后新加的八条：守「并发写不丢、写路径不是读-改-写、清登记原子、
    # 换轮不串味、结果类型往返、重置清全、读无副作用不刷 TTL、Redis 挂掉降级到内存」。
    # 依赖 Redis 的三条（要观察命令/TTL）标了 redis，Redis 不在时 SKIP 而非 FAIL
    ("暂停的 run_id 隔离",
     "app.utils.task_utils", "_check_run_isolation", ""),
    ("清登记是单条原子命令",
     "app.utils.task_utils", "_check_clear_active_is_atomic", "redis"),
    ("四路并发写进度不丢更新",
     "app.utils.task_utils", "_check_parallel_writes", ""),
    ("写进度不是读-改-写",
     "app.utils.task_utils", "_check_no_read_modify_write", "redis"),
    ("结果字段 JSON 往返",
     "app.utils.task_utils", "_check_result_roundtrip", ""),
    ("重置清掉全部结果字段",
     "app.utils.task_utils", "_check_reset_clears_result", ""),
    ("读未知 task 无副作用不刷 TTL",
     "app.utils.task_utils", "_check_unknown_getters_are_pure", "redis"),
    ("Redis 挂掉时降级到内存",
     "app.utils.task_utils", "_check_backend_fallback", ""),
    # 运行记录（query_runs，评测轨道的输入）：一条纯逻辑判结局映射，一条真跑一遍
    # run_query_graph（打桩图）确认记录**确实落库** —— 后者守的是调用点，不是函数
    ("状态→结局的映射与优先序",
     "app.query_process.api.query_service", "_check_outcome_mapping", ""),
    ("问答收尾落一条运行记录",
     "app.query_process.api.query_service", "_check_run_recorded", "mongo"),
    # 图片 URL 里带空格（文档名带空格时就是这样）不被白名单丢掉 —— 曾让 7 份文档里
    # 4 份的图全部召回不到，且不报错
    ("带空格的图片 URL 不被白名单丢掉",
     "app.query_process.agent.nodes.node_answer_output", "_check_image_whitelist_space_url", ""),
    # 输出护栏：模型被注入带偏、把内部提示词原文吐进答案时整段换成拒答
    ("内部提示词泄漏被输出护栏拦下",
     "app.query_process.agent.nodes.node_answer_output", "_check_prompt_leak_guard", ""),
    ("输出护栏接在生成路径上（打桩模型）",
     "app.query_process.agent.nodes.node_answer_output", "_check_prompt_leak_guard_wired", "mongo"),
    # 提示词注入那一批（2026-10-09）：输入护栏 / 它的接线 / 联网结果的不可信标注
    ("输入护栏拦下注入写法且不误伤",
     "app.core.input_guard", "_check_input_guard", ""),
    ("输入护栏命中时短路且不调模型",
     "app.query_process.agent.nodes.node_item_name_confirm", "_check_input_guard_shortcut", "mongo"),
    ("联网结果在上下文里带不可信标记",
     "app.query_process.agent.nodes.node_answer_output", "_check_context_marks_web", ""),
    # 故障分类重试：策略本身 + 一条接线（httpx 不抛非 2xx，最容易静默失效的就是它）
    ("重试只重试暂时性故障、429 认 Retry-After",
     "app.core.retry", "_check_retry_policy", ""),
    ("重排遇 429 真的会重试",
     "app.lm.reranker_utils", "_check_retry_on_429", ""),
]

DEP_CN = {"mongo": "MongoDB", "milvus": "Milvus", "redis": "Redis"}

# 探活超时。**必须显式设**：pymongo 默认等 30 秒，Mongo 挂着时这一条就把整轮拖成半分钟，
# 而「跑起来够快」是这张网有人肯用的前提。同样的快失败模式见 mongo_usage_utils / mongo_checkpoint_utils
PROBE_TIMEOUT_MS = 1500


def _probe_mongo() -> bool:
    from pymongo import MongoClient
    try:
        MongoClient(os.getenv("MONGO_URL") or "mongodb://127.0.0.1:27017",
                    serverSelectionTimeoutMS=PROBE_TIMEOUT_MS).admin.command("ping")
        return True
    except Exception:
        return False


def _probe_milvus() -> bool:
    try:
        from app.clients.milvus_utils import get_milvus_client
        return get_milvus_client() is not None
    except Exception:
        return False


def _probe_redis() -> bool:
    """任务状态的共享存储（见 app/clients/redis_utils.py）"""
    try:
        from app.clients.redis_utils import is_available
        return is_available()
    except Exception:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.core.regression",
        description="跑一遍「那几个曾经坏过的不变量」",
    )
    ap.add_argument("--verbose", action="store_true",
                    help="保留日志输出（默认静音，只看结果表）")
    args = ap.parse_args(argv)

    if not args.verbose:
        # 有几条用例故意触发错误（审核拒绝、降级），会打 ERROR 堆栈 —— 那是预期行为，
        # 但混在结果表里看着像跑挂了，所以默认摘掉 sink。失败信息由表格自己给全。
        logger.remove()

    print("回归用例 · 检查那几个曾经坏过的不变量\n")

    probes = {"mongo": _probe_mongo, "milvus": _probe_milvus, "redis": _probe_redis}
    deps = {}
    for key, probe in probes.items():
        # 只探实际用到的依赖，省掉没必要的等待
        deps[key] = probe() if any(c[3] == key for c in CASES) else True

    n_pass = n_fail = n_skip = 0
    failures = []
    t_all = time.time()

    for name, mod_path, fn_name, dep in CASES:
        if dep and not deps.get(dep, True):
            n_skip += 1
            print(f"  [SKIP] {name:<34} （{DEP_CN[dep]} 不可用）")
            continue
        t0 = time.time()
        try:
            fn = getattr(importlib.import_module(mod_path), fn_name)
            problems = fn() or []
        except Exception as e:
            # 模块导入失败 / 函数抛异常都算这一条失败，但不影响其余用例继续跑
            problems = [f"调用失败：{type(e).__name__}: {e}"]
        cost = time.time() - t0
        if problems:
            n_fail += 1
            print(f"  [FAIL] {name:<34} {cost:6.2f}s")
            failures.append((name, problems))
        else:
            n_pass += 1
            print(f"  [PASS] {name:<34} {cost:6.2f}s")

    print(f"\n  通过 {n_pass} / 跳过 {n_skip} / 失败 {n_fail}"
          f" · 总耗时 {time.time() - t_all:.1f}s")

    for name, problems in failures:
        print(f"\n  ── {name} ──")
        for p in problems:
            print(f"     · {p}")

    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
