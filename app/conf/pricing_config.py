"""
模型计价表（估算调用成本用）

**单位：元 / 百万 tokens**，取自阿里云百炼官方价目表（华北2·北京，按量付费）；
deepseek-flash 取 DeepSeek 官方定价页（主 agent 直连官方 API、不走百炼）。
来源与查询日期写在每条价格后面，改了价格记得一起改这一行。

**价格只是快照，不是事实**：账本里同时记了 tokens，所以单价变了随时能按新价重算
（`app/clients/mongo_usage_utils.py` 的报表就是现算的，不读历史成本字段）——
tokens 才是原始事实，成本是派生值。

四处刻意的简化，都写在明面上而不是藏着：

- **qwen-plus 按上下文长度阶梯计价**（≤128k: 0.8/2；128k~256k: 2.4/20；256k~1m: 4.8/48）。
  本项目单次请求的上下文是若干切片，远小于 128k，因此统一按最低档计。
  真要处理超长上下文，得把 `PRICE_PER_MILLION` 换成按输入区间分档的结构。
- **deepseek-flash 分 高峰/空闲 × 缓存命中/未命中 四档**（2026-10-14 官方定价页：
  输入命中 0.04/0.02、输入未命中 2/1、输出 8/4）。高峰 = 北京时间周一至周五
  9:00-12:00、14:00-18:00，其余（含周末）空闲。两处口径：
  - 高峰判定按**本机时间**（本机即北京时区）；**中国法定节假日未建模** ——
    节假日落在高峰窗口时按高峰计价（一年十几天、误差方向偏高，记档）
  - 缓存命中 tokens 取 API usage 的 `input_token_details.cache_read`；拿不到时
    全程按未命中计（偏保守）。推理模型的 reasoning tokens 由 API 计入 completion，
    按输出价计、无需特判
- **联网搜索（百炼 MCP EnhancedSearch）按次计费**，单价未见于公开文档，
  故账本里只记调用次数、不计成本（`estimate_cost` 返回 None）。
- **MinerU 解析按页数配额计费**，与 token 无关，不在这张表里。
"""
from datetime import datetime
from typing import Optional

# 型号 → 价格档位。
# 基础型：{input, output}（每百万 token 单价；仅输入计费的模型 output 记 0）。
# deepseek-flash 多出峰谷/缓存字段：input_offpeak / input_cached(_offpeak) / output_offpeak
PRICE_PER_MILLION = {
    # 百炼文档：qwen-plus（2026-10 查询）
    "qwen-plus": {"input": 0.8, "output": 2.0},
    # DeepSeek 官方定价页（2026-10-14）：deepseek-flash = V4.1-Flash 的官方现行名
    "deepseek-flash": {
        "input": 2.0, "input_offpeak": 1.0,
        "input_cached": 0.04, "input_cached_offpeak": 0.02,
        "output": 8.0, "output_offpeak": 4.0,
    },
    # 百炼文档：qwen3-vl-flash（2026-10 查询）
    "qwen3-vl-flash": {"input": 0.15, "output": 1.5},
    # 百炼文档：text-embedding-v2，0.0007 元/千 token（2026-10 查询）
    "text-embedding-v2": {"input": 0.7, "output": 0.0},
    # 百炼文档：gte-rerank-v2，0.8 元/百万 tokens（2026-10 查询）
    "gte-rerank-v2": {"input": 0.8, "output": 0.0},
}


def _lookup(model: str) -> Optional[dict]:
    """
    查单价，兼容带日期后缀的型号名

    DashScope 既接受 `qwen-plus` 也接受 `qwen-plus-2025-04-28` 这类带日期的快照名，
    两者同价，所以先精确匹配、再去掉日期后缀匹配。
    """
    if not model:
        return None
    if model in PRICE_PER_MILLION:
        return PRICE_PER_MILLION[model]

    # 去掉形如 -2025-04-28 的日期后缀再试一次
    parts = model.rsplit("-", 3)
    if len(parts) == 4 and all(p.isdigit() for p in parts[1:]):
        return PRICE_PER_MILLION.get(parts[0])
    return None


def _is_peak(at: Optional[datetime] = None) -> bool:
    """deepseek-flash 的高峰时段判定（北京时间；法定节假日未建模，见模块说明）"""
    at = at or datetime.now()
    if at.weekday() >= 5:                      # 周六日全天空闲
        return False
    return (9 <= at.hour < 12) or (14 <= at.hour < 18)


def estimate_cost(model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                  cache_hit_tokens: int = 0,
                  at: Optional[datetime] = None) -> Optional[float]:
    """
    按 tokens 估算一次调用的成本（元）

    :param model: 模型名
    :param prompt_tokens: 输入 tokens（**含**缓存命中的部分）
    :param completion_tokens: 输出 tokens（推理模型的 reasoning 也计在这里）
    :param cache_hit_tokens: 其中命中上下文缓存的输入 tokens（默认 0 = 全程未命中）
    :param at: 计价时刻，用来判高峰/空闲；默认现在。报表重算时传账目的原始时间
    :return: 估算成本（元）；型号不在表里时返回 None —— 宁可留空，也不要编一个数
    """
    price = _lookup(model)
    if not price:
        return None

    peak = _is_peak(at)
    if "output_offpeak" in price:
        out_rate = price["output"] if peak else price["output_offpeak"]
    else:
        out_rate = price["output"]

    hit = max(0, min(int(cache_hit_tokens or 0), int(prompt_tokens or 0)))
    miss = int(prompt_tokens or 0) - hit
    if "input_cached" in price:
        hit_rate = price["input_cached"] if peak else price["input_cached_offpeak"]
        miss_rate = price["input"] if peak else price["input_offpeak"]
    else:
        hit_rate = miss_rate = price["input"]

    cost = (hit * hit_rate + miss * miss_rate
            + int(completion_tokens or 0) * out_rate) / 1_000_000
    return round(cost, 8)


def _check_pricing_table() -> list:
    """
    离线自测：计价与型号名兼容 + deepseek-flash 的四档计价（纯逻辑）

    时间戳钉死在工作日 10:00（高峰）与周六 10:00 / 工作日 12:30（空闲）——
    不随运行时刻忽高忽低。**2026-10-14 是周三、2026-10-17 是周六**，
    改测试日期前先核对星期。

    :return: 问题描述列表，空表示通过
    """
    problems = []

    # ① 型号兼容：已知型号算得出成本、带日期后缀同价、未知型号返回 None
    compat = [
        ("qwen-plus", 3000, 500, True),
        ("qwen-plus-2025-04-28", 3000, 500, True),
        ("deepseek-flash", 3000, 500, True),
        ("text-embedding-v2", 10000, 0, True),
        ("gte-rerank-v2", 2000, 0, True),
        ("some-unknown-model", 100, 100, False),
    ]
    for model, pin, pout, should in compat:
        cost = estimate_cost(model, pin, pout)
        if (cost is not None) != should:
            problems.append(f"{model}：成本应{'有' if should else '无'}，实得 {cost}")

    # ② 四档计价（期望值手算；命中价必须比未命中价便宜，防两档接反）
    peak_at = datetime(2026, 10, 14, 10, 0)     # 周三上午 → 高峰
    off_at = datetime(2026, 10, 17, 10, 0)      # 周六上午 → 空闲
    noon_at = datetime(2026, 10, 14, 12, 30)    # 周三午休 → 空闲
    quad = [
        (peak_at, 0,    3000, 500, (3000 * 2.0 + 500 * 8.0) / 1e6, "高峰·未命中"),
        (off_at,  0,    3000, 500, (3000 * 1.0 + 500 * 4.0) / 1e6, "空闲·未命中"),
        (off_at,  2000, 3000, 500,
         (2000 * 0.02 + 1000 * 1.0 + 500 * 4.0) / 1e6, "空闲·部分命中"),
        (peak_at, 3000, 3000, 500,
         (3000 * 0.04 + 500 * 8.0) / 1e6, "高峰·全命中"),
        (noon_at, 0,    1000, 0,   (1000 * 1.0) / 1e6, "午休=空闲"),
    ]
    for at, hit, pin, pout, want, label in quad:
        got = estimate_cost("deepseek-flash", pin, pout, hit, at=at)
        if got is None or abs(got - want) > 1e-9:
            problems.append(f"{label}：期望 {want}，实得 {got}")

    # ③ 命中数比输入还大时按输入封顶（防御脏数据，不该算成负的未命中）
    got = estimate_cost("deepseek-flash", 100, 0, 99999, at=off_at)
    if got is None or abs(got - (100 * 0.02) / 1e6) > 1e-9:
        problems.append(f"缓存命中数超过输入时没有封顶：{got}")
    return problems


if __name__ == '__main__':
    """自测：计价与型号名兼容、deepseek-flash 四档（纯逻辑、不依赖外部服务）"""
    from app.core.logger import logger

    _problems = _check_pricing_table()
    for _p in _problems:
        logger.error(f"[测试] [FAIL] {_p}")
    if not _problems:
        logger.success("[测试] [PASS] 计价表验证通过")
