"""
评测（LLM-as-Judge）HTTP 接口

从 query_service 拆出来：问答服务只负责对话与 SSE，评测用例 / 结果 / 任务
是另一条独立的管理链路，单独一个 APIRouter 便于页面和测试按需挂载。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.logger import logger
from app.utils.auth_utils import current_tenant

NODE_NAME = "eval_service"

router = APIRouter(prefix="/eval", tags=["eval"])


class EvalRunRequest(BaseModel):
    """启动一次 LLM-as-Judge 评测"""
    cases: list[str] = Field(default_factory=list, description="用例 id；不传则按 limit 选")
    limit: int = Field(0, description="不指定 case 时取前 N 条，0 = 全部")
    runs: int = Field(1, description="每个用例重复次数，1–10")
    web: bool = Field(False, description="是否允许联网搜索参与回放")
    keep: bool = Field(False, description="保留评测会话现场（默认跑完清理）")


class EvalCaseCreateRequest(BaseModel):
    """新建一条自建评测用例"""
    id: str = Field("", description="可选；留空自动生成 custom_xxx")
    question: str = Field(..., description="用户问题")
    category: str = Field(..., description="fact/steps/descriptive/anaphora/...")
    doc: str = Field("", description="主要文档的 file_title")
    context: str = Field("", description="指代类用例的上一轮问题")
    source: str = Field("constructed", description="real / constructed")
    gold_chunks: list[int] = Field(default_factory=list, description="人工标注的切片 id")
    expected_outcome: str = Field("answered", description="期望结局")


@router.get("/cases", summary="评测用例列表", dependencies=[Depends(current_tenant)])
async def eval_cases():
    """列出可选用例（内置 + 自建）；顺带把 judge 配置问题带回页面"""
    from app.conf.judge_config import judge_config
    from app.eval.case_store import load_all_cases
    from app.eval.judge import validate_judge_config

    errors, warnings = validate_judge_config()
    try:
        items = [{
            "id": c.get("id"),
            "question": c.get("question"),
            "category": c.get("category"),
            "doc": c.get("doc") or "",
            "source": c.get("source") or "",
            "expected_outcome": c.get("expect", {}).get("outcome") or "",
            "has_gold": bool(c.get("gold_chunks")),
            "custom": bool(c.get("custom")),
            "created_at": c.get("created_at") or "",
        } for c in load_all_cases()]
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[{NODE_NAME}] [eval_cases] 读取用例失败：{exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"读取用例失败：{exc}")
    return {
        "items": items,
        "judge_model": judge_config.model,
        "config_errors": errors,
        "warnings": warnings,
    }


@router.post("/cases", summary="新建评测用例")
async def eval_case_create(payload: EvalCaseCreateRequest,
                           user: dict = Depends(current_tenant)):
    """把管理员填写的用例写入 output/eval/cases_custom.yaml，并立即参与评测"""
    from app.eval.case_store import create_case
    try:
        item = create_case(payload.model_dump(), created_by=user.get("name") or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[{NODE_NAME}] [eval_case_create] 新建用例失败：{exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"新建用例失败：{exc}")
    return {"item": item}


@router.get("/results", summary="历史评测结果", dependencies=[Depends(current_tenant)])
async def eval_results(limit: int = 50):
    """按修改时间倒序列出 output/eval 下的结果"""
    from app.eval.judge import list_result_files
    return {"items": list_result_files(limit=limit)}


@router.get("/results/{name}", summary="读取一份评测结果",
            dependencies=[Depends(current_tenant)])
async def eval_result_detail(name: str):
    """返回 JSONL 的逐条结果 + 汇总，或 Markdown 报告正文"""
    from app.eval.judge import load_result_file
    try:
        data = load_result_file(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[{NODE_NAME}] [eval_result_detail] 读取结果失败：{exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"读取结果失败：{exc}")
    if data.get("kind") == "jsonl":
        data["summary"] = {k: v for k, v in (data.get("summary") or {}).items()
                           if k != "worst"}
    return data


@router.post("/run", summary="启动评测", dependencies=[Depends(current_tenant)])
async def eval_run(payload: EvalRunRequest):
    """
    启动后台评测任务（单任务）

    当前用例跑完后才响应停止；已产出的逐条结果会保留。
    """
    from app.eval.runner import EvalJobBusy, start_job
    try:
        return start_job(case_ids=payload.cases, limit=payload.limit,
                         runs=payload.runs, web=payload.web, keep=payload.keep)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except EvalJobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[{NODE_NAME}] [eval_run] 启动评测失败：{exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"启动评测失败：{exc}")


@router.get("/jobs", summary="最近评测任务", dependencies=[Depends(current_tenant)])
async def eval_jobs(limit: int = 10):
    from app.eval.runner import list_jobs
    return {"items": list_jobs(limit=limit)}


@router.get("/jobs/{job_id}", summary="评测任务状态",
            dependencies=[Depends(current_tenant)])
async def eval_job(job_id: str):
    from app.eval.runner import get_job
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在（服务重启后任务表会清空）")
    return job


@router.post("/jobs/{job_id}/stop", summary="停止评测",
             dependencies=[Depends(current_tenant)])
async def eval_job_stop(job_id: str):
    from app.eval.runner import stop_job
    job = stop_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job
