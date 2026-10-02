"""除雪车回放接口：作业定位、时间轴校准、补录、趟次重放、复核回写。

所有写操作都要求携带事件版本（expected_version），服务端按乐观锁校验；
回放游标与事件版本绑定，轨迹更新后旧游标自动作废。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.replay import ReplayConflict, ReplayError, replay_service

router = APIRouter(prefix="/api/winter/replay", tags=["除雪车回放"])


class CalibratePayload(BaseModel):
    expected_version: int
    # 每项：原时间（定位既有事件，可空表示新增）、时间、桩号、作业编号、边界
    updates: list[dict[str, Any]] = Field(default_factory=list)


class SupplementPayload(BaseModel):
    expected_version: int
    points: list[dict[str, Any]] = Field(default_factory=list)


class BatchPayload(BaseModel):
    work_codes: list[str] = Field(default_factory=list)


class ReviewItem(BaseModel):
    作业编号: str
    seq: int
    passed: bool = True
    note: str = ""


class CommitPayload(BaseModel):
    reviews: list[ReviewItem]


class DispatchPayload(BaseModel):
    responsible_group: str
    support_from: str | None = None
    seat: str = "指挥席位"


class VersionPayload(BaseModel):
    expected_version: int | None = None


@router.post("/demo")
def seed_demo() -> dict[str, Any]:
    """载入可稳定复现“跨路段尾点错配”的演示时间轴。"""
    return replay_service.seed_demo_track()


@router.get("/works")
def list_works(keyword: str | None = None) -> dict[str, Any]:
    """除雪防滑列表页：按作业编号定位作业卡，查看错配与游标状态。"""
    return {"items": replay_service.list_works(keyword)}


@router.get("/works/{work_code}")
def get_work(work_code: str) -> dict[str, Any]:
    """除雪作业详情页：时间轴、归属段、错配诊断、趟次与派工。"""
    return replay_service.get_work(work_code)


@router.post("/works/{work_code}/calibrate")
def calibrate(work_code: str, payload: CalibratePayload) -> dict[str, Any]:
    """校准作业时间轴：事务化提交，事件版本不符整体失败（409）。"""
    return replay_service.calibrate_timeline(
        work_code, payload.updates, expected_version=payload.expected_version
    )


@router.post("/works/{work_code}/supplement")
def supplement(work_code: str, payload: SupplementPayload) -> dict[str, Any]:
    """并发补录轨迹点：乐观锁冲突返回 409，不覆盖他人更新。"""
    return replay_service.supplement_track(
        work_code, payload.points, expected_version=payload.expected_version
    )


@router.post("/works/{work_code}/trips/next")
def replay_next(work_code: str, payload: VersionPayload | None = None) -> dict[str, Any]:
    """重放下一条趟次；游标版本失效自动重头开始，同序号不重复生成。"""
    expected = payload.expected_version if payload else None
    return replay_service.replay_trip(work_code, expected_version=expected)


@router.post("/works/{work_code}/dispatch")
def dispatch(work_code: str, payload: DispatchPayload) -> dict[str, Any]:
    """指挥席位记录作业开始时责任组（跨区支援口径）。"""
    return replay_service.dispatch_work(
        work_code,
        responsible_group=payload.responsible_group,
        support_from=payload.support_from,
        seat=payload.seat,
    )


@router.get("/works/{work_code}/snapshots/{seq}")
def get_snapshot(work_code: str, seq: int, snapshot_id: str | None = None) -> dict[str, Any]:
    """读取历史回放快照；快照冻结，后续重放不会改动它。"""
    return replay_service.get_snapshot(work_code, seq, snapshot_id=snapshot_id)


@router.post("/batch")
def batch_replay(payload: BatchPayload) -> dict[str, Any]:
    """批量重放多条作业轨迹：逐条生成趟次，结果待人工逐条复核。"""
    return replay_service.batch_replay(payload.work_codes)


@router.get("/reviews")
def list_reviews() -> dict[str, Any]:
    """复核台：列出当前版本待逐条复核的趟次。"""
    return {"items": replay_service.list_review_items()}


@router.post("/reviews")
def mark_review(payload: ReviewItem) -> dict[str, Any]:
    """逐条复核：通过/驳回单条趟次。"""
    return replay_service.mark_reviewed(
        payload.作业编号, payload.seq, passed=payload.passed, note=payload.note
    )


@router.post("/commit")
def commit_review(payload: CommitPayload) -> dict[str, Any]:
    """整组复核结论一次提交：事务回写除雪台账、车辆清单、物资待办。"""
    reviews = [item.model_dump() for item in payload.reviews]
    return replay_service.commit_review(reviews)


@router.post("/migrate")
def migrate(payload: VersionPayload | None = None) -> dict[str, Any]:
    """存量数据迁移：空桩号按相邻轨迹点补齐。"""
    expected = payload.expected_version if payload else None
    return replay_service.migrate_legacy_stakes(expected_version=expected)
