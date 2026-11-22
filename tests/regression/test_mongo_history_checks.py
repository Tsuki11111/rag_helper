"""Mongo 会话历史回归用例（从 mongo_history_clients.py 迁出）。"""
import pytest

import app.clients.mongo_history_clients as _mh

globals().update({k: v for k, v in vars(_mh).items() if not k.startswith("__")})


def _check_history_order() -> list:
    """
    离线自测：基本读写 + **历史顺序**（只碰 Mongo，不调模型）

    守两条 2026-10-06 修过的回归：

    1. **更新一条老消息不该改变它的位置** —— `save_chat_message` 早先更新时连 `ts` 一起 `$set`，
       而「认不出产品 → 转去询问用户」那条路的真实次序是「先存用户提问 → 存助手那句澄清问题
       → 再回填用户提问的改写结果」，于是用户提问被挪到了澄清问题之后、**历史顺序整个反掉**
       （实测 14 个会话里 9 个中招）
    2. **排序键必须是 `_id` 而不是 `ts`** —— 本机 `datetime.now().timestamp()` 连取 2000 次
       只有 2 个不同值，同一轮里连续写入的两条消息经常拿到**完全相同**的 `ts`，
       撞车时排序就成了掷骰子

    :return: 问题描述列表，空表示通过
    """
    import time

    sid, tie_sid = "selftest_history", "selftest_history_tie"
    problems = []
    try:
        # 1. 基本通路：写一条、读回来
        uid = save_chat_message(sid, "user", "你好（自测）")
        msgs = get_recent_messages(sid, limit=5)
        if len(msgs) != 1 or msgs[0].get("text") != "你好（自测）":
            problems.append(f"基本写入/读取不对：{msgs}")
        # 记下这条的 ts，下面要验证「更新不会改变它」
        _row = get_history_mongo_tool().chat_message.find_one({"_id": ObjectId(uid)})
        _ts_before = (_row or {}).get("ts")

        # 2. 顺序回归：更新老消息不该改变它的位置
        time.sleep(0.05)   # 拉开时间，确保「若刷新 ts 就会乱序」
        save_chat_message(sid, "assistant", "「自测」没能锁定到具体产品，你想问的是下面哪一个？")
        save_chat_message(sid, "user", "你好（自测）",
                          rewritten_query="你好（改写后）", message_id=uid)

        msgs = get_recent_messages(sid, limit=10)
        order = [m.get("role") for m in msgs]
        if order != ["user", "assistant"]:
            problems.append(
                f"更新后历史顺序反了：{order}，应为 ['user', 'assistant']"
                f" —— 更新是不是把 ts 一起刷了？")
        # 别为了不动 ts 把内容也一起漏掉
        if msgs and msgs[0].get("rewritten_query") != "你好（改写后）":
            problems.append(f"更新没写进内容：rewritten_query={msgs[0].get('rewritten_query')!r}")
        # 顺带钉住「更新不动 ts」这条语义本身。
        # **单看顺序抓不到它** —— 排序键已经是 _id，ts 被弄坏顺序也照样对，
        # 两道防线重叠。但 `save_chat_message` 的注释承诺了「ts 是创建时间、
        # 更新不动它」，这条断言守的就是那句话别变成假的。
        _after = get_history_mongo_tool().chat_message.find_one({"_id": ObjectId(uid)})
        if _after and abs((_after.get("ts") or 0) - _ts_before) > 1e-6:
            problems.append(
                f"更新把 ts 改了：{_ts_before} → {_after.get('ts')}"
                f" —— 更新不该改变消息在对话里的位置")

        # 3. ts 与插入顺序矛盾时，仍要按**插入顺序**返回
        base = datetime.now().timestamp()
        get_history_mongo_tool().chat_message.insert_many([
            {"session_id": tie_sid, "role": "user", "text": "先写的",
             "rewritten_query": "", "item_names": None, "images": None,
             "ts": base + 10},   # 先插入，ts 反而更大
            {"session_id": tie_sid, "role": "assistant", "text": "后写的",
             "rewritten_query": "", "item_names": None, "images": None,
             "ts": base},        # 后插入，ts 反而更小
        ])
        tie_order = [m.get("role") for m in get_recent_messages(tie_sid, limit=10)]
        if tie_order != ["user", "assistant"]:
            problems.append(
                f"ts 与插入顺序矛盾时没按插入顺序返回：{tie_order} —— 排序键是不是用回 ts 了？")
    except Exception as e:
        problems.append(f"历史读写时抛异常：{type(e).__name__}: {e}")
    finally:
        get_history_mongo_tool().chat_message.delete_many(
            {"session_id": {"$in": [sid, tie_sid]}})
    return problems


def _check_images_roundtrip() -> list:
    """
    离线自测：配图连同**图注**存进去、原样读回来（只碰 Mongo）

    守的回归：答案配图必须**连图注一起落库**。只存 URL 的话，重新打开历史时
    每张图都会退化成「未标注来源」—— 图注本来是导入时视觉模型逐张写的、描述图里实际有什么。

    :return: 问题描述列表，空表示通过
    """
    sid = "selftest_history_img"
    problems = []
    imgs = [{"url": "http://example.invalid/a.jpg", "caption": "打开支架盖"},
            {"url": "http://example.invalid/b.jpg", "caption": "插入烫金膜盒"}]
    try:
        save_chat_message(sid, "user", "怎么装？")
        save_chat_message(sid, "assistant", "分两步。", images=imgs)
        got = get_recent_messages(sid, limit=10)[-1].get("images")
        if got != imgs:
            problems.append(f"配图没原样读回：{got!r}（是不是存档时没传 images？）")
    except Exception as e:
        problems.append(f"配图往返时抛异常：{type(e).__name__}: {e}")
    finally:
        get_history_mongo_tool().chat_message.delete_many({"session_id": sid})
    return problems


def _check_products_roundtrip() -> list:
    """离线自测：导购商品卡片随消息存档、原样读回（只碰 Mongo，M11/M12）。"""
    sid = "selftest_history_products"
    problems = []
    products = [{
        "productId": "1", "name": "游戏本", "image": "http://example.invalid/a.jpg",
        "price": "6999.00", "stock": "3", "sales": "12", "rating": "4.8",
        "merchantName": "数码店", "specText": "颜色:黑", "categoryName": "电脑",
    }]
    try:
        save_chat_message(sid, "user", "推荐个电脑")
        save_chat_message(sid, "assistant", "推荐这款。", products=products)
        got = get_recent_messages(sid, limit=10)[-1].get("products")
        if got != products:
            problems.append(f"商品卡片没原样读回：{got!r}")
    except Exception as e:
        problems.append(f"商品卡片往返时抛异常：{type(e).__name__}: {e}")
    finally:
        get_history_mongo_tool().chat_message.delete_many({"session_id": sid})
    return problems


def _check_trace_roundtrip() -> list:
    """
    离线自测：执行流（trace）随消息存档、原样读回（只碰 Mongo，M5.1）

    守的回归：重开历史要把**执行过程**一并还原（用户要求）。旧消息没有这个字段 ——
    读回按缺失/空处理，不能因此报错。

    :return: 问题描述列表，空表示通过
    """
    sid = "selftest_history_trace"
    problems = []
    trace = [{"kind": "delegate", "agent": "产品知识库智能体", "detail": "检索资料"},
             {"kind": "node", "agent": "产品知识库智能体", "detail": "切片搜索"},
             {"kind": "reply", "agent": "买小购", "detail": "生成回复（12 字符）"}]
    try:
        save_chat_message(sid, "user", "怎么装？")
        save_chat_message(sid, "assistant", "分两步。", trace=trace)
        row = get_recent_messages(sid, limit=10)[-1]
        if row.get("trace") != trace:
            problems.append(f"执行流没原样读回：{row.get('trace')!r}")
        # 老消息没有 trace 字段：读到的是缺失（不是报错）——调用方按空处理
        old = [m for m in get_recent_messages(sid, limit=10) if m.get("role") == "user"][0]
        if old.get("trace") not in (None, []):
            problems.append(f"没有 trace 的消息读出了怪值：{old.get('trace')!r}")
    except Exception as e:
        problems.append(f"执行流往返时抛异常：{type(e).__name__}: {e}")
    finally:
        get_history_mongo_tool().chat_message.delete_many({"session_id": sid})
    return problems


def _check_list_sessions() -> list:
    """
    离线自测：会话列表（只碰 Mongo）

    守的回归：`list_sessions` 的标题要取**第一条 user 消息**、按**最近活跃倒序**、
    `count` 与 `last_at` 正确。`last_at` 取自 ObjectId 的内嵌时间戳而非 `ts`
    （`ts` 不可信，见 `_check_history_order`）。

    :return: 问题描述列表，空表示通过
    """
    a_sid, b_sid = "selftest_history", "selftest_history_b"
    problems = []
    try:
        save_chat_message(a_sid, "user", "A 会话的第一句提问")
        save_chat_message(a_sid, "assistant", "A 的回答")
        save_chat_message(b_sid, "user", "B 会话的第一句提问")   # 晚写 → 应排在前面
        save_chat_message(b_sid, "assistant", "B 的回答")

        listed = list_sessions(limit=200)
        by_sid = {s["session_id"]: s for s in listed}
        for want in (a_sid, b_sid):
            if want not in by_sid:
                problems.append(f"list_sessions 没列出会话 {want}")
        if a_sid in by_sid:
            if by_sid[a_sid]["title"] != "A 会话的第一句提问":
                problems.append(f"标题没有取第一条 user 消息：{by_sid[a_sid]['title']!r}")
            if by_sid[a_sid]["count"] != 2:
                problems.append(f"count 不对：{by_sid[a_sid]['count']}")
            if not by_sid[a_sid]["last_at"]:
                problems.append("last_at 为空")
        if a_sid in by_sid and b_sid in by_sid:
            order = [s["session_id"] for s in listed]
            if order.index(b_sid) > order.index(a_sid):
                problems.append("没有按最近活跃倒序（后写的应排前面）")
    except Exception as e:
        problems.append(f"列会话时抛异常：{type(e).__name__}: {e}")
    finally:
        get_history_mongo_tool().chat_message.delete_many(
            {"session_id": {"$in": [a_sid, b_sid]}})
    return problems


def _check_session_ownership() -> list:
    """
    离线自测：多用户会话归属（只碰 Mongo）

    守的回归：
    - 同一 session 不能被另一个 user_id 接管
    - 按 user_id 读历史 / 列会话互不串
    - 老数据缺 user_id 时，首次认领会补齐
    """
    sid = "selftest_session_owner"
    problems = []
    try:
        ensure_session_owner(sid, "user_a", "tenant_a")
        save_chat_message(sid, "user", "A 的问题", user_id="user_a", tenant_id="tenant_a")
        save_chat_message(sid, "assistant", "A 的回答", user_id="user_a", tenant_id="tenant_a")

        if len(get_recent_messages(sid, user_id="user_a")) != 2:
            problems.append("user_a 读不到自己的两条历史")
        if get_recent_messages(sid, user_id="user_b"):
            problems.append("user_b 读到了 user_a 的历史")

        listed_a = {s["session_id"] for s in list_sessions(limit=500, user_id="user_a")}
        listed_b = {s["session_id"] for s in list_sessions(limit=500, user_id="user_b")}
        if sid not in listed_a:
            problems.append("user_a 的会话列表里没有自己的会话")
        if sid in listed_b:
            problems.append("user_b 的会话列表里出现了别人的会话")

        try:
            ensure_session_owner(sid, "user_b", "tenant_b")
            problems.append("user_b 竟然接管了 user_a 的会话")
        except PermissionError:
            pass

        # 老数据迁移：没有 user_id 的消息，认领后应归到当前用户
        get_history_mongo_tool().chat_message.insert_one({
            "session_id": sid, "role": "user", "text": "迁移前的老消息",
            "rewritten_query": "", "item_names": None, "images": None, "ts": 0,
        })
        ensure_session_owner(sid, "user_a", "tenant_a")
        texts = [m.get("text") for m in get_recent_messages(sid, user_id="user_a")]
        if "迁移前的老消息" not in texts:
            problems.append("缺 user_id 的老消息没有被补齐")
    except Exception as e:
        problems.append(f"会话归属自测抛异常：{type(e).__name__}: {e}")
    finally:
        tool = get_history_mongo_tool()
        tool.chat_message.delete_many({"session_id": sid})
        tool.chat_session.delete_many({"session_id": sid})
    return problems

CHECKS = [
    ("_check_history_order", _check_history_order),
    ("_check_images_roundtrip", _check_images_roundtrip),
    ("_check_products_roundtrip", _check_products_roundtrip),
    ("_check_trace_roundtrip", _check_trace_roundtrip),
    ("_check_list_sessions", _check_list_sessions),
    ("_check_session_ownership", _check_session_ownership),
]

@pytest.mark.integration
@pytest.mark.parametrize("name,fn", CHECKS, ids=[n for n, _ in CHECKS])
def test_mongo_history(name, fn, mongo_ready):
    assert fn() == []
