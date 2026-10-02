"""除雪车回放领域服务。

围绕「除雪车回放趟次错配」的修复链路收口业务规则：

1. 作业卡与轨迹对不上：趟次作业卡路段与轨迹点桩号/时间轴窗口逐点比对定位；
2. 校准作业时间轴：时间轴分段窗口是错配判定口径，校准后重算问题，事件版本 +1；
3. 批量重放：多条轨迹逐条复核、整组一次提交，过程中任一条失败整体回滚；
4. 回放游标与事件版本绑定：断点重放凭游标续放，已重放趟次幂等跳过，绝不重复生成；
5. 并发补录走乐观锁：携带 expected_version，版本不符返回 409；
6. 跨区支援以作业开始时指挥席位登记的责任组为准，重放时统一纠正；
7. 历史回放快照只读，迁移与重放都不改；存量空桩号在迁移时按相邻轨迹线性补齐；
8. 修正结论一次回写除雪台账、车辆清单与物资待办。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.store import store

REPLAY_META_TABLE = "_winter_replay_meta"
TRIP_TABLE = "_winter_replay_trips"
MATERIAL_TODO_TABLE = "_winter_material_todos"

# 桩号前缀 → 作业路段：轨迹点归属哪条路以此判定
STAKE_PREFIX_ROAD = {"K": "机场快速路", "L": "南站联络线", "B": "北互通匝道"}

# 路段 → 桩号前缀，插值时沿用
ROAD_PREFIX = {road: prefix for prefix, road in STAKE_PREFIX_ROAD.items()}

REPLAY_NOT_STARTED = "未重放"
REPLAY_PARTIAL = "断点重放中"
REPLAY_DONE = "整组已提交"
SNAPSHOT_ARCHIVED = "历史快照归档"

_today = "2026-09-26"


class OptimisticLockError(Exception):
    """事件版本与回放游标绑定的乐观锁冲突。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_stake(stake: str) -> tuple[str, int] | None:
    """把 K3+200 解析成 (前缀, 米数)；空桩号返回 None。"""
    text = str(stake or "").strip()
    if not text or "+" not in text:
        return None
    prefix, rest = text[0], text[1:]
    head, _, tail = rest.partition("+")
    if prefix not in STAKE_PREFIX_ROAD or not head.isdigit() or not tail.isdigit():
        return None
    return prefix, int(head) * 1000 + int(tail)


def format_stake(prefix: str, meters: int) -> str:
    meters = max(meters, 0)
    return f"{prefix}{meters // 1000}+{meters % 1000:03d}"


def road_of_stake(stake: str) -> str | None:
    parsed = parse_stake(stake)
    return STAKE_PREFIX_ROAD[parsed[0]] if parsed else None


class ReplayService:
    def __init__(self) -> None:
        self._bootstrapped = False

    # ------------------------------------------------------------------ 引导数据
    def bootstrap(self) -> None:
        """构造三个可复现场景；幂等，已引导过直接返回。"""
        if self._bootstrapped or store.rows(REPLAY_META_TABLE):
            self._bootstrapped = True
            return
        self._seed_works()
        self._bootstrapped = True

    def _seed_works(self) -> None:
        # 先把除雪台账里的占位样例改成真实作业，列表页/详情页/物资工作台口径一致
        ledger_updates = {
            "WINT-0001": {
                "作业路段": "机场快速路（K0+000~K8+000）",
                "作业日期": _today,
                "融雪剂用量": "待复核回写",
                "作业车辆": "吉A·X8201",
                "作业班组": "机场班组",
                "路面状况": "中雪·部分压实",
                "作业状态": "待作业",
            },
            "WINT-0002": {
                "作业路段": "南站联络线（L0+000~L6+000）",
                "作业日期": _today,
                "融雪剂用量": "待复核回写",
                "作业车辆": "吉A·X8201",
                "作业班组": "机场班组（跨区支援）",
                "路面状况": "小雪·湿滑",
                "作业状态": "作业中",
            },
            "WINT-0003": {
                "作业路段": "北互通匝道（B0+000~B3+200）",
                "作业日期": "2026-09-20",
                "融雪剂用量": "1.8 吨",
                "作业车辆": "吉A·X8506",
                "作业班组": "北站班组",
                "路面状况": "中雪",
                "作业状态": "已完成",
            },
        }
        for row in store.rows("winter"):
            code = str(row.get("作业编号") or "")
            if code in ledger_updates:
                row.update(ledger_updates[code])

        # 车辆清单补两台除雪车（已存在则不重复追加）
        vehicles = store.rows("vehicle")
        existing = {str(row.get("车牌号")) for row in vehicles}
        next_id = max((int(row.get("id", 0)) for row in vehicles), default=0) + 1
        for plate, unit in [("吉A·X8201", "机场除雪班"), ("吉A·X8506", "北站除雪班")]:
            if plate not in existing:
                vehicles.append({
                    "id": next_id,
                    "status": "出车作业",
                    "pending": True,
                    "abnormal": False,
                    "车辆编号": f"VEHI-9{next_id:03d}",
                    "车辆类型": "除雪撒布车",
                    "车牌号": plate,
                    "所属单位": unit,
                    "年检日期": "2026-08-10",
                    "驾驶员": "待值班排班",
                    "当前里程": "待回放回写",
                    "车辆状态": "出车作业",
                })
                next_id += 1

        metas = [
            {
                "winter_id": 1,
                "作业编号": "WINT-0001",
                "作业路段": "机场快速路",
                "作业车辆": "吉A·X8201",
                "started_team": "机场班组",
                "cross_area": False,
                "started_at": f"{_today} 06:00:00",
                "timeline": [
                    {"section": "机场快速路", "start": f"{_today} 06:00:00", "end": f"{_today} 08:30:00"},
                ],
                "event_version": 3,
                "replay_cursor": None,
                "replay_status": REPLAY_NOT_STARTED,
                "snapshot": False,
                "last_committed_at": None,
            },
            {
                "winter_id": 2,
                "作业编号": "WINT-0002",
                "作业路段": "南站联络线",
                "作业车辆": "吉A·X8201",
                # 跨区支援：以作业开始时指挥席位登记的责任组为准
                "started_team": "机场班组",
                "cross_area": True,
                "started_at": f"{_today} 09:00:00",
                "timeline": [
                    # 初始窗口错位：南站轨迹 09:00 就开始了，旧窗口却从 10:00 起算
                    {"section": "南站联络线", "start": f"{_today} 10:00:00", "end": f"{_today} 12:00:00"},
                ],
                "event_version": 5,
                "replay_cursor": None,
                "replay_status": REPLAY_NOT_STARTED,
                "snapshot": False,
                "last_committed_at": None,
            },
            {
                "winter_id": 3,
                "作业编号": "WINT-0003",
                "作业路段": "北互通匝道",
                "作业车辆": "吉A·X8506",
                "started_team": "北站班组",
                "cross_area": False,
                "started_at": "2026-09-20 22:00:00",
                "timeline": [
                    {"section": "北互通匝道", "start": "2026-09-20 22:00:00", "end": "2026-09-20 23:30:00"},
                ],
                "event_version": 1,
                "replay_cursor": None,
                "replay_status": SNAPSHOT_ARCHIVED,
                "snapshot": True,
                "last_committed_at": None,
            },
        ]
        store.rows(REPLAY_META_TABLE).extend(metas)

        def pt(ts: str, stake: str) -> dict[str, Any]:
            return {"ts": ts, "stake": stake, "stake_filled": False}

        trips: list[dict[str, Any]] = [
            # ---- WINT-0001：路段转换时作业卡与轨迹对不上，且有一处空桩号 ----
            {"id": 101, "winter_id": 1, "ordinal": 1, "source": "回放",
             "card_section": "机场快速路", "recorded_team": "机场班组",
             "duplicate_of": None, "status": "待重放", "snapshot": False,
             "event_version_at_create": 1,
             "points": [pt(f"{_today} 06:00:00", "K0+100"), pt(f"{_today} 06:12:00", "K1+100"),
                        pt(f"{_today} 06:24:00", "K2+100")]},
            {"id": 102, "winter_id": 1, "ordinal": 2, "source": "回放",
             "card_section": "机场快速路", "recorded_team": "机场班组",
             "duplicate_of": None, "status": "待重放", "snapshot": False,
             "event_version_at_create": 1,
             "points": [pt(f"{_today} 06:36:00", "K2+400"), pt(f"{_today} 06:48:00", ""),
                        pt(f"{_today} 07:00:00", "K4+400")]},
            # 转换到下一作业路段：作业卡仍是机场快速路，整条轨迹已切进南站联络线
            {"id": 103, "winter_id": 1, "ordinal": 3, "source": "回放",
             "card_section": "机场快速路", "recorded_team": "机场班组",
             "duplicate_of": None, "status": "待重放", "snapshot": False,
             "event_version_at_create": 2,
             "points": [pt(f"{_today} 07:28:00", "L0+200"), pt(f"{_today} 07:36:00", "L0+600"),
                        pt(f"{_today} 07:44:00", "L1+000"), pt(f"{_today} 07:52:00", "L1+400")]},

            # ---- WINT-0002：跨区支援 + 时间轴错位 + 补录旧趟次重复 + 空桩号 ----
            {"id": 201, "winter_id": 2, "ordinal": 1, "source": "回放",
             "card_section": "南站联络线", "recorded_team": "南站班组",  # 车载记录归属班组有误
             "duplicate_of": None, "status": "待重放", "snapshot": False,
             "event_version_at_create": 4,
             "points": [pt(f"{_today} 09:00:00", "L0+200"), pt(f"{_today} 09:12:00", "L1+200"),
                        pt(f"{_today} 09:24:00", "L2+200")]},
            # 补录趟次：补录后旧趟次仍被重复展示，旧趟次 202 已被 203 取代
            {"id": 202, "winter_id": 2, "ordinal": 2, "source": "回放",
             "card_section": "南站联络线", "recorded_team": "南站班组",
             "duplicate_of": 203, "status": "已重放", "snapshot": False,
             "event_version_at_create": 3,
             "points": [pt(f"{_today} 09:30:00", "L2+400"), pt(f"{_today} 09:40:00", "L3+000")]},
            {"id": 203, "winter_id": 2, "ordinal": 3, "source": "补录",
             "card_section": "南站联络线", "recorded_team": "机场班组",
             "duplicate_of": None, "status": "待重放", "snapshot": False,
             "event_version_at_create": 5,
             "points": [pt(f"{_today} 09:30:00", "L2+400"), pt(f"{_today} 09:42:00", "L3+400"),
                        pt(f"{_today} 09:54:00", ""), pt(f"{_today} 10:06:00", "L4+600")]},
            # 作业卡错挂成北互通匝道，轨迹实际在南站联络线
            {"id": 204, "winter_id": 2, "ordinal": 4, "source": "回放",
             "card_section": "北互通匝道", "recorded_team": "南站班组",
             "duplicate_of": None, "status": "待重放", "snapshot": False,
             "event_version_at_create": 4,
             "points": [pt(f"{_today} 10:18:00", "L4+800"), pt(f"{_today} 10:30:00", "L5+600")]},

            # ---- WINT-0003：历史回放快照，含空桩号，但任何修正都不得改动它 ----
            {"id": 301, "winter_id": 3, "ordinal": 1, "source": "回放",
             "card_section": "北互通匝道", "recorded_team": "北站班组",
             "duplicate_of": None, "status": "已重放", "snapshot": True,
             "event_version_at_create": 1,
             "points": [pt("2026-09-20 22:00:00", "B0+200"), pt("2026-09-20 22:12:00", ""),
                        pt("2026-09-20 22:24:00", "B1+800"), pt("2026-09-20 22:36:00", "B2+600")]},
        ]
        store.rows(TRIP_TABLE).extend(trips)

        todos = [
            {"id": 1, "winter_id": 1, "作业编号": "WINT-0001", "材料名称": "环保融雪剂",
             "应配数量": "按复核趟次核算", "status": "待办", "来源": "回放复核", "备注": "趟次错配未复核，暂不发料"},
            {"id": 2, "winter_id": 2, "作业编号": "WINT-0002", "材料名称": "环保融雪剂",
             "应配数量": "按复核趟次核算", "status": "待办", "来源": "回放复核", "备注": "跨区支援班组待确认"},
        ]
        store.rows(MATERIAL_TODO_TABLE).extend(todos)

    # ------------------------------------------------------------------ 读取
    def list_works(self, *, keyword: str | None = None, mismatched_only: bool = False) -> list[dict[str, Any]]:
        self.bootstrap()
        works: list[dict[str, Any]] = []
        for meta in store.rows(REPLAY_META_TABLE):
            detail = self._build_detail(meta)
            if keyword and keyword not in detail["作业编号"]:
                continue
            if mismatched_only and not detail["问题汇总"]["total"]:
                continue
            works.append({
                "winter_id": detail["winter_id"],
                "作业编号": detail["作业编号"],
                "作业路段": detail["作业路段"],
                "作业车辆": detail["作业车辆"],
                "责任组": detail["责任组"],
                "replay_status": detail["replay_status"],
                "event_version": detail["event_version"],
                "replay_cursor": detail["replay_cursor"],
                "snapshot": detail["snapshot"],
                "问题汇总": detail["问题汇总"],
            })
        return works

    def get_work(self, winter_id: int) -> dict[str, Any] | None:
        self.bootstrap()
        meta = self._meta(winter_id)
        return self._build_detail(meta) if meta else None

    def list_material_todos(self, *, status: str | None = None) -> list[dict[str, Any]]:
        self.bootstrap()
        rows = store.rows(MATERIAL_TODO_TABLE)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return [dict(row) for row in rows]

    def get_snapshot_view(self, winter_id: int) -> dict[str, Any] | None:
        """历史路线按原回放快照呈现，返回深拷贝，调用方改不到底层数据。"""
        self.bootstrap()
        meta = self._meta(winter_id)
        if meta is None or not meta.get("snapshot"):
            return None
        trips = [t for t in store.rows(TRIP_TABLE) if int(t["winter_id"]) == winter_id]
        return {
            "作业编号": meta["作业编号"],
            "snapshot": True,
            "snapshot_note": "历史路线保持原回放快照不变，空桩号与旧卡段均不修正",
            "event_version": meta["event_version"],
            "trips": deepcopy(sorted(trips, key=lambda t: int(t["ordinal"]))),
        }

    # ------------------------------------------------------------------ 修正动作
    def calibrate_timeline(self, winter_id: int, timeline: list[dict[str, Any]], expected_version: int) -> dict[str, Any]:
        """校准作业时间轴：校验乐观锁后落新窗口，事件版本 +1，错配按新口径重算。"""
        self.bootstrap()
        meta = self._meta(winter_id)
        if meta is None:
            raise KeyError(winter_id)
        if meta.get("snapshot"):
            raise ValueError("历史回放快照只读，不允许校准时间轴")
        self._check_lock(meta, expected_version)
        cleaned = []
        for seg in timeline:
            section = str(seg.get("section") or "").strip()
            start = str(seg.get("start") or "").strip()
            end = str(seg.get("end") or "").strip()
            if not section or not start or not end:
                raise ValueError("时间轴分段必须包含路段、开始时间与结束时间")
            if end <= start:
                raise ValueError(f"分段「{section}」结束时间必须晚于开始时间")
            cleaned.append({"section": section, "start": start, "end": end})
        snapshot = store.snapshot()
        try:
            meta["timeline"] = cleaned
            meta["event_version"] += 1
            detail = self._build_detail(meta)
        except Exception:
            store.restore(snapshot)
            raise
        return detail

    def supplement_trip(
        self,
        winter_id: int,
        trip_id: int,
        values: dict[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        """并发补录走乐观锁：版本不符抛 OptimisticLockError；补录不新增重复趟次。"""
        self.bootstrap()
        meta = self._meta(winter_id)
        if meta is None:
            raise KeyError(winter_id)
        self._check_lock(meta, expected_version)
        trip = self._trip(trip_id, winter_id)
        if trip is None:
            raise ValueError(f"趟次 {trip_id} 不属于作业 {meta['作业编号']}")
        if trip.get("snapshot"):
            raise ValueError("历史回放快照只读，不允许补录")
        if trip.get("duplicate_of"):
            raise ValueError(f"该趟次已被补录趟次 {trip['duplicate_of']} 取代，请直接补录新趟次")
        snapshot = store.snapshot()
        try:
            card_section = str(values.get("card_section") or trip["card_section"]).strip()
            points = values.get("points")
            if points is not None:
                normalized = []
                for point in points:
                    normalized.append({
                        "ts": str(point.get("ts") or "").strip(),
                        "stake": str(point.get("stake") or "").strip(),
                        "stake_filled": bool(point.get("stake_filled", False)),
                    })
                trip["points"] = normalized
            trip["card_section"] = card_section
            if values.get("recorded_team"):
                trip["recorded_team"] = str(values["recorded_team"]).strip()
            # 补录动作本身幂等：同一趟次再次提交不会再造一条
            trip["source"] = "补录"
            trip["status"] = "待重放"
            meta["event_version"] += 1
            detail = self._build_detail(meta)
        except Exception:
            store.restore(snapshot)
            raise
        return detail

    # ------------------------------------------------------------------ 迁移
    def migrate_legacy_stakes(self) -> dict[str, Any]:
        """存量数据迁移：非快照趟次的空桩号按相邻轨迹线性补齐；历史快照原样保留。"""
        self.bootstrap()
        snapshot = store.snapshot()
        try:
            filled: list[dict[str, Any]] = []
            skipped_snapshot = 0
            changed_works: set[int] = set()
            for trip in store.rows(TRIP_TABLE):
                if trip.get("snapshot"):
                    if any(not str(p.get("stake") or "").strip() for p in trip["points"]):
                        skipped_snapshot += 1
                    continue
                points = trip["points"]
                for index, point in enumerate(points):
                    if str(point.get("stake") or "").strip():
                        continue
                    prev_point = self._nearest_known(points, index, -1)
                    next_point = self._nearest_known(points, index, +1)
                    if prev_point is None or next_point is None:
                        continue
                    stake = self._interpolate_stake(prev_point, next_point, index, points)
                    if stake is None:
                        continue
                    point["stake"] = stake
                    point["stake_filled"] = True
                    filled.append({"trip_id": trip["id"], "ordinal": index + 1, "stake": stake})
                    changed_works.add(int(trip["winter_id"]))
            # 迁移按作业推进一次事件版本，而不是每补一个点都 +1
            for winter_id in changed_works:
                meta = self._meta(winter_id)
                if meta and not meta.get("snapshot"):
                    meta["event_version"] += 1
            return {"filled": filled, "skipped_snapshot": skipped_snapshot}
        except Exception:
            store.restore(snapshot)
            raise

    def _nearest_known(self, points: list[dict[str, Any]], index: int, direction: int) -> dict[str, Any] | None:
        cursor = index + direction
        while 0 <= cursor < len(points):
            if parse_stake(points[cursor].get("stake", "")):
                return points[cursor]
            cursor += direction
        return None

    def _interpolate_stake(
        self,
        prev_point: dict[str, Any],
        next_point: dict[str, Any],
        index: int,
        points: list[dict[str, Any]],
    ) -> str | None:
        prev = parse_stake(prev_point["stake"])
        nxt = parse_stake(next_point["stake"])
        if prev is None or nxt is None or prev[0] != nxt[0]:
            return None
        # 相邻轨迹之间按序号位置线性插值
        prev_index = next(i for i, p in enumerate(points) if p is prev_point)
        next_index = next(i for i, p in enumerate(points) if p is next_point)
        ratio = (index - prev_index) / (next_index - prev_index)
        meters = round(prev[1] + (nxt[1] - prev[1]) * ratio)
        return format_stake(prev[0], meters)

    # ------------------------------------------------------------------ 批量重放
    def replay_trips(
        self,
        winter_id: int,
        trip_ids: list[int],
        *,
        expected_version: int,
        expected_cursor: int | None,
    ) -> dict[str, Any]:
        """批量重放：逐条复核、整组一次提交；任一失败整体回滚。

        - 回放游标与事件版本绑定，两者都要与服务端当前状态一致；
        - 断点重放时，已重放/已标记重复的趟次幂等跳过，绝不重复生成趟次。
        """
        self.bootstrap()
        meta = self._meta(winter_id)
        if meta is None:
            raise KeyError(winter_id)
        if meta.get("snapshot"):
            raise ValueError("历史回放快照只读，不参与重放")
        self._check_lock(meta, expected_version)
        current_cursor = meta.get("replay_cursor")
        if current_cursor is not None and expected_cursor != current_cursor:
            raise OptimisticLockError(
                f"回放游标已推进到第 {current_cursor} 趟，请基于最新游标断点重放"
            )

        selected = [self._trip(int(tid), winter_id) for tid in trip_ids]
        if any(trip is None for trip in selected):
            raise ValueError("提交的趟次中有不属于本作业的编号")
        selected = sorted(selected, key=lambda t: int(t["ordinal"]))

        # 先逐条复核：存在未消除的错配就整组拦下，不产生任何半截结果
        blockers: list[dict[str, Any]] = []
        for trip in selected:
            if trip.get("duplicate_of") or trip.get("snapshot"):
                continue
            issues = self._trip_issues(meta, trip)
            serious = [issue for issue in issues if issue["类型"] != "重复旧趟次"]
            if serious:
                blockers.append({"trip_id": trip["id"], "issues": serious})
        if blockers:
            raise ValueError({"blockers": blockers})

        snapshot = store.snapshot()
        results: list[dict[str, Any]] = []
        try:
            warnings: list[str] = []
            for trip in selected:
                ordinal = int(trip["ordinal"])
                if trip.get("snapshot"):
                    results.append({"trip_id": trip["id"], "ordinal": ordinal,
                                    "action": "skip", "reason": "历史回放快照保持不变"})
                    continue
                if trip.get("duplicate_of"):
                    # 补录后旧趟次不再重复展示，断点重放也不会再生成一遍
                    if trip["status"] != "重复跳过":
                        trip["status"] = "重复跳过"
                    results.append({"trip_id": trip["id"], "ordinal": ordinal,
                                    "action": "dedupe", "reason": f"已被补录趟次 {trip['duplicate_of']} 取代"})
                    continue
                if trip["status"] == "已重放":
                    # 断点重放幂等：跳过而不是再生成一趟
                    results.append({"trip_id": trip["id"], "ordinal": ordinal,
                                    "action": "skip", "reason": "断点续放：该趟次此前已提交"})
                else:
                    team = str(trip.get("recorded_team") or "")
                    if team and team != meta["started_team"]:
                        warnings.append(
                            f"第 {ordinal} 趟车载责任组「{team}」纠正为作业开始时指挥席位登记的「{meta['started_team']}」"
                        )
                    trip["recorded_team"] = meta["started_team"]
                    trip["status"] = "已重放"
                    results.append({"trip_id": trip["id"], "ordinal": ordinal,
                                    "action": "replayed", "责任组": meta["started_team"]})
                meta["replay_cursor"] = ordinal

            still_pending = [
                t for t in store.rows(TRIP_TABLE)
                if int(t["winter_id"]) == winter_id
                and not t.get("snapshot")
                and not t.get("duplicate_of")
                and t["status"] != "已重放"
            ]
            meta["event_version"] += 1
            # 修正结论只在整组复核完成时一次回写；断点中途不闭环台账/车辆/物资
            writeback: dict[str, Any] | None = None
            if still_pending:
                meta["replay_status"] = REPLAY_PARTIAL
            else:
                writeback = self._writeback(meta)
                meta["replay_status"] = REPLAY_DONE
                meta["replay_cursor"] = None
                meta["last_committed_at"] = _now()
            return {
                "作业编号": meta["作业编号"],
                "event_version": meta["event_version"],
                "replay_cursor": meta["replay_cursor"],
                "replay_status": meta["replay_status"],
                "results": results,
                "warnings": warnings,
                "writeback": writeback,
            }
        except Exception:
            store.restore(snapshot)
            raise

    def _writeback(self, meta: dict[str, Any]) -> dict[str, Any]:
        """修正结论一次回写除雪台账、车辆清单与物资待办。"""
        winter_id = int(meta["winter_id"])
        ledger = store.find("winter", winter_id)
        effective_trips = [
            t for t in store.rows(TRIP_TABLE)
            if int(t["winter_id"]) == winter_id and not t.get("duplicate_of") and not t.get("snapshot")
        ]
        replayed = [t for t in effective_trips if t["status"] == "已重放"]
        total_distance_km = self._distance_km(effective_trips)
        deicer = f"{round(total_distance_km * 0.6, 1)} 吨"

        ledger_writeback: dict[str, Any] = {}
        if ledger is not None:
            ledger["作业班组"] = meta["started_team"] + ("（跨区支援）" if meta.get("cross_area") else "")
            ledger["融雪剂用量"] = deicer
            ledger["作业状态"] = "待复查"
            ledger["status"] = "待复查"
            ledger["回放趟次数"] = len(effective_trips)
            ledger["回放结论"] = (
                f"已复核 {len(replayed)}/{len(effective_trips)} 趟，"
                f"跨区支援责任组「{meta['started_team']}」，复核里程 {total_distance_km} km"
            )
            ledger_writeback = {"作业班组": ledger["作业班组"], "作业状态": ledger["作业状态"],
                                "回放趟次数": ledger["回放趟次数"], "融雪剂用量": deicer,
                                "回放结论": ledger["回放结论"]}

        vehicle_writeback: dict[str, Any] = {}
        plate = meta.get("作业车辆")
        for vehicle in store.rows("vehicle"):
            if vehicle.get("车牌号") == plate:
                vehicle["车辆状态"] = "回放已核对"
                vehicle["status"] = "出车作业"
                vehicle["最近回放作业"] = meta["作业编号"]
                vehicle["当前里程"] = f"复核里程 {total_distance_km} km"
                vehicle_writeback = {"车牌号": plate, "车辆状态": "回放已核对",
                                     "最近回放作业": meta["作业编号"], "当前里程": vehicle["当前里程"]}
                break

        material_writeback: dict[str, Any] = {}
        for todo in store.rows(MATERIAL_TODO_TABLE):
            if int(todo.get("winter_id", 0)) == winter_id and todo["status"] == "待办":
                todo["status"] = "已回写"
                todo["应配数量"] = deicer
                todo["备注"] = f"整组重放已提交（{_now()}）"
                material_writeback = {"材料名称": todo["材料名称"], "应配数量": deicer, "status": "已回写"}
                break

        return {"除雪台账": ledger_writeback, "车辆清单": vehicle_writeback, "物资待办": material_writeback}

    def _distance_km(self, trips: list[dict[str, Any]]) -> float:
        """按每条有效轨迹首末桩号粗算复核里程，用于融雪剂回写口径。"""
        total_m = 0
        for trip in trips:
            parsed = [parse_stake(p["stake"]) for p in trip["points"]]
            parsed = [s for s in parsed if s is not None]
            if len(parsed) >= 2:
                prefix = parsed[0][0]
                same_prefix = [meters for pre, meters in parsed if pre == prefix]
                if same_prefix:
                    total_m += max(same_prefix) - min(same_prefix)
        return round(total_m / 1000, 1)

    # ------------------------------------------------------------------ 判定口径
    def _build_detail(self, meta: dict[str, Any]) -> dict[str, Any]:
        trips = sorted(
            (t for t in store.rows(TRIP_TABLE) if int(t["winter_id"]) == int(meta["winter_id"])),
            key=lambda t: int(t["ordinal"]),
        )
        trip_views = [self._trip_view(meta, trip) for trip in trips]
        issue_counter: dict[str, int] = {}
        for view in trip_views:
            for issue in view["issues"]:
                issue_counter[issue["类型"]] = issue_counter.get(issue["类型"], 0) + 1
        return {
            "winter_id": meta["winter_id"],
            "作业编号": meta["作业编号"],
            "作业路段": meta["作业路段"],
            "作业车辆": meta["作业车辆"],
            "责任组": meta["started_team"],
            "cross_area": meta.get("cross_area", False),
            "started_at": meta["started_at"],
            "timeline": deepcopy(meta["timeline"]),
            "event_version": meta["event_version"],
            "replay_cursor": meta.get("replay_cursor"),
            "replay_status": meta["replay_status"],
            "snapshot": meta.get("snapshot", False),
            "last_committed_at": meta.get("last_committed_at"),
            "trips": trip_views,
            "问题汇总": {"total": sum(issue_counter.values()), "明细": issue_counter},
        }

    def _trip_view(self, meta: dict[str, Any], trip: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": trip["id"],
            "ordinal": trip["ordinal"],
            "source": trip["source"],
            "card_section": trip["card_section"],
            "recorded_team": trip.get("recorded_team"),
            "effective_team": meta["started_team"],
            "status": trip["status"],
            "snapshot": trip.get("snapshot", False),
            "duplicate_of": trip.get("duplicate_of"),
            "points": deepcopy(trip["points"]),
            "issues": [] if trip.get("snapshot") else self._trip_issues(meta, trip),
        }

    def _trip_issues(self, meta: dict[str, Any], trip: dict[str, Any]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if trip.get("duplicate_of"):
            issues.append({"类型": "重复旧趟次", "说明": f"已被补录趟次 {trip['duplicate_of']} 取代，重放时去重跳过"})
        card = str(trip.get("card_section") or "")
        windows = {seg["section"]: (seg["start"], seg["end"]) for seg in meta.get("timeline", [])}
        for index, point in enumerate(trip["points"], start=1):
            stake = str(point.get("stake") or "").strip()
            ts = str(point.get("ts") or "").strip()
            if not stake:
                issues.append({"类型": "空桩号", "说明": f"第 {index} 个轨迹点缺桩号，需迁移时按相邻轨迹补齐"})
                continue
            road = road_of_stake(stake)
            if road is None:
                issues.append({"类型": "桩号异常", "说明": f"第 {index} 点桩号「{stake}」无法识别"})
                continue
            if road != card:
                issues.append({
                    "类型": "作业卡错配",
                    "说明": f"第 {index} 点桩号 {stake} 属「{road}」，作业卡仍挂「{card}」",
                })
            window = windows.get(road)
            if window and ts and not (window[0] <= ts <= window[1]):
                issues.append({
                    "类型": "时间轴错位",
                    "说明": f"第 {index} 点 {ts} 不在「{road}」校准窗口 {window[0]} ~ {window[1]} 内",
                })
        return issues

    # ------------------------------------------------------------------ 内部工具
    def _meta(self, winter_id: int) -> dict[str, Any] | None:
        for row in store.rows(REPLAY_META_TABLE):
            if int(row["winter_id"]) == winter_id:
                return row
        return None

    def _trip(self, trip_id: int, winter_id: int) -> dict[str, Any] | None:
        for row in store.rows(TRIP_TABLE):
            if int(row["id"]) == trip_id and int(row["winter_id"]) == winter_id:
                return row
        return None

    def _check_lock(self, meta: dict[str, Any], expected_version: int) -> None:
        if int(expected_version) != int(meta["event_version"]):
            raise OptimisticLockError(
                f"事件版本已变化（当前 v{meta['event_version']}，提交基于 v{expected_version}），请刷新后重试"
            )


replay_service = ReplayService()
