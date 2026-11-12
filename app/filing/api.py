"""HTTP endpoints for proxy mileage filing, resumption and status queries."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from .db import ContentConflictError, TaskNotFoundError
from .models import FilingRequest, ResumeResponse, TaskOutcome
from .worker import FilingWorker

router = APIRouter(prefix="/filing", tags=["filing"])


def _worker(request: Request) -> FilingWorker:
    return request.app.state.filing_worker


def _to_outcome(task: dict) -> TaskOutcome:
    return TaskOutcome(
        task_id=task["task_id"],
        month=task["month"],
        state=task["state"],
        lines=task["lines"],
        content_hash=task["content_hash"],
        attempt_count=task["attempt_count"],
        receipt=task["receipt"],
        error=task["error"],
        created_at=task["created_at"],
        updated_at=task["updated_at"],
    )


@router.post(
    "/tasks",
    response_model=TaskOutcome,
    status_code=202,
    summary="提交月度线路里程代填报请求",
    description=(
        "接收 YYYY-MM 月份及 1～20 条唯一线路 ID 与非负整数运营公里数，"
        "先落库再由唯一的后台任务通过 Playwright 登录门户逐行保存草稿并最终提交。\n\n"
        "- 同月同内容重试：返回已存在的任务（幂等），续办而非新建。\n"
        "- 同月异内容：返回 409 conflict，拒绝受理。\n"
        "- 同时只运行一个填报任务；其他请求排队等待。"
    ),
    responses={
        409: {
            "description": "同月已存在内容不同的请求，拒绝受理。",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "status": "content_conflict",
                            "message": "月份 2026-11 已存在内容不同的填报请求",
                            "existing_task_id": "…",
                            "existing_state": "accepted",
                        }
                    }
                }
            },
        }
    },
)
def create_filing_task(payload: FilingRequest, request: Request) -> TaskOutcome:
    lines = [item.model_dump() for item in payload.lines]
    try:
        task, _created = _worker(request).enqueue(payload.month, lines)
    except ContentConflictError as exc:
        existing = exc.existing
        raise HTTPException(
            status_code=409,
            detail={
                "status": "content_conflict",
                "message": str(exc),
                "existing_task_id": existing["task_id"],
                "existing_state": existing["state"],
                "existing_month": existing["month"],
            },
        )
    return _to_outcome(task)


@router.get(
    "/tasks",
    response_model=list[TaskOutcome],
    summary="查询填报任务列表（可按月份过滤）",
)
def list_filing_tasks(
    request: Request,
    month: str | None = Query(default=None, description="可选：仅返回该月份的任务"),
) -> list[TaskOutcome]:
    return [_to_outcome(task) for task in _worker(request).store.list_tasks(month)]


@router.get(
    "/tasks/{task_id}",
    response_model=TaskOutcome,
    summary="查询单个填报任务状态与回执",
    responses={404: {"description": "任务不存在"}},
)
def get_filing_task(task_id: str, request: Request) -> TaskOutcome:
    try:
        task = request.app.state.filing_store.get_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail={"status": "not_found",
                                                     "task_id": task_id})
    return _to_outcome(task)


@router.post(
    "/tasks/{task_id}/resume",
    response_model=ResumeResponse,
    summary="续办指定填报任务",
    description=(
        "进程重启或断流后调用。续办首先查看该月门户已提交页：内容一致则保存回执并报成功；"
        "内容不同报 conflict；尚未提交则继续逐行填报与最终提交。"
    ),
    responses={404: {"description": "任务不存在"}},
)
def resume_filing_task(task_id: str, request: Request) -> ResumeResponse:
    worker = _worker(request)
    try:
        task = worker.store.get_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail={"status": "not_found",
                                                     "task_id": task_id})
    if task["state"] == "success":
        return ResumeResponse(
            task_id=task_id, month=task["month"], state="success",
            message="任务已成功提交，无需续办。", receipt=task["receipt"],
        )
    if task["state"] == "conflict":
        return ResumeResponse(
            task_id=task_id, month=task["month"], state="conflict",
            message=f"任务已判定为冲突：{task['error']}", error=task["error"],
        )
    result = worker.resume(task_id)
    state = result["state"]
    if state in ("accepted", "running"):
        # Resume wait timed out while the single worker was still busy.
        return ResumeResponse(
            task_id=task_id, month=result["month"], state="running",
            message="填报任务仍在运行中，请稍后通过状态查询接口获取结果。",
        )
    return ResumeResponse(
        task_id=task_id,
        month=result["month"],
        state=state,  # type: ignore[arg-type]
        message={
            "success": "续办成功，门户提交内容与请求一致，回执已保存。",
            "conflict": f"续办发现门户内容冲突：{result['error']}",
            "failed": f"续办失败，可再次调用续办：{result['error']}",
        }.get(state, state),
        receipt=result["receipt"],
        error=result["error"],
    )
