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
    ("重排给本地切片留配额",
     "app.query_process.agent.nodes.node_rerank", "_check_local_quota", ""),
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
    ("中断→恢复三条不变量",
     "app.query_process.agent.nodes.node_ask_user", "_check_interrupt_resume", "mongo"),
]

DEP_CN = {"mongo": "MongoDB", "milvus": "Milvus"}

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

    probes = {"mongo": _probe_mongo, "milvus": _probe_milvus}
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
