"""task_utils 回归用例（从生产模块迁出）。"""
import pytest

import app.utils.task_utils as _tu

globals().update({k: v for k, v in vars(_tu).items() if not k.startswith("__")})


def _check_run_isolation() -> list:
    """
    暂停的 run_id 隔离：换轮之后，上一轮的停止信号必须作废

    守的是 §3.9 那条返工过的语义 —— 前端的 session_id 跨轮复用，
    只按 session 置位会让迟到的暂停误杀下一轮。
    """
    problems = []
    sid = _self_id("isolation")
    try:
        set_active_run(sid, "run1")
        if request_stop(sid, "run1") is not True:
            problems.append("当前轮的停止请求竟然没置位")
        if is_stop_requested(sid) is not True:
            problems.append("置位了却读不到")

        # 换轮：新的 run_id，旧的停止标记必须失效
        set_active_run(sid, "run2")
        if is_stop_requested(sid) is not False:
            problems.append("换轮后仍报告暂停中（会误杀新一轮）")
        if request_stop(sid, "run1") is not False:
            problems.append("上一轮的迟到停止请求被接受了")

        # 上一轮迟到的收尾不能抹掉新一轮的登记
        clear_active_run(sid, "run1")
        if _active_for(sid) != "run2":
            problems.append("上一轮迟到的 clear 把新一轮的登记抹掉了")

        # 真正的收尾：带上正确 run_id 才清得掉
        clear_active_run(sid, "run2")
        if _active_for(sid) is not None:
            problems.append("带正确 run_id 的收尾没能清掉登记")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(sid)
    return problems


def _check_clear_active_is_atomic() -> list:
    """
    `clear_active_run(run_id)` 必须**一条 Redis 命令**完成「比较 + 删除」

    这是防 TOCTOU 的护栏：如果被改回「先 GET 再 DEL」两条独立命令，
    上一轮的收尾就能在两条命令之间把下一轮的登记删掉（→ 下一轮暂停静默失效）。
    代理里记下这次调用发出的命令名，断言只有一条 `eval`。

    依赖 Redis（没有它就观察不到命令）—— 所以 regression 里标 `redis` 依赖，
    不可用时整条 SKIP 而不是 FAIL。
    """
    problems = []
    sid = _self_id("atomic")
    recorded: List[str] = []
    # **改当前模块的 globals，不要 `import app.utils.task_utils as tu` 再改 tu 的属性**：
    # `python -m` 会把本文件当 `__main__` 执行，那个 import 会再加载出**第二个模块对象**，
    # 改的是另一份 globals，打桩等于没打（这里踩过）。同样的写法见 node_answer_output 自测。
    g = _tu.__dict__
    real_get_redis = g["get_redis"]

    class _Spy:
        """把命令名记下来再转发，不改行为"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            def wrapper(*args, **kwargs):
                recorded.append(name)
                return getattr(self._inner, name)(*args, **kwargs)

            return wrapper

    try:
        set_active_run(sid, "run1")
        recorded.clear()
        g["get_redis"] = lambda: _Spy(real_get_redis())
        clear_active_run(sid, "run1")
    except TaskStoreUnavailable:
        return []           # 熔断期内观察不到命令，交给回归套件的依赖探活去判
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        g["get_redis"] = real_get_redis
        clear_task(sid)

    if not problems:
        if recorded != ["eval"]:
            problems.append(
                f"清登记发出了 {recorded}，应只有一条 'eval'（两条独立命令之间存在 TOCTOU）"
            )
    return problems


def _check_parallel_writes() -> list:
    """
    四路并发写同一个 task_id：会崩溃吗？最终状态对吗？

    对应检索图里四路并发（向量 / HyDE / 联网 / 图谱）都往同一个 session_id 写进度。

    ⚠️ **这条抓不住「读-改-写」**：实测把 `add_done_task` 换成
    「SMEMBERS → 过滤 → 重建集合」之后，本条**三次全绿** —— 四个线程因为启动开销
    天然错开，靠调度碰运气复现不了丢更新。真正锁住那条不变量的是
    `_check_no_read_modify_write`（确定性地断言写入路径上不发读命令）。
    这里留着，是为了覆盖并发下的崩溃与最终状态。
    """
    import threading

    problems = []
    tid = _self_id("parallel")
    nodes = ["node_search_embedding", "node_search_embedding_hyde", "node_search_bm25",
             "node_web_search_mcp", "node_query_kg"]
    errors: List[str] = []

    def worker(node: str):
        try:
            add_running_task(tid, node)
            add_done_task(tid, node)
        except Exception as e:          # 线程里的异常不会让主线程知道，自己收着
            errors.append(f"{node}: {type(e).__name__}: {e}")

    try:
        threads = [threading.Thread(target=worker, args=(n,)) for n in nodes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            problems.extend(errors)
        done = set(get_done_task_list(tid))
        expect = {_to_cn(n) for n in nodes}
        if done != expect:
            problems.append(f"完成的节点应为 {sorted(expect)}，实际 {sorted(done)}（并发丢了更新）")
        if get_running_task_list(tid):
            problems.append(f"完成后仍留着运行中的节点：{get_running_task_list(tid)}")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(tid)
    return problems


def _check_no_read_modify_write() -> list:
    """
    写进度**不能是「读-改-写」**—— 写入路径上一条读命令都不该发

    这才是真正锁住并发不丢更新的用例。共享存储上四路并发写同一个 key，
    只要「先读出来、再写回去」，中间夹进来的那次写入就会丢。
    靠线程调度复现不了（`_check_parallel_writes` 在错误实现上三次全绿），
    所以改成**确定性地**看行为：打桩记下调用期间发出的命令，
    断言里面没有任何读命令（`SMEMBERS` / `GET` / `HGETALL` …）。

    依赖 Redis（没有它就观察不到命令）—— regression 里标 `redis`，不可用时 SKIP。
    """
    problems = []
    tid = _self_id("rmw")
    recorded: List[str] = []
    g = _tu.__dict__
    real_get_redis = g["get_redis"]

    class _Spy:
        """只记顶层命令名（pipeline 内部的方法不走这里，够用了）"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            def wrapper(*args, **kwargs):
                recorded.append(name)
                return getattr(self._inner, name)(*args, **kwargs)

            return wrapper

    # 任何读命令出现都算违规 —— 这些名字来自 redis-py 的 Redis 类
    read_cmds = {"smembers", "get", "mget", "hgetall", "hget", "hmget", "exists", "scard", "sismember"}
    try:
        set_active_run(f"{tid}_guard", "r1")     # 先把 Redis 打通，别让连接失败混进来
        clear_active_run(f"{tid}_guard", "r1")
        g["get_redis"] = lambda: _Spy(real_get_redis())

        recorded.clear()
        add_running_task(tid, "node_rrf")
        bad = set(recorded) & read_cmds
        if bad:
            problems.append(f"add_running_task 发了读命令 {sorted(bad)}（读-改-写在并发下会丢更新）")

        recorded.clear()
        add_done_task(tid, "node_rrf")
        bad = set(recorded) & read_cmds
        if bad:
            problems.append(f"add_done_task 发了读命令 {sorted(bad)}（读-改-写在并发下会丢更新）")
    except TaskStoreUnavailable:
        return []
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        g["get_redis"] = real_get_redis
        clear_task(tid)
    return problems


def _check_result_roundtrip() -> list:
    """
    结果字段往返后**类型不变**

    值被 JSON 编码后落库，所以逐字段验一遍：`images` 是 list[dict]、`need_confirm`
    是 bool、`clarify` 里的 `score` 是 float。顺带守住 `answer=""` 不被当成缺省
    （判缺失要用 `is None`，不能用真值判断 —— 这是 `_build_history` 那类静默 bug 的同款）。
    """
    problems = []
    tid = _self_id("result")
    try:
        set_task_result(tid, "answer", "")
        set_task_result(tid, "images", [{"url": "http://x/a.png", "caption": "图注"}])
        set_task_result(tid, "web_only", True)
        set_task_result(tid, "need_confirm", False)
        set_task_result(tid, "clarify", {
            "question": "是哪一个？",
            "options": [{"item_name": "万用表RS-12", "file_title": "a.pdf",
                         "score": 0.8834, "near": True}],
            "allow_custom": True,
        })

        if get_task_result(tid, "answer") != "":
            problems.append("空字符串答案没有原样读回（真值判断会把它当成缺省）")
        images = get_task_result(tid, "images")
        if images != [{"url": "http://x/a.png", "caption": "图注"}]:
            problems.append(f"images 往返不一致：{images!r}")
        if get_task_result(tid, "web_only") is not True:
            problems.append("web_only 往返后不是 True")
        if get_task_result(tid, "need_confirm") is not False:
            problems.append("need_confirm 往返后不是 False（缺省判断被混淆了）")

        clarify = get_task_result(tid, "clarify")
        score = (((clarify or {}).get("options") or [{}])[0]).get("score")
        if not isinstance(score, float) or abs(score - 0.8834) > 1e-9:
            problems.append(f"clarify 里的 score 往返后类型/值变了：{score!r}")
        if (clarify or {}).get("allow_custom") is not True:
            problems.append("clarify.allow_custom 往返后不是 True")

        # 缺省值必须原样返回调用方给的那个（调用方会传 [] / False / {}）
        sentinel = object()
        if get_task_result(tid, "没有这个字段", sentinel) is not sentinel:
            problems.append("缺失字段没有返回调用方给的缺省值")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(tid)
    return problems


def _check_reset_clears_result() -> list:
    """新开一轮时，结果字段（含 `web_only`）必须全部清掉"""
    problems = []
    tid = _self_id("reset")
    try:
        add_done_task(tid, "node_rrf")
        add_degraded_task(tid, "node_query_kg")
        add_running_task(tid, "node_rerank")
        for k, v in (("answer", "上一轮的答案"), ("images", [{"url": "u"}]),
                     ("web_only", True), ("need_confirm", True),
                     ("clarify", {"question": "q"}), ("run_error", "e")):
            set_task_result(tid, k, v)

        reset_task_progress(tid)

        if get_done_task_list(tid) or get_running_task_list(tid) or get_degraded_task_list(tid):
            problems.append("进度没清干净")
        for k, default in (("answer", ""), ("images", []), ("need_confirm", False),
                           ("clarify", {}), ("run_error", "")):
            if get_task_result(tid, k, default) != default:
                problems.append(f"结果字段 {k} 没被清掉（上一轮的会渗进这一轮）")
        # 改造前的清理列表漏了 web_only，这一条专门守它
        if get_task_result(tid, "web_only", False) is not False:
            problems.append("web_only 没被清掉（上一轮的纯联网横幅会渗进这一轮）")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(tid)
    return problems


def _check_unknown_getters_are_pure() -> list:
    """
    读未知 task_id 不能建 key、也不能刷 TTL

    导入页每 2 秒轮询一次 `/status`，读若顺手把 key 建出来或刷新存活时间，
    进度就**永远回收不掉**，TTL 等于白设。所以读必须是纯的。

    TTL 那部分要**先把 TTL 压成一个短值再读**：直接拿默认值前后对比是分辨不出来的
    —— Redis 的 TTL 精度是秒，而写入与读取发生在同一秒内，`after > before` 永远为假
    （第一版就是这么写的，对错误实现照样全绿）。
    """
    problems = []
    tid = _self_id("unknown")
    probes = [
        (_self_id("ttl_done"), _k(_self_id("ttl_done"), _F_DONE),
         lambda i: add_done_task(i, "node_rrf"), lambda i: get_done_task_list(i)),
        (_self_id("ttl_result"), _k(_self_id("ttl_result"), _F_RESULT),
         lambda i: set_task_result(i, "answer", "a"), lambda i: get_task_result(i, "answer")),
        (_self_id("ttl_status"), _k(_self_id("ttl_status"), _F_STATUS),
         lambda i: update_task_status(i, TASK_STATUS_PROCESSING), lambda i: get_task_status(i)),
    ]
    try:
        if get_done_task_list(tid) or get_running_task_list(tid) or get_degraded_task_list(tid):
            problems.append("未知 task_id 读出了非空进度")
        if get_task_result(tid, "answer", "") != "":
            problems.append("未知 task_id 读出了结果字段")
        if get_task_status(tid) != "":
            problems.append("未知 task_id 读出了状态")

        client = get_redis()
        if client.exists(_k(tid, _F_DONE)) or client.exists(_k(tid, _F_RESULT)):
            problems.append("读未知 task_id 竟然把 key 建出来了")

        # 有内容的 key：把 TTL 压到 30 秒，再读几次 —— 纯读应当让它继续倒数
        for tid2, key, write, read in probes:
            write(tid2)
            client.expire(key, 30)
            before = client.ttl(key)
            for _ in range(3):
                read(tid2)
            after = client.ttl(key)
            if after > before:
                problems.append(
                    f"读操作刷新了 TTL（{key}：{before}s → {after}s），进度将永远回收不掉"
                )
            if not client.exists(key):
                problems.append(f"{key} 读完之后不见了")
    except TaskStoreUnavailable as e:
        problems.append(f"Redis 不可用，本用例只在有 Redis 时有意义：{e}")
    except Exception as e:
        problems.append(f"异常：{type(e).__name__}: {e}")
    finally:
        clear_task(tid)
        for tid2, _key, _w, _r in probes:
            clear_task(tid2)
    return problems


def _check_backend_fallback() -> list:
    """
    Redis 连不上时必须降级到进程内存：API 可用、不抛、暂停照旧生效（同进程内）

    **必须在子进程里跑**：把 REDIS_URL 指向死端口会写进 `redis_clients` 的模块级
    熔断状态，在本进程里跑就会污染后面所有用例（它们会跟着一起降级）。
    照 §3.12 的做法，用环境变量在独立进程里验。
    """
    code = (
        "import os\n"
        "os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'\n"   # 没有服务在听
        "from app.utils.task_utils import (\n"
        "    add_running_task, add_done_task, get_done_task_list, get_running_task_list,\n"
        "    set_task_result, get_task_result, set_active_run, request_stop,\n"
        "    is_stop_requested, clear_task)\n"
        "tid = 'selftest_fallback'\n"
        "add_running_task(tid, 'node_rrf')\n"
        "add_done_task(tid, 'node_rrf')\n"
        "assert get_done_task_list(tid) == ['倒排融合'], get_done_task_list(tid)\n"
        "assert get_running_task_list(tid) == [], get_running_task_list(tid)\n"
        "set_task_result(tid, 'images', [{'url': 'u', 'caption': 'c'}])\n"
        "assert get_task_result(tid, 'images') == [{'url': 'u', 'caption': 'c'}]\n"
        "set_active_run('s1', 'r1')\n"
        "assert request_stop('s1', 'r1') is True\n"
        "assert is_stop_requested('s1') is True\n"
        "assert request_stop('s1', 'r0') is False\n"
        "clear_task(tid)\n"
        "print('OK')\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        # encoding 显式给 utf-8：本机默认是 GBK，子进程打的中文会解码失败 ——
        # 那会让 r.stdout 变成 None，把真实结论盖掉（踩过）
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception as e:
        return [f"子进程起不来：{type(e).__name__}: {e}"]
    if r.returncode != 0 or "OK" not in r.stdout:
        return [f"Redis 不可用时没能降级到内存（退出码 {r.returncode}）："
                f"{(r.stdout or '')[-300:]}{(r.stderr or '')[-500:]}"]
    return []

PURE_CHECKS = [
    ("_check_run_isolation", _check_run_isolation),
    ("_check_parallel_writes", _check_parallel_writes),
    ("_check_result_roundtrip", _check_result_roundtrip),
    ("_check_reset_clears_result", _check_reset_clears_result),
    ("_check_backend_fallback", _check_backend_fallback),
]

REDIS_CHECKS = [
    ("_check_clear_active_is_atomic", _check_clear_active_is_atomic),
    ("_check_no_read_modify_write", _check_no_read_modify_write),
    ("_check_unknown_getters_are_pure", _check_unknown_getters_are_pure),
]

@pytest.mark.parametrize("name,fn", PURE_CHECKS, ids=[n for n, _ in PURE_CHECKS])
def test_task_utils_pure(name, fn):
    assert fn() == []


@pytest.mark.integration
@pytest.mark.parametrize("name,fn", REDIS_CHECKS, ids=[n for n, _ in REDIS_CHECKS])
def test_task_utils_redis(name, fn, redis_ready):
    assert fn() == []
