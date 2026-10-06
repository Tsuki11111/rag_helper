# 导入系统模块：用于读取环境变量
import os
# 导入日志模块：用于记录程序运行日志（成功/失败/错误信息）
import logging
# 导入类型注解模块：用于函数参数/返回值的类型提示，提升代码可读性和规范性
from typing import List, Dict, Any, Optional
# 导入时间模块：用于生成时间戳，记录对话的创建时间
from datetime import datetime
# 导入pymongo核心模块：MongoDB原生Python驱动，实现数据库连接和操作
# ASCENDING：表示升序排序，用于MongoDB索引和查询排序
from pymongo import MongoClient, ASCENDING, DESCENDING
# 导入bson的ObjectId：MongoDB默认的主键类型，用于唯一标识文档
from bson import ObjectId
# 导入dotenv模块：用于从.env文件加载环境变量，避免硬编码敏感配置（如MongoDB连接地址）
from dotenv import load_dotenv

# 加载.env文件中的环境变量，使os.getenv能读取到配置
load_dotenv()


class HistoryMongoTool:
    """
    MongoDB 历史对话记录读写工具类 (基于原生 PyMongo 实现)
    核心功能：封装MongoDB的连接、集合初始化、索引创建，为上层提供统一的数据库操作入口
    扩展功能：支持与LangChain消息对象的格式转换（原代码预留能力）
    """
    def __init__(self):
        """
        类初始化方法：完成MongoDB的连接、数据库/集合获取、索引创建
        初始化失败会抛出异常并记录错误日志，确保程序感知连接问题
        """
        try:
            # 从环境变量读取MongoDB连接地址（敏感配置，不硬编码）
            self.mongo_url = os.getenv("MONGO_URL")
            # 从环境变量读取要使用的数据库名称
            self.db_name = os.getenv("MONGO_DB_NAME")

            # 创建MongoDB客户端实例，建立与数据库的连接
            self.client = MongoClient(self.mongo_url)
            # 获取指定名称的数据库对象
            self.db = self.client[self.db_name]
            # 获取对话记录的集合（相当于关系型数据库的表），集合名：chat_message
            self.chat_message = self.db["chat_message"]

            # 为chat_message集合创建复合索引，提升查询性能
            # 索引规则：session_id升序 + ts降序，适配"按会话查最新记录"的核心查询场景
            # create_index自带幂等性：索引已存在时不会重复创建，无需额外判断
            self.chat_message.create_index([("session_id", 1), ("ts", -1)])

            # 记录成功日志，确认数据库连接和初始化完成
            logging.info(f"Successfully connected to MongoDB: {self.db_name}")
        except Exception as e:
            # 捕获所有初始化异常，记录详细错误日志
            logging.error(f"Failed to connect to MongoDB: {e}")
            # 重新抛出异常，让调用方感知初始化失败，避免使用未初始化的实例
            raise


# 定义全局变量：存储HistoryMongoTool的单例实例
# 作用：避免多次创建HistoryMongoTool实例，从而避免重复建立MongoDB连接
_history_mongo_tool = None
# 模块加载时尝试初始化单例实例，实现预加载
# 目的：将数据库连接的初始化提前到模块加载阶段，避免第一次调用接口时才建立连接（提升首次响应速度）
try:
    _history_mongo_tool = HistoryMongoTool()
except Exception as e:
    # 初始化失败时仅记录警告日志，不抛出异常
    # 原因：模块加载阶段的异常可能导致整个程序启动失败，此处保留懒加载兜底（get_history_mongo_tool会再次尝试创建）
    logging.warning(f"Could not initialize HistoryMongoTool on module load: {e}")

def get_history_mongo_tool() -> HistoryMongoTool:
    """
    获取HistoryMongoTool的单例实例（懒加载模式）
    核心逻辑：全局实例为空时创建，不为空时直接返回，保证整个程序只有一个数据库连接实例
    :return: HistoryMongoTool的单例实例
    """
    # 声明使用全局变量，避免函数内视为局部变量
    global _history_mongo_tool
    # 懒加载：仅当全局实例为空时，才创建新的实例
    if _history_mongo_tool is None:
        _history_mongo_tool = HistoryMongoTool()
    # 返回单例实例
    return _history_mongo_tool



def clear_history(session_id: str) -> int:
    """
    清空指定会话的所有历史对话记录
    :param session_id: 会话唯一标识，用于筛选要删除的记录
    :return: 实际删除的文档数量，删除失败返回0
    """
    # 获取全局的HistoryMongoTool实例，使用单例模式避免重复创建数据库连接
    mongo_tool = get_history_mongo_tool()
    try:
        # 执行批量删除操作：删除所有session_id匹配的文档
        result = mongo_tool.chat_message.delete_many({"session_id": session_id})
        # 记录删除成功日志，包含删除数量和会话ID，便于问题排查
        logging.info(f"Deleted {result.deleted_count} messages for session {session_id}")
        # 返回实际删除的数量（delete_many的返回对象包含deleted_count属性）
        return result.deleted_count
    except Exception as e:
        # 捕获删除异常，记录错误日志，包含会话ID
        logging.error(f"Error clearing history for session {session_id}: {e}")
        # 异常时返回0，标识删除失败
        return 0


def save_chat_message(
        session_id: str,
        role: str,
        text: str,
        rewritten_query: str = "",
        item_names: List[str] = None,
        images: List[Dict[str, Any]] = None,
        message_id: str = None
) -> str:
    """
    写入/更新单条会话记录到MongoDB
    支持两种模式：无message_id时新增记录，有message_id时更新已有记录
    :param session_id: 会话唯一标识，关联对话所属的会话
    :param role: 消息角色，固定值：user（用户）/assistant（助手）
    :param text: 对话核心内容，用户的提问或助手的回答
    :param rewritten_query: 重写后的查询语句（可选，用于检索增强等场景，默认空字符串）
    :param item_names: 关联的产品名称列表（可选，支持多产品，默认None）
    :param images: 答案配图 `[{"url": …, "caption": …}]`（可选，默认None）。
        存对象而不是裸 URL 列表，是因为重新打开历史时要靠 **caption** 把图注显示回来
        —— 只存 URL 的话，历史里的每一张图都会变成「未标注来源」
    :param message_id: 记录主键ID（可选，有值则更新，无值则新增）
    :return: 插入/更新的记录唯一标识（新增返回ObjectId字符串，更新返回传入的message_id）

    **更新时不会改动 `ts`**：它是「这条消息在对话里的位置」，只在创建那一刻定死。
    `get_recent_messages` 取上下文时是**按 `_id` 排序**的（`ts` 在本机分辨率极差、
    而且历史上被更新刷坏过，见该函数与 HANDOFF §4.18），但无论如何，
    「先把用户消息存进去、稍后再回来回填改写问题与产品名」这种写法都不该改变消息的位置 ——
    早先就是因为它连 `ts` 一起刷，让「认不出产品、转去询问用户」那条路的历史顺序整个反了。
    要改内容就改内容，别动位置。
    """
    # 生成当前时间的时间戳（秒级）。这是「位置」，只在创建时用，更新路径见上面的说明
    ts = datetime.now().timestamp()

    # 构造要插入/更新的文档数据（MongoDB的基本数据单元是文档，类似Python字典）
    document = {
        "session_id": session_id,  # 会话ID，关联维度
        "role": role,  # 消息角色
        "text": text,  # 消息内容
        "rewritten_query": rewritten_query or "",  # 重写查询，空值处理为空字符串
        "item_names": item_names,  # 关联产品名称列表
        "images": images,  # 答案配图 [{"url", "caption"}]
        "ts": ts  # 时间戳，仅供参考（排序不用它，理由见上）
    }

    # 获取全局的HistoryMongoTool实例，使用单例模式
    mongo_tool = get_history_mongo_tool()
    # 判断是否传入主键ID，区分更新/新增逻辑
    if message_id:
        # 有message_id：执行更新操作（根据主键更新）
        # **ts 必须排除在 $set 之外** —— 否则一次回填就把这条消息挪到对话末尾去了
        result = mongo_tool.chat_message.update_one(
            {"_id": ObjectId(message_id)},  # 更新条件：主键匹配（需将字符串转为ObjectId类型）
            {"$set": {k: v for k, v in document.items() if k != "ts"}}  # 只改内容，不动位置
        )
        # 更新操作返回传入的message_id作为标识
        return message_id
    else:
        # 无message_id：执行新增操作
        result = mongo_tool.chat_message.insert_one(document)
        # 新增操作返回插入的ObjectId并转为字符串，便于上层使用（避免直接返回ObjectId对象）
        return str(result.inserted_id)


def update_message_item_names(ids: List[str], item_names: List[str]) -> int:
    """
    批量更新历史会话记录的关联产品名称
    :param ids: 要更新的记录主键ID列表（字符串类型）
    :param item_names: 要设置的新产品名称列表
    :return: 实际更新的文档数量，更新失败返回0
    """
    # 获取全局的HistoryMongoTool实例，使用单例模式
    mongo_tool = get_history_mongo_tool()
    try:
        # 将字符串类型的主键列表转为MongoDB的ObjectId类型（数据库中主键是ObjectId类型）
        object_ids = [ObjectId(i) for i in ids]
        # 执行批量更新操作
        result = mongo_tool.chat_message.update_many(
            # 更新条件：复合条件，同时满足
            {
                "_id": {"$in": object_ids}# 主键在指定的ID列表中（批量筛选）
            },
            {"$set": {"item_names": item_names}}  # 更新操作：设置新的产品名称列表
        )
        # 记录更新成功日志，包含更新数量和新的产品名称
        logging.info(f"Updated {result.modified_count} records to item_names: {item_names}")
        # 返回实际更新的数量（modified_count：真正被修改的文档数，区别于matched_count）
        return result.modified_count
    except Exception as e:
        # 捕获批量更新异常，记录错误日志
        logging.error(f"Error updating history item_names: {e}")
        # 异常时返回0，标识更新失败
        return 0


def get_recent_messages(session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    查询指定会话的最近N条对话记录，返回原始字典格式
    结果按**插入顺序**（即真实对话顺序）排列，可直接喂给LLM作为上下文。
    排序键是 `_id` 而不是 `ts`，原因见下面那段注释
    :param session_id: 会话唯一标识，用于筛选指定会话的记录
    :param limit: 条数限制，默认返回最近10条
    :return: 对话记录列表（字典格式），查询失败返回空列表
    """
    # 获取全局的HistoryMongoTool实例，使用单例模式
    mongo_tool = get_history_mongo_tool()
    try:
        # 构造查询条件：仅查询指定session_id的记录
        query = {"session_id": session_id}

        # 先按 `_id` 倒序取最新的 limit 条，再反转成插入正序返回。
        # 注意不能直接 sort(ASCENDING).limit(limit)：那样取到的是最早的 limit 条，
        # 对话超过 limit 轮后，喂给 LLM 的会是会话开头而非最近的上下文。
        #
        # **排序键用 `_id`（插入顺序），不用 `ts`** —— 实测证据：
        #   1. 本机 `datetime.now().timestamp()` 分辨率极差（连取 2000 次只有 2 个不同值），
        #      同一轮里连续写入的两条消息经常拿到**完全相同**的 ts
        #      （实测 40 轮「用户提问 + 助手澄清」撞车 4 次，撞车时顺序 100% 是反的）
        #   2. `save_chat_message` 更新时曾连 `ts` 一起刷，被回填过的那条会跳到对话末尾
        #      （见该函数的说明）。**库里已经写坏的数据就是这个后果** ——
        #      改之前 14 个会话里有 9 个按 ts 读出来是错的（对话以助手消息开场）
        #   3. ObjectId 在同一进程内单调递增，**就是真实插入顺序**，而插入顺序在本项目里
        #      就等于对话顺序（用户提问先落库、助手回复后落库，澄清与兜底答复同理）
        # 改成 `_id` 之后 14 个会话全部读对，**包括 ts 已经被写坏的历史数据、无需迁移**。
        cursor = (
            mongo_tool.chat_message.find(query)
            .sort("_id", DESCENDING)
            .limit(limit)
        )
        messages = list(cursor)
        messages.reverse()   # 反转回时间正序，适配 LLM 上下文顺序
        return messages
    except Exception as e:
        # 捕获查询异常，记录错误日志
        logging.error(f"Error getting recent messages: {e}")
        # 异常时返回空列表，避免上层处理None报错
        return []


def list_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    """
    列出所有会话，按**最近活跃**倒序 —— 给前端的左侧会话栏用

    刻意**不用聚合管道**：本机 `chat_message` 只有几百条、十几个会话，全扫一遍的代价
    可以忽略；而「取每个会话的第一条 user 消息当标题」用管道反而更绕 ——
    `$first` 取的是分组内第一条**文档**的值，不是第一条*满足条件*的文档，
    想表达「第一条 user 消息」得先 `$match` 再 `$group`，写错一处就是静默取到错标题。
    等消息量真上来了（几十万条）再换管道 + `(session_id, _id)` 索引不迟。

    :param limit: 最多返回多少个会话（按最近活跃取前 N 个）
    :return: `[{"session_id", "title", "count", "last_at"}]`
        - `title`：该会话**第一条 user 消息**，超 30 字截断；一条 user 消息都没有时回退成 session_id
        - `last_at`：最后一条消息的时间，取自 **ObjectId 内嵌的时间戳**（ISO 字符串）。
          刻意不用 `ts` —— 本机 `datetime.now().timestamp()` 分辨率极差、且历史上被更新刷坏过，
          不可信（见 HANDOFF §4.18）；ObjectId 的生成时间没有这两个毛病
        - `count`：消息条数
    """
    mongo_tool = get_history_mongo_tool()
    try:
        # 按 _id 升序扫一遍：于是同一会话内「先遇到的」= 先写入的 = 对话里更早的
        cursor = mongo_tool.chat_message.find(
            {}, {"session_id": 1, "role": 1, "text": 1}
        ).sort("_id", 1)

        agg: Dict[str, Dict[str, Any]] = {}
        for d in cursor:
            sid = d.get("session_id") or ""
            if not sid:
                continue
            item = agg.setdefault(sid, {
                "session_id": sid, "title": "", "count": 0,
                "last_at": None, "_last_id": None,
            })
            item["count"] += 1
            item["_last_id"] = d["_id"]
            item["last_at"] = d["_id"].generation_time
            # 标题只认**第一条** user 消息；已经有标题就不再覆盖
            if not item["title"] and d.get("role") == "user":
                item["title"] = (d.get("text") or "").strip()

        items = sorted(agg.values(), key=lambda x: x["_last_id"], reverse=True)[:limit]
        for it in items:
            it.pop("_last_id", None)
            title = it["title"]
            it["title"] = (title[:30] + "…") if len(title) > 30 else (title or it["session_id"])
            it["last_at"] = it["last_at"].isoformat() if it["last_at"] else ""
        return items
    except Exception as e:
        # 与 get_recent_messages 一致：查询失败返回空列表，让上层照常渲染空状态
        logging.error("Error listing sessions: %s", e)
        return []


# 主程序入口：仅当直接运行该脚本时执行
if __name__ == "__main__":
    """
    自测：写入/查询的基本通路，外加一条**顺序回归用例**

    回归用例复现的是「认不出产品、转去询问用户」那条路的真实写入次序：
    先存用户提问 → 存助手那句澄清问题 → 再回填用户提问的改写结果。
    第 3 步是**更新**，更新一旦连 `ts` 一起刷，用户提问就会被挪到澄清问题之后，
    历史顺序整个反掉（2026-10-06 实测）。
    """
    import time

    sid = f"selftest_history_{int(time.time())}"
    problems = []

    # 1. 基本通路：写一条、读回来
    uid = save_chat_message(sid, "user", "你好（自测）")
    msgs = get_recent_messages(sid, limit=5)
    if len(msgs) != 1 or msgs[0].get("text") != "你好（自测）":
        problems.append(f"基本写入/读取不对：{msgs}")

    # 2. 顺序回归：更新老消息不该改变它的位置
    time.sleep(0.05)   # 拉开时间，确保「若刷新 ts 就会乱序」
    save_chat_message(sid, "assistant", "「自测」没能锁定到具体产品，你想问的是下面哪一个？")
    save_chat_message(sid, "user", "你好（自测）", rewritten_query="你好（改写后）", message_id=uid)

    msgs = get_recent_messages(sid, limit=10)
    order = [m.get("role") for m in msgs]
    if order != ["user", "assistant"]:
        problems.append(f"更新后历史顺序反了：{order}，应为 ['user', 'assistant']")
    # 别为了不动 ts 把内容也一起漏掉
    if msgs and msgs[0].get("rewritten_query") != "你好（改写后）":
        problems.append(f"更新没写进内容：rewritten_query={msgs[0].get('rewritten_query')!r}")

    # 3. ts 不可信时仍按**插入顺序**返回（排序键必须是 _id）
    #    本机 datetime.now().timestamp() 连取 2000 次只有 2 个不同值，而且历史上
    #    更新还会把 ts 刷掉 —— 库里 9 个会话的历史就是这么被写坏的。这里直接造
    #    「ts 与插入顺序相反」的记录，把「不能拿 ts 当排序键」钉死
    tie_sid = sid + "_tie"
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
        problems.append(f"ts 与插入顺序矛盾时没按插入顺序返回：{tie_order}")

    # 4. 配图连同**图注**存进去、原样读回来
    #    只存 URL 的话，重新打开历史时每张图都会变成「未标注来源」
    img_sid = sid + "_img"
    _imgs = [{"url": "http://example.invalid/a.jpg", "caption": "打开支架盖"},
             {"url": "http://example.invalid/b.jpg", "caption": "插入烫金膜盒"}]
    save_chat_message(img_sid, "user", "怎么装？")
    save_chat_message(img_sid, "assistant", "分两步。", images=_imgs)
    got_imgs = get_recent_messages(img_sid, limit=10)[-1].get("images")
    if got_imgs != _imgs:
        problems.append(f"配图没原样读回：{got_imgs!r}")

    # 5. list_sessions：标题取第一条 user 消息、按最近活跃倒序、count 正确
    b_sid = sid + "_b"
    save_chat_message(b_sid, "user", "B 会话的第一句提问")
    save_chat_message(b_sid, "assistant", "B 的回答")          # b 比 sid 晚写 → 应排在前面
    listed = list_sessions(limit=200)
    by_sid = {s["session_id"]: s for s in listed}
    for want_sid in (sid, img_sid, b_sid):
        if want_sid not in by_sid:
            problems.append(f"list_sessions 没列出会话 {want_sid}")
    if img_sid in by_sid:
        if by_sid[img_sid]["title"] != "怎么装？":
            problems.append(f"标题没有取第一条 user 消息：{by_sid[img_sid]['title']!r}")
        if by_sid[img_sid]["count"] != 2:
            problems.append(f"count 不对：{by_sid[img_sid]['count']}")
        if not by_sid[img_sid]["last_at"]:
            problems.append("last_at 为空")
    if sid in by_sid and b_sid in by_sid:
        order = [s["session_id"] for s in listed]
        if order.index(b_sid) > order.index(sid):
            problems.append("list_sessions 没有按最近活跃倒序（后写的应排前面）")

    # 自测数据不该留在用户的库里
    get_history_mongo_tool().chat_message.delete_many(
        {"session_id": {"$in": [sid, tie_sid, img_sid, b_sid]}})

    for p in problems:
        logging.error("[测试] [FAIL] %s", p)
    if not problems:
        print("[测试] [PASS] 历史读写、顺序、配图往返、会话列表用例通过")
