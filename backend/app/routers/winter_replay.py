"""除雪车回放接口：错配定位、时间轴校准、并发补录、批量重放与历史快照。

这组接口固定在通用 /api/winter 路由之前注册，避免 /api/winter/{entry_id}
把 replay 误当成作业编号。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ReplaySubmitPayload,
    TimelineCalibratePayload,
    TripSupplementPayload,
)
from app.services.replay import OptimisticLockError, replay_service

router = APIRouter(prefix="/api/winter/replay", tags=["除雪车回放"])


def _lock_conflict(exc: OptimisticLockError) -> HTTPException:
    raise HTTPException(status_code=409, detail=str(exc))


@router.get("/works")
def list_replay_works(
    keyword: str | None = Query(default=None, description="按作业编号定位错配路段"),
    mismatched_only: bool = Query(default=False, description="只返回仍有错配/空桩号/重复趟次的作业"),
) -> dict:
    """除雪防滑列表页复现入口：按作业编号筛选，标记需要回放复核的作业。"""
    return {"items": replay_service.list_works(keyword=keyword, mismatched_only=mismatched_only)}


@router.post("/migrate-stakes")
def migrate_stakes() -> dict:
    """存量数据迁移：非快照轨迹的空桩号按相邻轨迹线性补齐，历史快照原样保留。"""
    return replay_service.migrate_legacy_stakes()


@router.get("/works/{winter_id}")
def get_replay_work(winter_id: int) -> dict:
    """除雪作业详情页：时间轴、逐趟轨迹、问题标注与当前回放游标一次取全。"""
    detail = replay_service.get_work(winter_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"作业 {winter_id} 没有回放数据")
    return detail


@router.put("/works/{winter_id}/timeline")
def calibrate_timeline(winter_id: int, payload: TimelineCalibratePayload) -> dict:
    """校准作业时间轴：乐观锁落新窗口，事件版本 +1 后重算错配。"""
    try:
        return replay_service.calibrate_timeline(
            winter_id,
            [segment.model_dump() for segment in payload.timeline],
            payload.expected_version,
        )
    except OptimisticLockError as exc:
        _lock_conflict(exc)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"作业 {winter_id} 没有回放数据")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/works/{winter_id}/trips/{trip_id}")
def supplement_trip(winter_id: int, trip_id: int, payload: TripSupplementPayload) -> dict:
    """并发补录：乐观锁校验事件版本；同一趟次重复提交是幂等更新，不会重复生成趟次。"""
    try:
        return replay_service.supplement_trip(
            winter_id,
            trip_id,
            payload.model_dump(exclude_none=True),
            payload.expected_version,
        )
    except OptimisticLockError as exc:
        _lock_conflict(exc)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"作业 {winter_id} 没有回放数据")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/works/{winter_id}/replay")
def replay_trips(winter_id: int, payload: ReplaySubmitPayload) -> dict:
    """批量重放：多条轨迹逐条复核、整组一次提交；游标与事件版本不符返回 409。"""
    try:
        return replay_service.replay_trips(
            winter_id,
            payload.trip_ids,
            expected_version=payload.expected_version,
            expected_cursor=payload.expected_cursor,
        )
    except OptimisticLockError as exc:
        _lock_conflict(exc)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"作业 {winter_id} 没有回放数据")
    except ValueError as exc:
        # blockers 是结构化错配明细，其余是可读错误说明
        detail = exc.args[0] if exc.args else str(exc)
        raise HTTPException(status_code=400, detail=detail)


@router.get("/works/{winter_id}/snapshot")
def get_snapshot(winter_id: int) -> dict:
    """历史路线原回放快照：只读呈现，迁移与重放都不改动它。"""
    snapshot = replay_service.get_snapshot_view(winter_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"作业 {winter_id} 不存在历史回放快照")
    return snapshot
