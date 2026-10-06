"""
项目日志工具类

基于 loguru，三种输出各司其职。**同一条日志记录**经过补丁后同时喂给三者，
所以「控制台看到的」和「JSON 文件里的」永远是同一份事实，不会各记各的。

| 输出 | 格式 | 给谁看 |
|---|---|---|
| 控制台 | 彩色人读，前面挂一个 `[trace·node]` 标签 | 开发时盯着看 |
| `logs/app_YYYYMMDD.log` | 同上但不带颜色 | `grep` / 翻历史 |
| `logs/app_YYYYMMDD.jsonl` | 每行一个 JSON 对象 | 程序/jq 查询、将来接采集 |

**结构化字段来自 `request_context`**：`trace_id` / `tenant_id` / `session_id` / `node`
由请求入口与图节点包装器写进 ContextVar，日志补丁自动附着到每一条记录上——
**业务代码一行都不用改**，现有的 `logger.info(...)` 直接就有了可关联的上下文。

三个刻意的取舍：

- **控制台只显示 `trace_id` 前 8 位 + 节点名**，不显示完整 trace 与租户：
  它是给人扫视的，塞满 32 位哈希反而看不清。完整字段在 JSONL 里，一个不缺。
- **文本日志保留**，没有换成 JSONL 就删掉：出问题时人肉读 JSON 很难受，
  两种读者要两种格式，代价只是多写一个文件（本项目日志量很小）。
- **补丁只走一次调用栈**：定位真实调用位置要遍历栈，加输出不加遍历次数，
  两个效果合在一个补丁里完成。

配置见 `.env`：`LOG_CONSOLE_*` / `LOG_FILE_*` / `LOG_JSON_*`。
"""
import inspect
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from loguru import logger

from app.core.request_context import context_fields


# -------------------------- 第一步：加载.env配置文件 --------------------------
load_dotenv()

# -------------------------- 第二步：读取.env配置（带默认值，防止配置缺失） --------------------------
LOG_CONSOLE_ENABLE = os.getenv("LOG_CONSOLE_ENABLE", "True").lower() == "true"
LOG_CONSOLE_LEVEL = os.getenv("LOG_CONSOLE_LEVEL", "INFO").upper()
LOG_FILE_ENABLE = os.getenv("LOG_FILE_ENABLE", "True").lower() == "true"
LOG_FILE_LEVEL = os.getenv("LOG_FILE_LEVEL", "INFO").upper()
LOG_FILE_RETENTION = os.getenv("LOG_FILE_RETENTION", "7 days")
# JSONL：给机器读的那份，可以单独开关（比如只想看人读日志时关掉）
LOG_JSON_ENABLE = os.getenv("LOG_JSON_ENABLE", "True").lower() == "true"
LOG_JSON_LEVEL = os.getenv("LOG_JSON_LEVEL", "INFO").upper()

# -------------------------- 第三步：定义日志路径（自动推导项目根） --------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE_NAME = "app_{time:YYYYMMDD}.log"
LOG_FILE_PATH = LOG_DIR / LOG_FILE_NAME
LOG_JSON_NAME = "app_{time:YYYYMMDD}.jsonl"
LOG_JSON_PATH = LOG_DIR / LOG_JSON_NAME

# -------------------------- 第四步：定义日志格式（彩色、结构化、易读） --------------------------
# {ctx} 是补丁算好的上下文标签，形如 `[9b2063cb·node_rerank] `；不在请求里时为空串，
# 所以命令行单跑节点不会多出一段空白
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{ctx}</cyan>"
    "<cyan>{name: <20}</cyan>:<cyan>{function: <15}</cyan>:<cyan>{line: <4}</cyan> - "
    "<level>{message}</level>"
)
# 文件版去掉颜色标签
FILE_FORMAT = LOG_FORMAT.replace("<green>", "").replace("</green>", "") \
    .replace("<level>", "").replace("</level>", "") \
    .replace("<cyan>", "").replace("</cyan>", "")

# JSONL 里每行包含的字段。改这里等于改日志表结构，加字段前先想想下游怎么消费
JSON_FIELDS = ("trace_id", "tenant_id", "session_id", "node")


# 行首形如 `[xxx]` 的标记，`[xxx]` 内部不含 `]`（避免贪婪吃掉正文）
_LEADING_BRACKET_RE = re.compile(r"^\[([^\]]+)\]\s*")


def _strip_redundant_prefix(message: str, node: str, function: str) -> str:
    """
    去掉消息开头与「节点名」或「函数名」重复的 `[xxx]`

    节点代码沿用了 `logger.info(f"[{NODE_NAME}] [{function_name}] ...")` 的写法，
    而在**入口函数**里这两个名字恰好相同，于是消息自己就先重复了一遍；
    再加上行首标签与 `module:function`，一整行里节点名能出现四次。实测 41% 的图内日志如此。

    | 改前 | 改后 |
    |---|---|
    | `[node_rerank] [node_rerank] 开始处理` | `开始处理` |
    | `[node_rerank] [step_3_topk] 触发断崖截断…` | `触发断崖截断…` |
    | `[记账] llm …` | 原样保留（`记账` 不是节点名也不是函数名） |

    **在补丁里收口而不是去改 230 处 f-string**：一处生效、新节点自动受益，
    而且节点代码不必为了日志好看而扭曲写法。代价是消息与源码里的字面量不再逐字相同——
    这个函数就是为此存在的，别把它「优化」掉。

    只剥**开头连续**的 `[节点名]` / `[函数名]`，正文中间出现的方括号一律不动
    （切片标题、`[图片]` 之类的正文内容不能被误伤）。
    """
    redundant = {x for x in (node, function) if x}
    if not redundant:
        return message

    while True:
        matched = _LEADING_BRACKET_RE.match(message)
        if not matched or matched.group(1) not in redundant:
            return message
        message = message[matched.end():].lstrip()


def _build_json_line(record: Dict[str, Any]) -> str:
    """
    把一条记录渲染成单行 JSON（含结尾换行）

    这是「结构化日志」的落点：字段名固定、值可空但不会缺键，
    可以直接 `jq 'select(.node=="node_rerank") | .message'` 这样查。
    """
    item = {
        "ts": record["time"].isoformat(),
        "level": record["level"].name,
        "module": record["name"],
        "function": record["function"],
        "line": record["line"],
        "message": record["message"],
    }
    # 归因字段：一定有这几个键（值为空串表示不在请求里），便于下游写固定查询
    for field in JSON_FIELDS:
        item[field] = record.get(field, "")

    # 业务自己 bind 的字段（loguru 把 logger.info(..., key=value) 的 kwargs 放这里）
    extra = record.get("extra") or {}
    if extra:
        item["extra"] = extra

    # 异常单独一列，别混在 message 里——否则 message 长度不可控、还会带换行
    exception = record.get("exception")
    if exception:
        item["exception"] = {
            "type": exception.type.__name__ if exception.type else "",
            "message": str(exception.value) if exception.value else "",
            "traceback": "".join(
                traceback.format_exception(exception.type, exception.value, exception.traceback)
            ).strip(),
        }

    # default=str 兜底：extra 里可能出现非 JSON 类型（对象、路径），宁可转成字符串也别丢日志。
    # 换行自己加：字符串 format loguru 会自动补，可调用 format 不会。
    return json.dumps(item, ensure_ascii=False, default=str) + "\n"


def _json_format(record: Dict[str, Any]) -> str:
    """
    JSONL sink 的 format：把补丁预先渲染好的那一行原样吐出去

    模板里**只有 `{jsonl}` 一个占位符、不含任何内容**，这是有意的。
    loguru 会把可调用 format 的返回值**再当模板解析一遍**——既跑 `format_map`，
    又按 `<tag>` 解析颜色标记。内容一旦进了模板，就会连踩两个坑：

    - JSON 的花括号被当字段名 → `KeyError: '"ts"'`
    - 正文里的 `<frozen runpy>`、`<class 'ValueError'>` 被当颜色标签 →
      `ValueError: Tag "<module>" does not correspond to any known color directive`

    把内容留在**值**里就没这个问题：`format_map` 只把值当字符串替换，不再解析。
    模板恒定还顺带让 loguru 的格式缓存（`lru_cache(maxsize=64)`）一直命中。
    """
    return "{jsonl}"


# -------------------------- 第五步：记录补丁（定位调用位置 + 附着上下文） --------------------------
def enrich_record(record):
    """
    给每条记录补两样东西

    1. **真实调用位置**：穿透 loguru 内部帧与本文件，找到业务代码的位置
    2. **请求上下文**：从 ContextVar 取 `trace_id` / `tenant_id` / `session_id` / `node`
       写成记录的顶层字段（JSONL 直接输出），并合成一个给人看的短标签 `ctx`

    两个效果合在一个补丁里，是为了只遍历一次调用栈（`inspect.stack()` 不便宜，
    而这个补丁每条日志都要跑）。
    """
    # --- 1. 调用位置 ---
    for frame in inspect.stack():
        # 过滤掉两类「代为记录」的帧，让位置落到真正的业务代码上：
        # - loguru 内部与 logger.py 自身
        # - error_policy.py：它的 degrade() 是替调用方记的日志，指到它没有意义
        if (("_logger.py" in frame.filename or frame.function == "_log")
                or "logger.py" in frame.filename
                or "error_policy.py" in frame.filename):
            continue
        record.update(
            name=frame.filename.split("/")[-1].split("\\")[-1],
            function=frame.function,
            line=frame.lineno,
        )
        break

    # --- 2. 请求上下文 ---
    try:
        fields = context_fields()
    except Exception:
        # 上下文坏了也不能让日志本身失败——日志是排查问题的最后一道保障
        fields = {k: "" for k in JSON_FIELDS}

    record.update(fields)

    # --- 2.5 去掉消息开头重复的 [节点名] / [函数名]（此时位置已定，才取得到函数名）---
    record["message"] = _strip_redundant_prefix(
        record["message"], fields["node"], record.get("function") or ""
    )

    # 控制台标签：trace 只取前 8 位（32 位十六进制没人会去读），后面接节点名
    parts = [fields["trace_id"][:8], fields["node"]]
    label = "·".join(p for p in parts if p)
    record["ctx"] = f"[{label}] " if label else ""

    # --- 3. 预渲染 JSONL ---
    # 放在补丁里而不是 format 里，原因见 _json_format 的说明。
    # 关掉 JSONL 时不做，省掉每条日志一次 json.dumps
    if LOG_JSON_ENABLE:
        try:
            record["jsonl"] = _build_json_line(record)
        except Exception:
            # 渲染失败也不能丢日志：退化成一行说明，至少还能看到这条记录存在
            record["jsonl"] = json.dumps(
                {"ts": record["time"].isoformat(), "level": record["level"].name,
                 "message": record["message"], "error": "结构化渲染失败"},
                ensure_ascii=False,
            ) + "\n"


# -------------------------- 第六步：初始化日志配置（核心方法） --------------------------
def init_logger():
    """
    初始化全局日志配置
    1. 移除loguru默认控制台输出（避免重复打印）
    2. 根据.env配置开启/关闭控制台输出
    3. 根据.env配置开启/关闭文件输出（自动创建logs文件夹）
    4. 配置日志格式、级别、分割、保留策略
    :return: 配置完成的loguru logger实例
    """
    # 1. 移除loguru默认的控制台输出
    logger.remove()

    # 2. 配置控制台输出（若.env开启）
    if LOG_CONSOLE_ENABLE:
        # Windows 控制台默认编码是 GBK，遇到 MinerU 解析出的私有区符号（如 项目符号）
        # 或 emoji 会抛 UnicodeEncodeError，导致整条日志写不出去。这里与文件 sink 保持 utf-8 一致
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            # 部分被重定向的流不支持 reconfigure，忽略即可，不影响文件日志
            pass
        logger.add(
            sink=sys.stdout,
            level=LOG_CONSOLE_LEVEL,
            format=LOG_FORMAT,
            colorize=True,
            enqueue=True
        )

    # 3. 配置文本文件输出（若.env开启）——给人 grep 的
    if LOG_FILE_ENABLE:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(
            sink=LOG_FILE_PATH,
            level=LOG_FILE_LEVEL,
            format=FILE_FORMAT,
            rotation="00:00",
            retention=LOG_FILE_RETENTION,
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=True
        )

    # 4. 配置 JSONL 输出（若.env开启）——给机器查的
    if LOG_JSON_ENABLE:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(
            sink=LOG_JSON_PATH,
            level=LOG_JSON_LEVEL,
            format=_json_format,
            rotation="00:00",
            retention=LOG_FILE_RETENTION,
            encoding="utf-8",
            enqueue=True
        )

    return logger


# -------------------------- 第六步半：SafeLogger 代理 --------------------------
# loguru 的 level 方法（info / error ……）。只有这几个被代理接管，其余属性照旧透传
_LEVELS = ("trace", "debug", "info", "success", "warning", "error", "critical", "exception")


class SafeLogger:
    """
    loguru 的薄代理，**保证不往 loguru 传任何 kwarg**

    为什么需要（2026-10-06 实测踩到，是个真事故，不是洁癖）：loguru 的 `_logger._log()` 里有

        if args or kwargs:
            log_record["message"] = message.format(*args, **kwargs)

    即**只要传了任意 kwarg（`exc_info=True` 也算）**，它就会拿消息去跑一遍 `str.format`。
    而我们的消息里常嵌着外部服务返回的 JSON 原文，例如 DashScope 的报错体
    `{'error': {'code': 'data_inspection_failed'}}` —— `str.format` 把 `{'error': …}`
    当成替换字段去找名叫 `'error'` 的键，当场 `KeyError: "'error'"`。

    真正要命的是它炸在**错误处理自己的那行日志**上：`error_policy.degrade()` 里的
    `logger.error(..., exc_info=True, degraded=True)` 抛出异常，把本来要记的那个故障顶掉，
    整轮请求跟着失败。实测表现是用户的提问报成「检索图执行失败："'error'"」，
    而真实原因（内容审核拦截）在日志里一个字都没有。

    所以这里把两类 kwarg 分流（分流后调用写法一行都不用改）：

    | 原来写法 | 代理转成 | 为什么 |
    |---|---|---|
    | `logger.error(msg, exc_info=True)` | `logger.opt(exception=True).error(msg)` | loguru 的正式开关，不进 kwargs |
    | `logger.warning(msg, degraded=True, kind=…)` | `logger.bind(degraded=True, kind=…).warning(msg)` | 照旧进 `extra`，JSONL 里的字段不变 |

    顺带修掉一个小毛病：`exc_info=True` 以前会被 loguru 塞进 `extra`
    （JSONL 里能看到 `"extra": {"exc_info": true}`），现在不会了。
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        self._inner = inner

    # bind / opt 是链式的，**必须也回代理**：
    # 否则 `logger.bind(...).error(msg, exc_info=True)` 后半截又落到裸 loguru 上，照样炸
    def bind(self, **kwargs) -> "SafeLogger":
        return SafeLogger(self._inner.bind(**kwargs))

    def opt(self, **kwargs) -> "SafeLogger":
        return SafeLogger(self._inner.opt(**kwargs))

    def _emit(self, level: str, message, args: tuple, kwargs: dict):
        exc_info = kwargs.pop("exc_info", None)
        inner = self._inner
        if exc_info:
            # exc_info 允许是 True（用当前异常）或 (type, value, tb) 三元组，loguru 两种都收
            inner = inner.opt(exception=True if exc_info is True else exc_info)
        if kwargs:
            inner = inner.bind(**kwargs)
        # 只把 message 与位置参数传下去。kwargs 已清空 ⇒ loguru 不走 format 分支，
        # 消息里的花括号从此只是普通字符 —— 这正是整个代理存在的理由，别把它"简化"回去
        return getattr(inner, level)(message, *args)

    def __getattr__(self, name):
        # add / remove / patch / configure / level 之类照旧透传给真 logger
        if name in _LEVELS:
            return lambda message, *args, **kwargs: self._emit(name, message, args, kwargs)
        return getattr(self._inner, name)


# -------------------------- 第七步：初始化并导出全局logger --------------------------
base_logger = init_logger()

# 应用补丁，再套代理：补丁负责归因与结构化，代理负责挡掉 loguru 的消息格式化
logger = SafeLogger(base_logger.patch(enrich_record))


# -------------------------- 测试代码（验证修复效果与结构化字段） --------------------------
if __name__ == '__main__':
    from app.core.request_context import bind_context, reset_context

    # 先跑离线的消息去重用例：最容易过火的地方是「把正文里的方括号也剥掉」
    s = _strip_redundant_prefix
    _cases = [
        ("[node_x] [node_x] 开始处理", "node_x", "node_x", "开始处理"),
        ("[node_rerank] [step_3_topk] 触发断崖截断", "node_rerank", "step_3_topk", "触发断崖截断"),
        ("[node_x] 合并输入：10 条", "node_x", "node_x", "合并输入：10 条"),
        # 不是节点名也不是函数名 → 原样保留
        ("[记账] llm tokens=1+2", "node_x", "record", "[记账] llm tokens=1+2"),
        ("[LLM客户端] 开始初始化", "node_x", "get_llm_client", "[LLM客户端] 开始初始化"),
        # 正文中间的方括号不能被误伤
        ("答案如下 [重要] 请注意", "node_x", "node_x", "答案如下 [重要] 请注意"),
        # 无上下文（命令行单跑）→ 原样
        ("[node_x] 开始处理", "", "", "[node_x] 开始处理"),
    ]
    _bad = [(m, s(m, n, f), w) for m, n, f, w in _cases if s(m, n, f) != w]
    for _m, _got, _want in _bad:
        logger.error(f"[测试] [FAIL] 去重错误：{_m!r} -> {_got!r}，应为 {_want!r}")
    if not _bad:
        logger.success(f"[测试] [PASS] 消息去重 {len(_cases)} 个用例通过")

    logger.info("【测试】不在请求里的日志：ctx 标签应为空")
    token = bind_context(trace_id="9b2063cb6de44084", tenant_id="t_demo", node="node_rerank")
    logger.info("【测试】请求内的日志：应带上 [9b2063cb·node_rerank] 标签")
    logger.bind(biz="test").info("【测试】带 extra 的日志")
    try:
        raise ValueError("故意的异常，用于验证 exception 字段")
    except ValueError:
        logger.exception("【测试】异常日志")
    reset_context(token)

    # 事故复现式：消息里嵌外部服务的 JSON 原文（带花括号）+ 任意 kwarg。
    # 改前必炸 KeyError: "'error'"，且炸的是错误处理自己那行日志。
    _brace = "降级继续：BadRequestError: 400 - {'error': {'code': 'data_inspection_failed'}}"
    try:
        logger.error(_brace, exc_info=True, degraded=True, kind="blocked")
        logger.bind(degraded=True).warning(_brace, exc_info=True)
    except KeyError as e:
        logger.error(f"[测试] [FAIL] 消息含花括号 + kwargs 仍抛 KeyError：{e}")
    else:
        logger.success("[测试] [PASS] 消息含花括号 + kwargs 不再抛 KeyError")

    print(f"\n文本日志：{LOG_FILE_PATH}")
    print(f"结构化日志：{LOG_JSON_PATH}")
