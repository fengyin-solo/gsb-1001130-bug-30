"""除雪车回放领域：作业路段切分、趟次重放、补录与修正回写。

这里集中解决除雪车回放的几类问题：

1. 轨迹切到下一个作业路段时作业卡与轨迹对不上。旧实现按设备上报的
   「作业编号」首次变化点切段，但车辆在物理越界后、作业正式交接前会有一
   段尾点（设备仍挂旧编号或过早挂新编号），于是尾点被划给下一张作业卡。
   修法：一律以作业时间轴上的开始/结束边界围成的时间窗切段，窗内轨迹点
   不管设备上报归属漂移，都归该作业；窗之间的空档不计趟次。
2. 趟次必须幂等：同一作业按序号唯一，断点重放命中已有趟次直接返回冻结快
   照，不重复生成。
3. 回放游标与事件版本绑定：游标推进时记录所依据的事件版本；轨迹一旦更新
   （版本变化）旧游标作废，必须从头重放，不允许接着旧结果走。
4. 轨迹更新走事务（staging 构建 + 校验 + 一次性 commit），提交时比对事件
   版本，版本不符整体失败，不留下“轨迹已换、趟次仍旧”的半成品。
5. 并发补录采用乐观锁：补录携带期望版本，冲突拒绝并提示重新拉取。

领域口径（不可随实现改动）：
- 跨区支援以作业开始时指挥席位记录的责任组为准，作业中途变更不追溯历史
  趟次。
- 历史回放快照一经生成即冻结；轨迹更新后重放只生成新版本快照，旧快照永
  不改动；当前作业卡只展示最新版本趟次，不与旧趟次并列重复。
- 存量空桩号点不臆造数值，迁移时按相邻轨迹点线性补齐。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from app.store import store

WINTER_MODULE = "winter"
VEHICLE_MODULE = "vehicle"
TODO_MODULE = "winter_material_todo"

BOUNDARY_START = "start"
BOUNDARY_END = "end"


class ReplayConflict(RuntimeError):
    """乐观锁 / 事件版本冲突：调用方需重新拉取后再提交，映射 HTTP 409。"""


class ReplayError(RuntimeError):
    """回放前置条件不满足（作业不存在、趟次缺失等），映射 HTTP 400。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_ts(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ReplayError("轨迹点缺少时间，无法校准时间轴")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayError(f"时间格式无法解析：{text}") from exc


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fill_empty_stake(points: list[dict[str, Any]]) -> int:
    """存量数据迁移：按相邻轨迹点补齐空桩号，返回补齐点数。

    中间空值按前后最近有效点线性插值；开头连续空值顺延首个有效桩号；末尾
    沿用最后一个有效桩号；全程无有效桩号则无法补齐（返回 0，不臆造）。
    没有「桩号」键的点（如开始/结束边界事件）不属于轨迹点，跳过。
    """
    stakes = [_to_float(point.get("桩号")) if "桩号" in point else False for point in points]
    eligible = [idx for idx, value in enumerate(stakes) if value is not False]
    known = [idx for idx in eligible if stakes[idx] is not None]
    if not known:
        return 0
    filled = 0
    first, last = known[0], known[-1]

    def mark(idx: int, value: float) -> None:
        nonlocal filled
        points[idx]["桩号"] = value
        points[idx]["桩号补齐"] = True
        filled += 1

    for idx in eligible[:eligible.index(first)]:
        mark(idx, stakes[first])  # type: ignore[arg-type]
    for left, right in zip(known, known[1:]):
        if right == left + 1:
            continue
        start_value, end_value = stakes[left], stakes[right]
        span = right - left
        for idx in range(left + 1, right):
            if idx not in eligible:
                continue
            ratio = (idx - left) / span
            mark(idx, round(start_value + (end_value - start_value) * ratio, 3))  # type: ignore[operator]
    for idx in eligible[eligible.index(last) + 1:]:
        mark(idx, stakes[last])  # type: ignore[arg-type]
    return filled


@dataclass
class _WorkState:
    code: str
    # 当前版本的趟次视图（seq -> 趟次）。重放产生新版本时整体替换，旧版本
    # 只保留在 snapshots 里，因此回看不会把新旧趟次并列重复显示。
    trips: dict[int, dict[str, Any]] = field(default_factory=dict)
    # 冻结快照：快照编号 -> 快照（含当时轨迹与趟次），永不修改
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 回放游标与事件版本绑定
    cursor_seq: int = 0
    cursor_version: int = 0
    # 正在生成中的趟次序号：断点重放时同序号只生成一次
    generating: set[int] = field(default_factory=set)
    # 作业开始时指挥席位的派工记录
    dispatch: dict[str, Any] = field(default_factory=dict)


class ReplayService:
    """除雪车回放应用服务。内存存储 + 锁，语义对齐真实数据库事务。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 车辆级全局事件时间轴：轨迹点 + 作业开始/结束边界事件。
        # 轨迹点上的「作业编号」是设备上报归属，边界交接期间可能漂移；
        # 切段只认边界时间窗。
        self._timeline: list[dict[str, Any]] = []
        self._vehicle_code: str | None = None
        self._event_version: int = 0
        self._works: dict[str, _WorkState] = {}
        self._seeded = False
        self._bootstrap()

    # ---- 初始化 / 作业定位 -------------------------------------------------

    def _bootstrap(self) -> None:
        for row in store.rows(WINTER_MODULE):
            code = str(row.get("作业编号") or "")
            if code:
                self._works.setdefault(code, _WorkState(code=code))

    def _require_work(self, work_code: str) -> _WorkState:
        work = self._works.get(work_code)
        if work is None:
            raise ReplayError(f"作业编号 {work_code} 不存在，请先在除雪防滑列表核对")
        return work

    def seed_demo_track(self, *, vehicle_code: str = "VEHI-0002", reset: bool = True) -> dict[str, Any]:
        """造一组可稳定复现错配的演示时间轴（两台作业卡、一个交接尾点）。"""
        with self._lock:
            a_code = self._pick_seed_code(offset=2)   # WINT-0002
            b_code = self._pick_seed_code(offset=3)   # WINT-0003
            self._require_work(a_code)
            self._require_work(b_code)
            if reset or not self._seeded:
                self._timeline = []
                self._event_version = 0
                for work in self._works.values():
                    work.trips = {}
                    work.snapshots = {}
                    work.cursor_seq = 0
                    work.cursor_version = 0
                    work.generating = set()
                self._vehicle_code = vehicle_code
                timeline: list[dict[str, Any]] = [
                    {"时间": "2026-09-02T08:00:00", "边界": BOUNDARY_START, "作业编号": a_code},
                    {"时间": "2026-09-02T08:00:00", "桩号": 100.0, "作业编号": a_code},
                    # 存量空桩号点：迁移时应按相邻 100/200 补成 150
                    {"时间": "2026-09-02T08:00:30", "桩号": None, "作业编号": a_code},
                    {"时间": "2026-09-02T08:01:00", "桩号": 200.0, "作业编号": a_code},
                    # 指挥侧按车辆进入下一路段提前开了 B（旧切段口径）
                    {"时间": "2026-09-02T08:02:00", "边界": BOUNDARY_START, "作业编号": b_code},
                    # 错配尾点：设备仍挂 A，物理时间在 B 开始边界之后；
                    # 该点空桩号是第二个存量空值，迁移时按相邻 400 顺延补齐
                    {"时间": "2026-09-02T08:02:00", "桩号": None, "作业编号": a_code},
                    {"时间": "2026-09-02T08:03:00", "桩号": 400.0, "作业编号": b_code},
                    {"时间": "2026-09-02T08:04:00", "桩号": 500.0, "作业编号": b_code},
                    # 返程趟次：桩号回退，切成 B 的第 2 趟
                    {"时间": "2026-09-02T08:04:30", "桩号": 400.0, "作业编号": b_code},
                    {"时间": "2026-09-02T08:05:00", "边界": BOUNDARY_END, "作业编号": b_code},
                ]
                self._timeline = timeline
                self._event_version = 1
                self._works[a_code].dispatch = {
                    "责任组": "一组", "跨区支援自": None,
                    "指挥席位": "指挥A", "记录时间": "2026-09-02T08:00:00",
                }
                self._seeded = True
            return {
                "事件版本": self._event_version,
                "轨迹车辆": self._vehicle_code,
                "作业A": a_code,
                "作业B": b_code,
                "事件数": len(self._timeline),
            }

    def _pick_seed_code(self, *, offset: int) -> str:
        for row in store.rows(WINTER_MODULE):
            if int(row.get("id", 0)) == offset:
                return str(row["作业编号"])
        return sorted(self._works)[offset - 1]

    def _ledger_row(self, work_code: str) -> dict[str, Any] | None:
        for row in store.rows(WINTER_MODULE):
            if str(row.get("作业编号") or "") == work_code:
                return row
        return None

    def list_works(self, keyword: str | None = None) -> list[dict[str, Any]]:
        """除雪防滑列表页入口：按作业编号定位作业卡与回放状态。"""
        items: list[dict[str, Any]] = []
        with self._lock:
            for code, work in self._works.items():
                if keyword and keyword not in code:
                    continue
                windows = self._windows()
                own = windows.get(code, [])
                current = [point for seg in own for point in seg]
                drift = sum(
                    1 for point in current
                    if point.get("作业编号") and point.get("作业编号") != code
                )
                outside = sum(
                    1 for point in self._timeline
                    if not point.get("边界")
                    and point.get("作业编号") == code
                    and point not in current
                )
                ledger = self._ledger_row(code)
                items.append({
                    "作业编号": code,
                    "台账id": ledger.get("id") if ledger else None,
                    "作业路段": (ledger or {}).get("作业路段"),
                    "事件版本": self._event_version,
                    "当前趟次数": len(work.trips),
                    "游标": {"seq": work.cursor_seq, "事件版本": work.cursor_version},
                    "游标有效": work.cursor_version == self._event_version and self._event_version > 0,
                    "错配点数": drift + outside,
                    "窗内漂移点": drift,
                    "窗外尾点": outside,
                    "作业卡轨迹一致": drift + outside == 0 and bool(work.trips),
                    "责任组": work.dispatch.get("责任组"),
                })
        return items

    def get_work(self, work_code: str) -> dict[str, Any]:
        """作业详情页作业卡：时间轴、趟次、游标、派工与错配诊断一次取齐。"""
        with self._lock:
            work = self._require_work(work_code)
            windows = self._windows()
            segments = windows.get(work_code, [])
            current = [
                point for seg in segments for point in seg
            ]
            drift_points = [
                {"时间": p.get("时间"), "桩号": p.get("桩号"), "设备上报": p.get("作业编号")}
                for p in current
                if p.get("作业编号") and p.get("作业编号") != work_code
            ]
            outside = [
                {"时间": p.get("时间"), "桩号": p.get("桩号")}
                for p in self._timeline
                if not p.get("边界") and p.get("作业编号") == work_code and p not in current
            ]
            ledger = self._ledger_row(work_code)
            return {
                "作业编号": work_code,
                "台账id": ledger.get("id") if ledger else None,
                "作业路段": (ledger or {}).get("作业路段"),
                "事件版本": self._event_version,
                "时间轴": deepcopy(self._ordered()),
                "归属段": [
                    {
                        "开始时间": seg[0]["时间"],
                        "结束时间": seg[-1]["时间"],
                        "点数": len(seg),
                        "桩号序列": [p.get("桩号") for p in seg],
                    } for seg in segments
                ],
                "错配点数": len(drift_points) + len(outside),
                "窗内漂移点": drift_points,
                "落到窗外的点": outside,
                "趟次": [work.trips[key] for key in sorted(work.trips)],
                "游标": {"seq": work.cursor_seq, "事件版本": work.cursor_version},
                "游标有效": work.cursor_version == self._event_version and self._event_version > 0,
                "派工": dict(work.dispatch),
            }

    # ---- 时间轴切分（修复核心） --------------------------------------------

    def _ordered(self) -> list[dict[str, Any]]:
        return sorted(self._timeline, key=lambda p: parse_ts(p.get("时间")))

    def _windows(self) -> dict[str, list[list[dict[str, Any]]]]:
        """按开始/结束边界时间窗切段。

        边界事件决定当前开放作业；窗内轨迹点无视设备上报编号漂移归窗内作
        业；start 切换即隐式结束上一作业；窗外点丢弃为运行空档。
        没有任何边界事件时退化为按设备编号连续归并（兼容无边界数据）。
        """
        ordered = self._ordered()
        has_boundary = any(point.get("边界") for point in ordered)
        windows: dict[str, list[list[dict[str, Any]]]] = {}
        if not has_boundary:
            current_code: str | None = None
            for point in ordered:
                if point.get("边界"):
                    continue
                code = point.get("作业编号")
                if not code:
                    continue
                if code != current_code:
                    windows.setdefault(code, []).append([])
                    current_code = code
                windows[code][-1].append(point)
            return windows

        open_code: str | None = None
        bucket: list[dict[str, Any]] = []
        for point in ordered:
            boundary = point.get("边界")
            if boundary == BOUNDARY_START:
                if open_code is not None and bucket:
                    windows.setdefault(open_code, []).append(bucket)
                open_code = point.get("作业编号")
                bucket = []
            elif boundary == BOUNDARY_END:
                if open_code is not None and bucket:
                    windows.setdefault(open_code, []).append(bucket)
                open_code = None
                bucket = []
            elif open_code is not None:
                bucket.append(point)
        if open_code is not None and bucket:
            windows.setdefault(open_code, []).append(bucket)
        return windows

    @staticmethod
    def _passes(segment: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """把一个作业时间窗按桩号方向折返拆成趟次。"""
        passes: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        direction = 0
        last_known: float | None = None
        for point in segment:
            stake = _to_float(point.get("桩号"))
            if stake is not None and last_known is not None:
                step = 1 if stake > last_known else (-1 if stake < last_known else 0)
                if step and direction and step != direction:
                    # 折返：当前点属于新趟次，旧趟次收口
                    passes.append(current)
                    current = []
                if step:
                    direction = step
            current.append(point)
            if stake is not None:
                last_known = stake
        if current:
            passes.append(current)
        return [pass_ for pass_ in passes if pass_]

    # ---- 时间轴校准（事务 + 乐观版本） -------------------------------------

    def calibrate_timeline(
        self,
        work_code: str,
        updates: list[dict[str, Any]],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        """校准作业时间轴（改点时间/桩号/归属、移动或新增边界）。

        两阶段事务：先在 deepcopy 的 staging 上构建并逐条校验，全部通过才
        commit；提交时比对事件版本，不符抛 ReplayConflict，已生效改动为零。
        成功后事件版本 +1，所有绑定旧版本的回放游标作废。
        """
        with self._lock:
            self._require_work(work_code)
            if expected_version != self._event_version:
                raise ReplayConflict(
                    f"事件版本不一致：当前 v{self._event_version}，提交基于 v{expected_version}，"
                    "请重新拉取时间轴"
                )
            staging = deepcopy(self._timeline)
            touched = inserted = 0
            for change in updates:
                original = str(change.get("原时间") or "").strip()
                new_time = str(change.get("时间") or "").strip()
                if not new_time:
                    raise ReplayError("校准项缺少时间")
                parse_ts(new_time)
                target: dict[str, Any] | None = None
                if original:
                    wants_boundary = bool(change.get("边界"))
                    candidates = [
                        p for p in staging
                        if str(p.get("时间")) == original and bool(p.get("边界")) == wants_boundary
                    ]
                    target = candidates[0] if candidates else None
                    if target is None:
                        raise ReplayError(f"原时间点 {original} 已不存在，请刷新时间轴后重试")
                if target is None:
                    same_time = next((p for p in staging if str(p.get("时间")) == new_time), None)
                    if same_time is not None and not change.get("边界"):
                        raise ReplayError(f"{new_time} 已存在轨迹点，不能重复插入")
                    target = {}
                    staging.append(target)
                    inserted += 1
                target["时间"] = new_time
                if "桩号" in change:
                    target["桩号"] = change["桩号"]
                if "作业编号" in change and change["作业编号"]:
                    target["作业编号"] = change["作业编号"]
                elif "作业编号" not in target:
                    target["作业编号"] = work_code
                boundary = change.get("边界")
                if boundary:
                    if boundary not in (BOUNDARY_START, BOUNDARY_END):
                        raise ReplayError(f"未知边界类型：{boundary}")
                    target["边界"] = boundary
                elif "边界" in change and not change["边界"]:
                    target.pop("边界", None)
                target["校准时间"] = _now()
                touched += 1
            staging.sort(key=lambda p: parse_ts(p.get("时间")))
            self._validate_boundaries(staging)
            # commit：staging 校验通过后一次性生效
            self._timeline = staging
            self._event_version += 1
            self._invalidate_cursors()
            return {"事件版本": self._event_version, "校准点数": touched, "新增事件": inserted}

    @staticmethod
    def _validate_boundaries(events: list[dict[str, Any]]) -> None:
        open_code: str | None = None
        for point in events:
            boundary = point.get("边界")
            if boundary == BOUNDARY_START:
                open_code = point.get("作业编号")
            elif boundary == BOUNDARY_END:
                code = point.get("作业编号")
                if open_code is None:
                    raise ReplayError(f"{point['时间']} 的结束边界没有对应的开始边界")
                if code and open_code and code != open_code:
                    raise ReplayError(
                        f"边界不配对：{open_code} 的开始被 {code} 的结束收尾，请先校准时间轴"
                    )
                open_code = None

    def _invalidate_cursors(self) -> None:
        """轨迹更新后游标全部作废，须与新版本重新绑定。

        当前视图中的旧版本趟次一并摘除（旧结果仍保留在冻结快照里，回看走
        快照接口），否则补录/校准后作业卡会把旧趟次与新趟次并列重复显示。
        """
        for work in self._works.values():
            work.cursor_seq = 0
            work.cursor_version = 0
            work.trips = {}

    # ---- 并发补录（乐观锁） ------------------------------------------------

    def supplement_track(
        self,
        work_code: str,
        points: list[dict[str, Any]],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        """并发补录轨迹点：携带期望版本，冲突拒绝，不覆盖他人更新。"""
        cleaned: list[dict[str, Any]] = []
        for point in points:
            text = str(point.get("时间") or "").strip()
            if not text:
                raise ReplayError("补录点缺少时间")
            parse_ts(text)
            cleaned.append({
                "时间": text,
                "桩号": point.get("桩号"),
                "作业编号": work_code,
                "补录": True,
                "补录时间": _now(),
            })
        with self._lock:
            self._require_work(work_code)
            if expected_version != self._event_version:
                raise ReplayConflict(
                    f"补录冲突：轨迹已更新到 v{self._event_version}，你的补录基于 "
                    f"v{expected_version}，请重新拉取后再补录"
                )
            existing = {
                str(p.get("时间")) for p in self._timeline
                if not p.get("边界")
            }
            added = 0
            for point in cleaned:
                if point["时间"] in existing:
                    continue  # 同时刻重复补录幂等处理，不重复入库
                self._timeline.append(point)
                existing.add(point["时间"])
                added += 1
            self._timeline.sort(key=lambda p: parse_ts(p.get("时间")))
            self._event_version += 1
            self._invalidate_cursors()
            return {"事件版本": self._event_version, "补录点数": added}

    # ---- 趟次重放（游标版本绑定 + 幂等） -----------------------------------

    def replay_trip(
        self, work_code: str, *, expected_version: int | None = None
    ) -> dict[str, Any]:
        """重放下一条趟次。游标版本不符即重置；同版本同序号不重复生成。"""
        with self._lock:
            work = self._require_work(work_code)
            if expected_version is not None and expected_version != self._event_version:
                raise ReplayConflict("事件版本已变化，请刷新作业卡后重放")
            passes = self._work_passes(work)
            if work.cursor_version != self._event_version:
                # 游标与事件版本绑定：失效后从头重放；旧趟次被新版本替换，
                # 旧结果仍以冻结快照形式保留，不会在当前视图重复显示。
                work.cursor_seq = 0
                work.cursor_version = self._event_version
                work.trips = {}
            if work.cursor_seq >= len(passes):
                return {"作业编号": work_code, "完成": True, "趟次": None,
                        "事件版本": self._event_version, "游标": work.cursor_seq}
            seq = work.cursor_seq + 1
            work.generating.add(seq)
            try:
                trip = self._build_or_get_trip(work, seq, passes[seq - 1])
            finally:
                work.generating.discard(seq)
            work.cursor_seq = seq
            work.cursor_version = self._event_version
            return {"作业编号": work_code, "完成": False, "趟次": trip,
                    "事件版本": self._event_version, "游标": seq}

    def _work_passes(self, work: _WorkState) -> list[list[dict[str, Any]]]:
        segments = self._windows().get(work.code, [])
        passes: list[list[dict[str, Any]]] = []
        for segment in segments:
            passes.extend(self._passes(segment))
        return passes

    def _build_or_get_trip(
        self, work: _WorkState, seq: int, points: list[dict[str, Any]]
    ) -> dict[str, Any]:
        existing = work.trips.get(seq)
        if existing is not None and existing["事件版本"] == self._event_version:
            # 断点重放幂等命中：返回原趟次与原快照，绝不重复生成
            return existing
        ledger = self._ledger_row(work.code)
        known = [_to_float(p.get("桩号")) for p in points]
        known_non_null = [value for value in known if value is not None]
        trip = {
            "seq": seq,
            "作业编号": work.code,
            "作业路段": (ledger or {}).get("作业路段", ""),
            "开始时间": str(points[0]["时间"]),
            "结束时间": str(points[-1]["时间"]),
            "起点桩号": known_non_null[0] if known_non_null else None,
            "终点桩号": known_non_null[-1] if known_non_null else None,
            "轨迹点数": len(points),
            "责任组": str(work.dispatch.get("责任组") or "未派工"),
            "跨区支援自": work.dispatch.get("跨区支援自"),
            "指挥席位": work.dispatch.get("指挥席位"),
            "快照编号": f"SNAP-{work.code}-{seq}-v{self._event_version}",
            "事件版本": self._event_version,
            "复核状态": "待复核",
        }
        # 历史路线保持原回放快照不变：快照只增不改
        work.snapshots[trip["快照编号"]] = {
            "快照编号": trip["快照编号"],
            "冻结时间": _now(),
            "事件版本": self._event_version,
            "轨迹": deepcopy(points),
            "趟次": deepcopy(trip),
        }
        work.trips[seq] = trip
        return trip

    def batch_replay(self, work_codes: Iterable[str]) -> dict[str, Any]:
        """批量重放：多条作业轨迹逐条生成趟次，结果置为待复核供人工逐条核对。

        同样强制游标绑定当前事件版本；重复点击批量重放不会产生重复趟次。
        """
        codes: list[str] = []
        for raw in work_codes:
            code = str(raw or "").strip()
            if code and code not in codes:
                codes.append(code)
        results: list[dict[str, Any]] = []
        with self._lock:
            for code in codes:
                work = self._require_work(code)
                passes = self._work_passes(work)
                work.trips = {}
                work.cursor_seq = 0
                work.cursor_version = self._event_version
                trips: list[dict[str, Any]] = []
                for seq, points in enumerate(passes, start=1):
                    work.generating.add(seq)
                    try:
                        trips.append(self._build_or_get_trip(work, seq, points))
                    finally:
                        work.generating.discard(seq)
                    work.cursor_seq = seq
                results.append({
                    "作业编号": code,
                    "事件版本": self._event_version,
                    "趟次数": len(trips),
                    "趟次": trips,
                })
        return {"事件版本": self._event_version, "结果": results}

    # ---- 逐条复核 / 整组提交 -----------------------------------------------

    def list_review_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with self._lock:
            for code in sorted(self._works):
                work = self._works[code]
                for seq in sorted(work.trips):
                    items.append(dict(work.trips[seq]))
        return items

    def mark_reviewed(
        self, work_code: str, seq: int, *, passed: bool, note: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            work = self._require_work(work_code)
            trip = work.trips.get(seq)
            if trip is None:
                raise ReplayError(f"趟次 {work_code}#{seq} 尚未重放，无法复核")
            trip["复核状态"] = "通过" if passed else "驳回"
            trip["复核意见"] = note
            return dict(trip)

    def commit_review(self, reviews: list[dict[str, Any]]) -> dict[str, Any]:
        """整组复核结论一次提交，事务性回写除雪台账、车辆清单、物资待办。

        任一趟次缺失或未通过则整组失败、全部不回写，杜绝半提交。
        """
        with self._lock:
            plan: list[tuple[_WorkState, dict[str, Any]]] = []
            for review in reviews:
                code = str(review.get("作业编号") or "").strip()
                seq = int(review.get("seq") or 0)
                work = self._require_work(code)
                trip = work.trips.get(seq)
                if trip is None:
                    raise ReplayError(f"趟次 {code}#{seq} 缺少回放结果，不能提交")
                if trip["事件版本"] != self._event_version:
                    raise ReplayConflict(
                        f"趟次 {code}#{seq} 基于 v{trip['事件版本']}，轨迹已更新到 "
                        f"v{self._event_version}，请重新重放复核"
                    )
                if trip.get("复核状态") != "通过":
                    raise ReplayError(f"趟次 {code}#{seq} 复核未通过，请先逐条处理")
                plan.append((work, trip))
            written = [self._write_back(work, trip) for work, trip in plan]
            return {"事件版本": self._event_version, "已提交": len(written), "回写": written}

    def _write_back(self, work: _WorkState, trip: dict[str, Any]) -> dict[str, Any]:
        ledger = self._ledger_row(work.code)
        if ledger is None:
            raise ReplayError(f"除雪台账缺少作业 {work.code}")

        # 1) 除雪台账：最近趟次、责任组（作业开始时口径）、回放版本、一致性
        ledger["最近趟次"] = trip["seq"]
        ledger["回放版本"] = f"v{trip['事件版本']}"
        ledger["责任组"] = trip["责任组"]
        ledger["跨区支援自"] = trip["跨区支援自"]
        ledger["作业卡轨迹一致"] = True
        ledger["status"] = "待复查"
        ledger["pending"] = True

        # 2) 车辆清单：按回放车辆编号回写最近除雪趟次与当前桩位
        vehicle = None
        if self._vehicle_code:
            vehicle = next(
                (row for row in store.rows(VEHICLE_MODULE)
                 if str(row.get("车辆编号")) == self._vehicle_code),
                None,
            )
        if vehicle is not None:
            vehicle["最近除雪作业"] = work.code
            vehicle["最近趟次"] = trip["seq"]
            if trip.get("终点桩号") is not None:
                vehicle["当前里程"] = trip["终点桩号"]
            vehicle["回放版本"] = f"v{trip['事件版本']}"
            if vehicle.get("status") == "在库":
                vehicle["status"] = "出车作业"

        # 3) 物资待办：按 (作业, 趟次) 幂等，重放提交不重复建待办
        todo_rows = store.rows(TODO_MODULE)
        todo_key = f"{work.code}#{trip['seq']}"
        todo = next((row for row in todo_rows if row.get("关联趟次") == todo_key), None)
        if todo is None:
            todo = {"id": max((int(row.get("id", 0)) for row in todo_rows), default=0) + 1}
            todo_rows.append(todo)
        todo.update({
            "关联作业": work.code,
            "关联趟次": todo_key,
            "作业路段": ledger.get("作业路段"),
            "责任组": trip["责任组"],
            "待办内容": f"按 {work.code} 第{trip['seq']}趟复核结论核销/补充融雪剂",
            "status": "待办",
            "pending": True,
            "abnormal": False,
            "回放版本": f"v{trip['事件版本']}",
        })
        return {
            "作业编号": work.code,
            "趟次": trip["seq"],
            "台账": True,
            "车辆": vehicle.get("车辆编号") if vehicle else None,
            "物资待办": todo["id"],
        }

    # ---- 跨区支援派工 ------------------------------------------------------

    def dispatch_work(
        self,
        work_code: str,
        *,
        responsible_group: str,
        support_from: str | None,
        seat: str,
    ) -> dict[str, Any]:
        """指挥席位在作业开始时记录责任组；已有趟次后拒绝追溯修改。"""
        with self._lock:
            work = self._require_work(work_code)
            if not responsible_group:
                raise ReplayError("责任组不能为空")
            if work.dispatch and work.trips:
                raise ReplayError("作业已有回放趟次，责任组以作业开始时记录为准，不能追溯修改")
            work.dispatch = {
                "责任组": responsible_group,
                "跨区支援自": support_from or None,
                "指挥席位": seat or "未记录席位",
                "记录时间": _now(),
            }
            return {"作业编号": work_code, **work.dispatch}

    # ---- 快照 / 存量迁移 / 物资待办 ----------------------------------------

    def get_snapshot(self, work_code: str, seq: int, *, snapshot_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            work = self._require_work(work_code)
            key = snapshot_id
            if key is None:
                trip = work.trips.get(seq)
                if trip is None:
                    raise ReplayError(f"趟次 {work_code}#{seq} 尚无快照")
                key = trip["快照编号"]
            snapshot = work.snapshots.get(str(key))
            if snapshot is None:
                raise ReplayError(f"快照 {key} 不存在")
            return deepcopy(snapshot)

    def migrate_legacy_stakes(self, *, expected_version: int | None = None) -> dict[str, Any]:
        """存量数据迁移：空桩号按相邻轨迹线性补齐，已有有效桩号不动。"""
        with self._lock:
            if expected_version is not None and expected_version != self._event_version:
                raise ReplayConflict("事件版本已变化，请刷新后再执行迁移")
            filled = fill_empty_stake(self._timeline)
            if filled:
                self._event_version += 1
                self._invalidate_cursors()
            return {"事件版本": self._event_version, "补齐桩号点": filled}

    def material_todos(self) -> list[dict[str, Any]]:
        """物资工作台：读取修正结论回写产生的物资待办。"""
        with self._lock:
            return [dict(row) for row in store.rows(TODO_MODULE)]

    def resolve_todo(self, todo_id: int) -> dict[str, Any]:
        with self._lock:
            todo = next(
                (row for row in store.rows(TODO_MODULE) if int(row.get("id", 0)) == todo_id),
                None,
            )
            if todo is None:
                raise ReplayError(f"物资待办 {todo_id} 不存在")
            todo["status"] = "已核销"
            todo["pending"] = False
            return dict(todo)


replay_service = ReplayService()
