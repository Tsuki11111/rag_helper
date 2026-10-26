"""
自建评测用例的持久化。

内置用例是仓库里的 `eval/cases.yaml`（手写、跟代码一起评审）；管理端新建的用例
写到 `output/eval/cases_custom.yaml` —— `output` 是 Docker 命名卷，重建容器不丢，
也不会改动已提交的内置用例。

读取时按「内置在前、自建在后」合并；id 必须全局唯一。
"""
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml

from app.core.eval_cases import CATEGORIES, SOURCES, load_cases, validate
from app.utils.path_util import PROJECT_ROOT

CUSTOM_CASES_PATH = PROJECT_ROOT / "output" / "eval" / "cases_custom.yaml"

# 与 rag_helper 的 outcome 口径一致；允许留空（不校验结局）
OUTCOMES = {"answered", "no_match", "rejected", "blocked", "paused", "waiting_user", "error"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
MAX_QUESTION_CHARS = 2000
MAX_FIELD_CHARS = 300

_lock = threading.Lock()


def load_custom_cases() -> List[Dict[str, Any]]:
    """读自建用例；文件不存在返回空列表，格式坏了直接报错"""
    if not CUSTOM_CASES_PATH.exists():
        return []
    data = yaml.safe_load(CUSTOM_CASES_PATH.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError(
            f"自建用例文件顶层应当是列表，实为 {type(data).__name__}：{CUSTOM_CASES_PATH}")
    cases = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"自建用例里有非字典条目：{item!r}")
        case = dict(item)
        case["custom"] = True
        cases.append(case)
    return cases


def load_all_cases() -> List[Dict[str, Any]]:
    """内置 + 自建；id 重复视为配置错误，避免静默覆盖"""
    builtin = []
    for item in load_cases():
        case = dict(item)
        case["custom"] = False
        builtin.append(case)
    custom = load_custom_cases()
    seen = set()
    for case in builtin + custom:
        cid = str(case.get("id") or "")
        if cid in seen:
            raise ValueError(f"评测用例 id 重复：{cid}")
        seen.add(cid)
    return builtin + custom


def create_case(payload: Dict[str, Any], created_by: str = "") -> Dict[str, Any]:
    """
    校验并追加一条自建用例，返回落盘后的完整用例。

    参数由 API 层传入，字段使用 snake_case（与 YAML 一致）。
    """
    question = str(payload.get("question") or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"问题最长 {MAX_QUESTION_CHARS} 个字符")

    category = str(payload.get("category") or "").strip()
    if category not in CATEGORIES:
        raise ValueError(f"category 必须是 {', '.join(sorted(CATEGORIES))} 之一")

    source = str(payload.get("source") or "constructed").strip()
    if source not in SOURCES:
        raise ValueError("source 只能是 real 或 constructed")

    expected = str(payload.get("expected_outcome") or "answered").strip()
    if expected and expected not in OUTCOMES:
        raise ValueError(f"expected_outcome 必须是 {', '.join(sorted(OUTCOMES))} 之一或留空")

    context = str(payload.get("context") or "").strip()
    if category == "anaphora" and not context:
        raise ValueError("指代类用例必须填写上一轮问题（context）")
    if len(context) > MAX_QUESTION_CHARS:
        raise ValueError(f"context 最长 {MAX_QUESTION_CHARS} 个字符")

    doc = str(payload.get("doc") or "").strip()
    if len(doc) > MAX_FIELD_CHARS:
        raise ValueError(f"doc 最长 {MAX_FIELD_CHARS} 个字符")

    gold_chunks = []
    for raw in payload.get("gold_chunks") or []:
        if isinstance(raw, bool):
            raise ValueError("gold_chunks 必须是整数切片 id")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"gold_chunks 里的 {raw!r} 不是整数") from exc
        if value <= 0:
            raise ValueError("gold_chunks 必须是正整数")
        gold_chunks.append(value)

    with _lock:
        existing = load_all_cases()
        if payload.get("id"):
            case_id = str(payload["id"]).strip().lower()
        else:
            case_id = f"custom_{uuid.uuid4().hex[:8]}"
        if not ID_PATTERN.fullmatch(case_id):
            raise ValueError("用例 id 只能用小写字母、数字、下划线或短横线，长度 3–64")
        if any(str(c.get("id")) == case_id for c in existing):
            raise ValueError(f"用例 id 已存在：{case_id}")

        case: Dict[str, Any] = {
            "id": case_id,
            "question": question,
            "category": category,
            "doc": doc,
            "source": source,
            "gold_chunks": gold_chunks,
            "expect": {"outcome": expected} if expected else {},
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "created_by": created_by,
        }
        if context:
            case["context"] = context

        problems = validate([case], check_gold_exists=bool(gold_chunks))
        if problems:
            raise ValueError("；".join(problems))

        custom = load_custom_cases()
        # custom 标记只存在于运行时，不写进 YAML
        persisted = [{k: v for k, v in item.items() if k != "custom"} for item in custom]
        persisted.append(case)
        CUSTOM_CASES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CUSTOM_CASES_PATH.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(persisted, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, CUSTOM_CASES_PATH)
        return {**case, "custom": True}


def _check_custom_case_store() -> List[str]:
    """离线回归：自建用例能落盘、合并、去重，且指代类必须带上文"""
    import tempfile

    global CUSTOM_CASES_PATH
    old_path = CUSTOM_CASES_PATH
    problems: List[str] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            CUSTOM_CASES_PATH = Path(tmp) / "cases_custom.yaml"
            created = create_case({
                "question": "测试自建用例",
                "category": "fact",
                "source": "constructed",
                "gold_chunks": [],
                "expected_outcome": "answered",
            }, created_by="selftest")
            if not created["id"].startswith("custom_"):
                problems.append(f"未自动生成 id：{created['id']}")
            if not created.get("custom"):
                problems.append("新建用例没有 custom 标记")
            all_cases = load_all_cases()
            if not any(c.get("id") == created["id"] and c.get("custom") for c in all_cases):
                problems.append("新建用例没有出现在合并列表里")
            if any(c.get("custom") for c in all_cases[:len(load_cases())]):
                problems.append("内置用例被错误标成自建")
            try:
                create_case({"id": created["id"], "question": "重复",
                             "category": "fact"})
                problems.append("重复 id 没有被拒绝")
            except ValueError:
                pass
            try:
                create_case({"question": "那它呢", "category": "anaphora"})
                problems.append("缺 context 的指代用例没有被拒绝")
            except ValueError:
                pass
    except Exception as exc:  # noqa: BLE001
        problems.append(f"自建用例存储自测抛异常：{type(exc).__name__}: {exc}")
    finally:
        CUSTOM_CASES_PATH = old_path
    return problems

