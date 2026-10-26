"""
评估任务管理器（8002 评测页使用）

单进程、单任务：评测会真实回放主 agent，同时跑多份会互相抢 Milvus / LLM 配额，
也会让清理计数难以对齐，所以一次只允许一个任务。任务状态只在内存里，
服务重启后丢失；历史结果文件仍在 `output/eval/`，页面可以直接打开。

与 SSE 一样，这个实现假设 query_service 单 worker 运行。多 worker 时每个进程
各有一份任务表，轮询可能落到没有任务的那个进程 —— 要横向扩展得把状态挪到 Redis。
"""
import copy
import threading
import time
import uuid
from typing import List, Optional

from app.core import eval_judge
from app.core.logger import logger

MAX_JOBS = 20
_ACTIVE_STATUSES = {"queued", "running"}

_jobs = {}
_active_id = None
_lock = threading.Lock()


class EvalJobBusy(RuntimeError):
    """已有评测在跑"""


def _summary_for_ui(summary: dict) -> dict:
    """去掉 worst（内含证据正文），避免轮询响应过大；报告里仍保留"""
    return {k: v for k, v in summary.items() if k != "worst"}


def _public(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "elapsed_ms": job["elapsed_ms"],
        "params": copy.deepcopy(job["params"]),
        "total": job["total"],
        "done": job["done"],
        "current_case": job["current_case"],
        "percent": round(job["done"] / job["total"] * 100, 1) if job["total"] else 0,
        "result": copy.deepcopy(job["result"]),
        "error": job["error"],
        "warnings": list(job["warnings"]),
    }


def _trim_jobs() -> None:
    if len(_jobs) <= MAX_JOBS:
        return
    removable = sorted(
        (j for j in _jobs.values() if j["status"] not in _ACTIVE_STATUSES),
        key=lambda j: j["created_at"],
    )
    for job in removable[:len(_jobs) - MAX_JOBS]:
        _jobs.pop(job["id"], None)


def start_job(case_ids: Optional[List[str]] = None, limit: int = 0, runs: int = 1,
              web: bool = False, keep: bool = False) -> dict:
    """
    校验参数并启动后台评测线程，返回任务快照

    参数错误抛 `ValueError`，已有任务在跑抛 `EvalJobBusy`。
    """
    global _active_id
    errors, warnings = eval_judge.validate_judge_config()
    if errors:
        raise ValueError("；".join(errors))
    selected_ids = [str(x) for x in (case_ids or [])]
    # 明确选了用例时不再截断；limit 只用于「没选用例、按前 N 条跑」
    try:
        cases = eval_judge._select_cases(
            eval_judge._load_cases(), selected_ids, 0 if selected_ids else int(limit or 0))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if not cases:
        raise ValueError("没有选中的用例")

    runs = max(1, min(int(runs or 1), 10))
    params = {
        "case_ids": [c.get("id") for c in cases],
        "limit": 0 if selected_ids else int(limit or 0),
        "runs": runs,
        "web": bool(web),
        "keep": bool(keep),
        "case_count": len(cases),
        "judge_model": eval_judge.judge_config.model,
    }
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        active = _jobs.get(_active_id) if _active_id else None
        if active and active["status"] in _ACTIVE_STATUSES:
            raise EvalJobBusy(f"已有评测在跑（{active['id']}），等它结束或先停止")
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "elapsed_ms": None,
            "params": params,
            "total": len(cases) * runs,
            "done": 0,
            "current_case": "",
            "result": None,
            "error": "",
            "warnings": list(warnings),
            "_stop": threading.Event(),
        }
        _jobs[job_id] = job
        _active_id = job_id
        _trim_jobs()
        snapshot = _public(job)

    thread = threading.Thread(
        target=_run_job, args=(job_id, cases, runs, bool(web), bool(keep)),
        name=f"eval-job-{job_id}", daemon=True,
    )
    thread.start()
    return snapshot


def _run_job(job_id: str, cases: List[dict], runs: int, web: bool, keep: bool) -> None:
    global _active_id
    job = _jobs.get(job_id)
    if not job:
        return
    started = time.time()
    with _lock:
        job["status"] = "running"
        job["started_at"] = started
        job["current_case"] = cases[0].get("id", "") if cases else ""
    stop_event = job["_stop"]

    def on_progress(p: dict) -> None:
        with _lock:
            job["done"] = int(p.get("done") or 0)
            job["current_case"] = p.get("case_id") or ""

    try:
        result = eval_judge.run_selected(
            cases, runs=runs, enable_web_search=web, keep=keep,
            progress=on_progress, should_stop=stop_event.is_set,
        )
        with _lock:
            job["done"] = len(result["rows"])
            job["current_case"] = ""
            job["result"] = {
                "jsonl": result["jsonl"],
                "report": result["report"],
                "summary": _summary_for_ui(result["summary"]),
            }
            job["status"] = "stopped" if result["stopped"] else "completed"
            job["finished_at"] = time.time()
            job["elapsed_ms"] = int((job["finished_at"] - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"评测任务 {job_id} 失败：{exc}", exc_info=True)
        with _lock:
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["finished_at"] = time.time()
            job["elapsed_ms"] = int((job["finished_at"] - started) * 1000)
    finally:
        with _lock:
            if _active_id == job_id:
                _active_id = None


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def list_jobs(limit: int = 10) -> List[dict]:
    with _lock:
        items = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
        return [_public(j) for j in items[:max(1, int(limit))]]


def stop_job(job_id: str) -> Optional[dict]:
    """
    请求停止：只置位，当前用例会跑完；下一轮开始前退出并保留已产出的结果。
    """
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        if job["status"] in _ACTIVE_STATUSES:
            job["_stop"].set()
        return _public(job)
