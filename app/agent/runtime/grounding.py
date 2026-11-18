"""
终稿接地校验：事实台账 + 高风险 token 确定性核对 + LLM 校验/一次修复

用户看到的是主 agent 的转述稿。子 agent 即使 grounded，主 agent 在转述时仍可能
补上工具没给的价格、型号、外观、步骤顺序。本模块在 `_finalize_turn` 里做最后一道闸：

1. **确定性核对**（零模型调用）：回复里的价格、订单/物流号、型号必须能在本轮
   工具返回或用户原话里找到；不通过才进入 LLM 校验。
2. **LLM 校验**（flash）：判断哪些句子在台账中无依据；只有台账里存在实质工具返回、
   且回复达到长度阈值时才调用，避免每轮都加延迟。
3. **一次修复**：只基于台账重写，删除/降级无依据内容；不得新增事实。
4. **失败 fail-open**：校验/修复异常时保留原文，只记录 metadata；不阻断主流程。

它只负责“终稿是否可追溯”，不替代 RAG 生成侧的引用约束与离线 judge。
"""
import json
import re
import unicodedata
from typing import Iterable

from langchain_core.messages import HumanMessage

from app.conf.grounding_config import grounding_config
from app.conf.lm_config import lm_config
from app.core.budget import remaining_wall_clock
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.core.retry import invoke_with_retry
from app.lm.lm_utils import get_llm_client

NODE_NAME = "grounding"

MONEY_RE = re.compile(
    r"(?:¥|￥)\s*([0-9][0-9,]*(?:\.[0-9]+)?)|([0-9][0-9,]*(?:\.[0-9]+)?)\s*元")
# 10 位以上的字母数字串（至少含一个数字）：订单号、物流单号、哈希类 id
LONG_ID_RE = re.compile(r"(?<![A-Za-z0-9-])(?=[A-Za-z0-9-]{10,}(?![A-Za-z0-9-]))"
                        r"(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])")
# 型号类：至少有 2 个字母 + 2 位数字，如 HAK 180 / GS3104T-PRO / ER2100
MODEL_RE = re.compile(r"(?<![A-Za-z0-9-])[A-Za-z]{2,}[- ]?\d{2,}[A-Za-z0-9-]*(?![A-Za-z0-9-])")
URL_RE = re.compile(r"https?://[^\s)]+")

FALLBACK_UNCOVERED = "本轮资料未覆盖这部分，无法确认。"


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[^0-9a-z一-鿿]+", "", text)


def _strip_urls(text: str) -> str:
    return URL_RE.sub(" ", text or "")


def _money_values(text: str, include_plain: bool = False) -> list:
    text = _strip_urls(text)
    vals = []
    for m in MONEY_RE.finditer(text):
        raw = (m.group(1) or m.group(2) or "").replace(",", "")
        try:
            vals.append(float(raw))
        except ValueError:
            pass
    if include_plain:
        # 台账里的价格可能只写 199.00 / 1299.00，没有 ¥
        for raw in re.findall(r"(?<![\d.])(\d{1,7}\.\d{2})(?![\d])", text):
            try:
                vals.append(float(raw))
            except ValueError:
                pass
    return vals


def high_risk_tokens(text: str) -> list:
    """
    提取需要核对的高风险 token：价格 / 长订单号 / 型号

    :return: [{"kind": "money|order_or_tracking|model", "token": "原文", "norm": "规范化"}]
    """
    text = _strip_urls(text)
    out, seen = [], set()

    def _add(kind, token):
        norm = _norm(token)
        if not norm or (kind, norm) in seen:
            return
        seen.add((kind, norm))
        out.append({"kind": kind, "token": token.strip(), "norm": norm})

    for m in MONEY_RE.finditer(text):
        raw = (m.group(1) or m.group(2) or "").strip()
        if raw:
            _add("money", raw)
    for m in LONG_ID_RE.finditer(text):
        _add("order_or_tracking", m.group(0))
    for m in MODEL_RE.finditer(text):
        _add("model", m.group(0))
    return out


def deterministic_issues(reply: str, ledger_text: str, user_text: str = "") -> list:
    """
    确定性核对：回复里的高风险 token 必须出现在台账或用户原话里

    只做高置信度信号；不判断普通句子是否 grounded。价格按数值比较，
    订单/物流号与型号按规范化字符串包含比较。
    """
    ledger = _strip_urls(ledger_text or "")
    user = _strip_urls(user_text or "")
    ledger_money = _money_values(ledger, include_plain=True) + _money_values(user, include_plain=True)
    ledger_norm = _norm(ledger + "\n" + user)

    issues = []
    for token in high_risk_tokens(reply):
        kind, raw, norm = token["kind"], token["token"], token["norm"]
        supported = False
        if kind == "money":
            try:
                value = float(raw.replace(",", ""))
                supported = any(abs(value - x) < 0.005 for x in ledger_money)
            except ValueError:
                supported = False
        else:
            supported = norm in ledger_norm
        if not supported:
            issues.append({
                "kind": kind,
                "token": raw,
                "severity": "critical",
                "reason": "台账与用户原话里都找不到这个事实" if kind != "money"
                          else "台账与用户原话里没有这个金额",
            })
    return issues


def _build_ledger_text(ledger: Iterable[dict]) -> str:
    parts, total = [], 0
    for entry in ledger or []:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        content = content[:grounding_config.max_tool_chars]
        label = str(entry.get("tool") or "tool")
        block = f"[{label}]\n{content}"
        if total + len(block) > grounding_config.max_ledger_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts) or "（本轮没有工具返回）"


def _should_verify(reply: str, ledger: list, issues: list) -> bool:
    if not grounding_config.enabled or not reply:
        return False
    # 确定性核对已经命中高风险 token（价格/订单号/型号）时必须校验，
    # 不受工具返回长度阈值限制 —— 否则短工具返回会让错误价格直接漏过去。
    if issues:
        return True
    tool_entries = [
        e for e in (ledger or [])
        if isinstance(e, dict) and e.get("tool") != "user"
        and len(str(e.get("content") or "")) >= grounding_config.min_tool_chars
    ]
    # 没有命中时只对「有实质工具返回 + 长回复」主动跑校验，避免每轮加延迟
    return bool(tool_entries) and len(reply) >= grounding_config.min_reply_chars


def _parse_json_object(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(content)
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}


def _verify_llm(question: str, ledger_text: str, reply: str) -> dict:
    """调 flash 校验；失败返回 error，不抛异常（fail-open 由调用方处理）"""
    try:
        client = get_llm_client(
            model=grounding_config.verifier_model,
            json_mode=True,
            base_url=lm_config.main_agent_base_url,
            api_key=lm_config.main_agent_api_key,
            retry_transient=True,
        )
        prompt = load_prompt("reply_verifier", question=question or "",
                             ledger=ledger_text, reply=reply or "")
        response = invoke_with_retry(
            client, [HumanMessage(content=prompt)], "校验回答依据")
        data = _parse_json_object(response.content)
        raw_items = data.get("unsupported") or []
        unsupported = []
        for item in raw_items[:20] if isinstance(raw_items, list) else []:
            if isinstance(item, str):
                text, reason, severity = item, "", "minor"
            elif isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                reason = str(item.get("reason") or "").strip()
                severity = str(item.get("severity") or "minor").lower()
            else:
                continue
            if text:
                unsupported.append({
                    "text": text,
                    "reason": reason,
                    "severity": "critical" if severity == "critical" else "minor",
                })
        return {"unsupported": unsupported, "error": ""}
    except Exception as e:
        logger.warning(f"[{NODE_NAME}] 接地校验调用失败（fail-open）：{type(e).__name__}: {e}")
        return {"unsupported": [], "error": f"{type(e).__name__}: {e}"}


def _repair_llm(question: str, ledger_text: str, reply: str, unsupported: list) -> str:
    """按台账重写一次；失败返回空串，由调用方走确定性降级"""
    try:
        client = get_llm_client(
            model=grounding_config.verifier_model,
            json_mode=True,
            base_url=lm_config.main_agent_base_url,
            api_key=lm_config.main_agent_api_key,
            retry_transient=True,
        )
        prompt = load_prompt(
            "reply_repair", question=question or "", ledger=ledger_text,
            reply=reply or "",
            unsupported=json.dumps(unsupported, ensure_ascii=False),
        )
        response = invoke_with_retry(
            client, [HumanMessage(content=prompt)], "修复回答依据")
        data = _parse_json_object(response.content)
        return str(data.get("reply") or "").strip()
    except Exception as e:
        logger.warning(f"[{NODE_NAME}] 回答修复调用失败（降级）：{type(e).__name__}: {e}")
        return ""


def _strip_sentences_with_issues(reply: str, issues: list) -> str:
    """最后的确定性降级：删掉包含高风险 token 的句子；空则明确说未覆盖"""
    tokens = [i.get("token") for i in issues if i.get("severity") == "critical"]
    if not tokens:
        return reply
    sentences = re.split(r"(?<=[。！？!?；;\n])", reply or "")
    kept = []
    for sentence in sentences:
        if any(tok and tok in sentence for tok in tokens):
            continue
        kept.append(sentence)
    out = "".join(kept).strip()
    return out or FALLBACK_UNCOVERED


def ground_reply(reply: str, ledger: list, user_message: str = "") -> tuple:
    """
    终稿接地校验入口（纯逻辑 + 可选 LLM）

    :return: `(最终回复, metadata)`；metadata 随 `query_runs.grounding` 落库
    """
    meta = {
        "enabled": bool(grounding_config.enabled),
        "checked": False,
        "verifier_used": False,
        "repair_used": False,
        "deterministic_issues": [],
        "unsupported": [],
        "error": "",
        "model": grounding_config.verifier_model,
    }
    if not grounding_config.enabled or not reply:
        return reply, meta

    ledger_text = _build_ledger_text(ledger)
    issues = deterministic_issues(reply, ledger_text, user_message)
    meta["checked"] = True
    meta["deterministic_issues"] = issues

    if not _should_verify(reply, ledger, issues):
        return reply, meta

    # 整轮预算不足时不再加校验/修复调用；答案已经生成，保留原文
    remaining = remaining_wall_clock()
    if remaining is not None and remaining < 10:
        meta["skipped"] = "low_budget"
        return reply, meta

    meta["verifier_used"] = True
    verified = _verify_llm(user_message, ledger_text, reply)
    meta["unsupported"] = verified.get("unsupported") or []
    meta["error"] = verified.get("error") or ""

    if meta["unsupported"]:
        # 修复也要占整轮预算：校验已经花掉一部分时，剩余不足就保留原文，
        # 只做确定性高风险 token 降级（如果有）。
        remaining = remaining_wall_clock()
        if remaining is not None and remaining < 10:
            meta["skipped"] = "low_budget"
            if issues:
                reply = _strip_sentences_with_issues(reply, issues)
            return reply, meta
        meta["repair_used"] = True
        repaired = _repair_llm(user_message, ledger_text, reply, meta["unsupported"])
        if repaired:
            reply = repaired
        else:
            reply = _strip_sentences_with_issues(reply, issues)
    elif meta["error"] and issues:
        # 校验器不可用但确定性检查已经发现高风险 token：走确定性降级
        reply = _strip_sentences_with_issues(reply, issues)

    # 修复后再核对一次高风险 token；仍不通过就删句
    remaining = deterministic_issues(reply, ledger_text, user_message)
    meta["remaining_issues"] = remaining
    if remaining:
        reply = _strip_sentences_with_issues(reply, remaining)
    return reply, meta


# ── 离线自测（项目约定：用例贴在守护的代码旁边，registry 见 app/core/regression.py）──

def _check_high_risk_tokens() -> list:
    """确定性核对：金额/订单/型号命中与不命中；步骤号不误伤。"""
    problems = []
    ledger = "价格 ¥199.00；订单号 NO1234567890；型号 Brother HAK 180；物流单号 SF1234567890"
    user = "我的订单 NO1234567890"
    ok = "这款 ¥199.00，订单 NO1234567890，型号 HAK 180，第 2 步如图 3 所示。"
    if deterministic_issues(ok, ledger, user):
        problems.append(f"有依据的回复被误判：{deterministic_issues(ok, ledger, user)}")
    bad_money = deterministic_issues("这款 ¥299.00。", ledger, user)
    if not any(i["kind"] == "money" for i in bad_money):
        problems.append(f"改价未命中：{bad_money}")
    bad_order = deterministic_issues("订单 NO9999999999。", ledger, user)
    if not any(i["kind"] == "order_or_tracking" for i in bad_order):
        problems.append(f"伪造订单号未命中：{bad_order}")
    bad_model = deterministic_issues("型号 XYZ 999 支持无线。", ledger, user)
    if not any(i["kind"] == "model" for i in bad_model):
        problems.append(f"伪造型号未命中：{bad_model}")
    # 用户原话提供的短订单号也应算有依据（长 id 规则之外）
    if deterministic_issues("订单 A1B2C3D4E5F6。", "", "我的订单 A1B2C3D4E5F6"):
        problems.append("用户原话里的订单号被误判")
    return problems


def _check_grounding_repair() -> list:
    """校验/修复接线：命中→修复；校验失败→fail-open；修复失败→确定性降级。"""
    problems = []
    ledger = [{"tool": "product-kb", "content": "说明书：" + "安装步骤。" * 40}]
    reply = "安装时把膜盒插入支架，价格 ¥299.00，外观是白色。"

    old_verify, old_repair = globals()["_verify_llm"], globals()["_repair_llm"]
    old_enabled = grounding_config.enabled
    old_min_tool = grounding_config.min_tool_chars
    old_min_reply = grounding_config.min_reply_chars
    grounding_config.enabled = True
    grounding_config.min_tool_chars = 1
    grounding_config.min_reply_chars = 1
    try:
        globals()["_verify_llm"] = lambda q, l, r: {"unsupported": [
            {"text": "价格 ¥299.00", "reason": "台账没有", "severity": "critical"}], "error": ""}
        globals()["_repair_llm"] = (
            lambda q, l, r, u: "安装时把膜盒插入支架。价格本轮资料未覆盖。")
        out, meta = ground_reply(reply, ledger, "怎么装膜盒？")
        if "299" in out:
            problems.append(f"修复后仍保留无依据价格：{out!r}")
        if not meta.get("verifier_used") or not meta.get("repair_used"):
            problems.append(f"校验/修复标记不对：{meta}")

        globals()["_verify_llm"] = lambda q, l, r: {"unsupported": [], "error": "boom"}
        globals()["_repair_llm"] = lambda q, l, r, u: ""
        out2, meta2 = ground_reply(reply, ledger, "怎么装膜盒？")
        if "299" in out2:
            problems.append(f"校验失败时未走确定性降级：{out2!r}")
        if meta2.get("error") != "boom":
            problems.append(f"校验失败未记录：{meta2}")

        # 工具返回很短（低于 min_tool_chars）但确定性核对命中价格：仍必须校验/修复，
        # 不能被「没有实质工具返回」的阈值挡掉（2026-11-18 真实冒烟踩到）。
        grounding_config.min_tool_chars = 999
        short_ledger = [{"tool": "product-kb", "content": "价格 199.00 元"}]
        globals()["_verify_llm"] = lambda q, l, r: {"unsupported": [
            {"text": "价格 ¥299.00", "reason": "台账没有", "severity": "critical"}], "error": ""}
        globals()["_repair_llm"] = lambda q, l, r, u: "价格 199.00 元。"
        out3, meta3 = ground_reply("价格 ¥299.00。", short_ledger, "")
        if "299" in out3 or not meta3.get("verifier_used"):
            problems.append(f"短工具返回时未触发校验/修复：{out3!r} / {meta3}")
    finally:
        globals()["_verify_llm"], globals()["_repair_llm"] = old_verify, old_repair
        grounding_config.enabled = old_enabled
        grounding_config.min_tool_chars = old_min_tool
        grounding_config.min_reply_chars = old_min_reply
    return problems
