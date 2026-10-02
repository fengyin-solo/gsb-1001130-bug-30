"""除雪车回放修复的端到端验证。

覆盖用户描述的完整操作链：列表页按作业编号定位错配路段 → 详情页校准时间轴
→ 批量重放并逐条复核 → 整组提交回写台账/车辆/物资待办；以及跨区支援口
径、历史快照冻结、空桩号迁移、游标版本绑定、乐观锁与断点幂等等不变量。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.replay import fill_empty_stake, replay_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_demo():
    client.post("/api/winter/replay/demo")
    yield


def get_work(code: str) -> dict:
    return client.get(f"/api/winter/replay/works/{code}").json()


def test_demo_seed_reproduces_mismatch() -> None:
    """列表页与详情页应能定位到交接尾点造成的错配。"""
    items = {row["作业编号"]: row for row in client.get("/api/winter/replay/works").json()["items"]}
    a = items["WINT-0002"]
    b = items["WINT-0003"]
    # A 的尾点（08:02，设备仍挂 A）落在 B 的开始边界之后
    assert a["错配点数"] == 1
    assert a["窗外尾点"] == 1
    # B 窗内吃到一个设备上报为 A 的漂移点
    assert b["窗内漂移点"] == 1
    assert a["作业卡轨迹一致"] is False
    detail = get_work("WINT-0002")
    assert detail["落到窗外的点"][0]["时间"] == "2026-09-02T08:02:00"


def test_naive_segmentation_is_not_used_window_assignment() -> None:
    """修复核心：B 的作业时间窗必须吃到交接尾点而非从下一有效点起。"""
    detail = get_work("WINT-0003")
    # 尾点（空桩号）归入 B 窗：按设备编号切段会把它留在 A；
    # 之后由桩号折返拆成两趟次（500→400 返程）
    assert detail["归属段"][0]["桩号序列"] == [None, 400.0, 500.0, 400.0]
    replay = client.post(
        "/api/winter/replay/batch", json={"work_codes": ["WINT-0003"]}
    ).json()
    seq_stakes = [
        (trip["起点桩号"], trip["终点桩号"])
        for trip in replay["结果"][0]["趟次"]
    ]
    assert seq_stakes == [(400.0, 500.0), (400.0, 400.0)]


def test_calibrate_timeline_then_replay_matches_cards() -> None:
    """校准 A 的结束边界到尾点之前后，A 不再丢尾点、B 不再吃漂移点。"""
    detail = get_work("WINT-0002")
    assert detail["事件版本"] == 1
    resp = client.post(
        "/api/winter/replay/works/WINT-0002/calibrate",
        json={
            "expected_version": 1,
            "updates": [
                # A 在交接尾点之前正式结束
                {"原时间": "2026-09-02T08:05:00", "时间": "2026-09-02T08:01:30",
                 "边界": "end", "作业编号": "WINT-0002"},
                # 交接尾点的设备归属漂移校准到 B（物理上它已在 B 路段）
                {"原时间": "2026-09-02T08:02:00", "时间": "2026-09-02T08:02:00",
                 "作业编号": "WINT-0003"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["事件版本"] == 2

    a = get_work("WINT-0002")
    b = get_work("WINT-0003")
    assert a["错配点数"] == 0
    assert a["归属段"][0]["桩号序列"] == [100.0, None, 200.0]
    # 尾点仍在 B 窗（它在 B 开始之后），归属校准后 B 窗不再有漂移点
    assert b["归属段"][0]["桩号序列"] == [None, 400.0, 500.0, 400.0]
    # 轨迹更新后游标与事件版本重新绑定，旧游标失效
    assert a["游标有效"] is False


def test_calibrate_uses_optimistic_version_lock() -> None:
    resp = client.post(
        "/api/winter/replay/works/WINT-0002/calibrate",
        json={"expected_version": 99, "updates": [
            {"时间": "2026-09-02T08:01:30", "边界": "end", "作业编号": "WINT-0002"}]},
    )
    assert resp.status_code == 409
    # 冲突时改动零生效
    assert get_work("WINT-0002")["事件版本"] == 1


def test_calibrate_transaction_rolls_back_bad_change() -> None:
    before = len(get_work("WINT-0002")["时间轴"])
    resp = client.post(
        "/api/winter/replay/works/WINT-0002/calibrate",
        json={"expected_version": 1, "updates": [
            {"原时间": "2026-09-02T08:00:00", "时间": "2026-09-02T09:00:00",
             "边界": "start", "作业编号": "WINT-0002"},
            {"原时间": "1999-01-01T00:00:00", "时间": "2026-09-02T08:01:30"},
        ]},
    )
    assert resp.status_code == 400
    detail = get_work("WINT-0002")
    assert detail["事件版本"] == 1
    assert len(detail["时间轴"]) == before


def test_supplement_conflict_under_concurrency() -> None:
    """并发补录：后提交者版本过期必须被拒，不能覆盖先到的更新。"""
    first = client.post(
        "/api/winter/replay/works/WINT-0002/supplement",
        json={"expected_version": 1, "points": [
            {"时间": "2026-09-02T08:00:15", "桩号": 150.0}]},
    )
    assert first.status_code == 200
    stale = client.post(
        "/api/winter/replay/works/WINT-0002/supplement",
        json={"expected_version": 1, "points": [
            {"时间": "2026-09-02T08:00:45", "桩号": 175.0}]},
    )
    assert stale.status_code == 409


def test_supplement_after_replay_replaces_not_duplicates() -> None:
    """补录后再回看：旧趟次进入冻结快照，当前视图只显示新版本，不重复。"""
    client.post("/api/winter/replay/works/WINT-0003/trips/next", json={"expected_version": 1})
    client.post("/api/winter/replay/works/WINT-0003/trips/next", json={"expected_version": 1})
    work = get_work("WINT-0003")
    assert len(work["趟次"]) == 2
    old_snapshot = work["趟次"][0]["快照编号"]
    snap = client.get(f"/api/winter/replay/works/WINT-0003/snapshots/1").json()
    assert snap["快照编号"] == old_snapshot

    # 补录使事件版本推进，游标失效
    resp = client.post(
        "/api/winter/replay/works/WINT-0003/supplement",
        json={"expected_version": 1, "points": [
            {"时间": "2026-09-02T08:03:30", "桩号": 450.0}]},
    )
    assert resp.json()["事件版本"] == 2
    work2 = get_work("WINT-0003")
    assert work2["趟次"] == []  # 当前视图清空，等重放
    assert work2["游标有效"] is False

    # 重放后新版本趟次唯一；旧快照仍可回看且内容冻结
    client.post("/api/winter/replay/works/WINT-0003/trips/next", json={"expected_version": 2})
    work3 = get_work("WINT-0003")
    assert len(work3["趟次"]) == 1
    assert work3["趟次"][0]["快照编号"] != old_snapshot
    frozen = client.get(
        f"/api/winter/replay/works/WINT-0003/snapshots/1?snapshot_id={old_snapshot}"
    ).json()
    assert frozen["快照编号"] == old_snapshot
    assert frozen["事件版本"] == 1


def test_resume_replay_is_idempotent() -> None:
    """断点重放：同版本重复请求同一趟次不能重复生成。"""
    r1 = client.post("/api/winter/replay/works/WINT-0003/trips/next", json={"expected_version": 1}).json()
    r2 = client.post("/api/winter/replay/works/WINT-0003/trips/next", json={"expected_version": 1}).json()
    # 第二次游标推进到第 2 趟；再回头模拟断点（直接调用服务层同 seq 生成）
    trips_a = replay_service._works["WINT-0003"].trips
    rebuilt = replay_service._build_or_get_trip(
        replay_service._works["WINT-0003"], 1,
        replay_service._work_passes(replay_service._works["WINT-0003"])[0],
    )
    assert rebuilt["快照编号"] == r1["趟次"]["快照编号"]
    assert len(trips_a) == 2
    assert r2["趟次"]["seq"] == 2


def test_cursor_bound_to_event_version() -> None:
    client.post("/api/winter/replay/works/WINT-0003/trips/next", json={"expected_version": 1})
    # 带着旧游标版本直接要求重放，必须 409 而不是接着旧结果走
    client.post(
        "/api/winter/replay/works/WINT-0002/calibrate",
        json={"expected_version": 1, "updates": [
            {"时间": "2026-09-02T08:01:30", "边界": "end", "作业编号": "WINT-0002"}]},
    )
    stale = client.post("/api/winter/replay/works/WINT-0003/trips/next", json={"expected_version": 1})
    assert stale.status_code == 409


def test_batch_replay_review_and_commit_writes_back_three_targets() -> None:
    # 先校准消除错配
    client.post(
        "/api/winter/replay/works/WINT-0002/calibrate",
        json={"expected_version": 1, "updates": [
            {"时间": "2026-09-02T08:01:30", "边界": "end", "作业编号": "WINT-0002"}]},
    )
    batch = client.post("/api/winter/replay/batch", json={"work_codes": ["WINT-0002", "WINT-0003"]}).json()
    result = {row["作业编号"]: row for row in batch["结果"]}
    assert result["WINT-0002"]["趟次数"] == 1
    assert result["WINT-0003"]["趟次数"] == 2
    # 批量重放幂等：再来一遍趟次数不变、快照不变
    again = client.post("/api/winter/replay/batch", json={"work_codes": ["WINT-0002", "WINT-0003"]}).json()
    again_map = {row["作业编号"]: row for row in again["结果"]}
    assert again_map["WINT-0003"]["趟次"][0]["快照编号"] == result["WINT-0003"]["趟次"][0]["快照编号"]

    # 逐条复核
    reviews = client.get("/api/winter/replay/reviews").json()["items"]
    assert len(reviews) == 3
    for item in reviews:
        ok = client.post("/api/winter/replay/reviews", json={
            "作业编号": item["作业编号"], "seq": item["seq"], "passed": True, "note": "核对一致"})
        assert ok.status_code == 200

    # 未全部通过时整组提交失败（先驳回一条试试）
    client.post("/api/winter/replay/reviews", json={
        "作业编号": "WINT-0002", "seq": 1, "passed": False, "note": "存疑"})
    bad = client.post("/api/winter/replay/commit", json={"reviews": [
        {"作业编号": r["作业编号"], "seq": r["seq"], "passed": True} for r in reviews]})
    assert bad.status_code == 400
    client.post("/api/winter/replay/reviews", json={
        "作业编号": "WINT-0002", "seq": 1, "passed": True, "note": "复核通过"})

    commit = client.post("/api/winter/replay/commit", json={"reviews": [
        {"作业编号": r["作业编号"], "seq": r["seq"], "passed": True} for r in reviews]})
    assert commit.status_code == 200
    assert commit.json()["已提交"] == 3

    # 回写除雪台账
    winter = client.get("/api/winter").json()["items"]
    ledger = {row["作业编号"]: row for row in winter}
    assert ledger["WINT-0002"]["作业卡轨迹一致"] is True
    assert ledger["WINT-0002"]["责任组"] == "一组"
    assert ledger["WINT-0003"]["最近趟次"] == 2

    # 回写车辆清单
    vehicles = client.get("/api/vehicle").json()["items"]
    vehi = next(row for row in vehicles if row["车辆编号"] == "VEHI-0002")
    assert vehi["最近除雪作业"] == "WINT-0003"
    assert vehi["最近趟次"] == 2
    assert vehi["回放版本"] == "v2"

    # 回写物资待办（物资工作台可见），提交幂等不重复建待办
    todos = client.get("/api/material/todos").json()["items"]
    assert len(todos) == 3
    client.post("/api/winter/replay/commit", json={"reviews": [
        {"作业编号": r["作业编号"], "seq": r["seq"], "passed": True} for r in reviews]})
    assert len(client.get("/api/material/todos").json()["items"]) == 3
    resolve = client.post(f"/api/material/todos/{todos[0]['id']}/resolve")
    assert resolve.json()["status"] == "已核销"


def test_cross_region_support_locks_to_start_time_group() -> None:
    # 作业开始时由指挥席位记录跨区支援
    resp = client.post("/api/winter/replay/works/WINT-0002/dispatch", json={
        "responsible_group": "支援二组", "support_from": "相邻片区", "seat": "指挥A"})
    assert resp.status_code == 200
    client.post("/api/winter/replay/works/WINT-0002/trips/next", json={"expected_version": 1})
    trip = get_work("WINT-0002")["趟次"][0]
    assert trip["责任组"] == "支援二组"
    assert trip["跨区支援自"] == "相邻片区"
    # 已有趟次后不能追溯修改责任组
    forbidden = client.post("/api/winter/replay/works/WINT-0002/dispatch", json={
        "responsible_group": "三组", "support_from": None, "seat": "指挥B"})
    assert forbidden.status_code == 400
    assert get_work("WINT-0002")["趟次"][0]["责任组"] == "支援二组"


def test_legacy_empty_stake_migration_fills_from_neighbors() -> None:
    resp = client.post("/api/winter/replay/migrate", json={"expected_version": 1})
    assert resp.status_code == 200
    assert resp.json()["补齐桩号点"] == 2
    detail = get_work("WINT-0002")
    filled = next(p for p in detail["时间轴"] if p.get("时间") == "2026-09-02T08:00:30")
    assert filled["桩号"] == 150.0  # 100 与 200 的线性插值
    # B 窗内的交接尾点为空桩号，按全局相邻轨迹点 200(08:01) 与 400(08:03)
    # 线性插值；同一时刻的开始边界事件无桩号键，不应被补
    b = get_work("WINT-0003")
    candidates = [
        p for p in b["时间轴"]
        if p.get("时间") == "2026-09-02T08:02:00" and "桩号" in p
    ]
    assert len(candidates) == 1
    assert candidates[0]["桩号"] == pytest.approx(333.333)
    assert detail["事件版本"] == 2
    # 迁移同样推进版本、游标作废；重复迁移不产生改动
    again = client.post("/api/winter/replay/migrate", json={"expected_version": 2})
    assert again.json()["补齐桩号点"] == 0
    assert again.json()["事件版本"] == 2


def test_fill_empty_stake_helpers() -> None:
    points = [
        {"桩号": None}, {"桩号": 100.0}, {"桩号": None}, {"桩号": 300.0}, {"桩号": None},
    ]
    assert fill_empty_stake(points) == 3
    assert [p["桩号"] for p in points] == [100.0, 100.0, 200.0, 300.0, 300.0]
    no_known = [{"桩号": None}, {"桩号": None}]
    assert fill_empty_stake(no_known) == 0
