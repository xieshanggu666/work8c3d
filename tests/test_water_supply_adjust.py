"""枯水期供水保障 · 审核后滚动调整配水测试。

覆盖：
- reviewed/executing 滚动调整：快照轮次演进、处置记录留痕、预警按工况刷新
- 保留已供水上报与应急出库记录：已上报乡镇不得移除/调低于已供水量（409）
- 重新校验：死水位可用水量（409）、乡镇优先级（新增/调量重置确认，
  executing 中补确认后方可上报实供）
- 幂等：调整内容与现行方案一致时归并返回（轮次不增、记录不重复）；
  调整后完成扣库容只发生一次，重复完成 409
- 角色（越权 403）与状态机（submitted/completed 不可调整 409）
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (FloodZone, RainfallEvent, Reservoir, ReservoirDispatchLog,
                        RiverNode, RiverReach, SubBasin, Supply, Township,
                        WarningRecord, WaterStation, WaterSupplyAllocation,
                        WaterSupplyEmergency, WaterSupplyPlan)
from app.services import water_supply as ws


def _seed(db):
    db.add_all([
        RiverNode(id=1, name="源头", kind="headwater"),
        RiverNode(id=2, name="库址", kind="reservoir"),
        RiverNode(id=3, name="出口", kind="outlet"),
        RiverReach(id=1, name="源→库", from_node_id=1, to_node_id=2,
                   k_hr=1.0, x_coef=0.2),
        RiverReach(id=2, name="库→出口", from_node_id=2, to_node_id=3,
                   k_hr=1.0, x_coef=0.2),
        SubBasin(id=1, name="子流域", area_km2=120.0, cn=88.0, lag_hr=1.0,
                 outlet_node_id=1),
        # 水位 11.5m，死水位 10m（100 万m³），枯水预警线 12.5m：可用 150 万m³
        Reservoir(id=1, name="测试水库", node_id=2, normal_level=12.0,
                  flood_level=13.0, crest_level=16.0,
                  storage_curve=[[10, 100], [12, 300], [14, 600], [16, 1000], [18, 1500]],
                  discharge_curve=[[14, 0], [16, 200], [18, 600]],
                  gate_max=120.0, current_level=11.5, current_storage=250.0,
                  dead_level=10.0, drought_warn_level=12.5),
        WaterStation(id=1, name="出口水位站", node_id=3,
                     thresholds={"base_level": 5.0, "blue": 6.0, "yellow": 7.0,
                                 "orange": 8.0, "red": 9.0,
                                 "rating": [[0, 5.0], [30, 7.0], [60, 9.0], [100, 11.0]]}),
        FloodZone(id=1, name="沿岸村", node_id=3, population=500,
                  low_level=6.0, high_level=8.0),
        RainfallEvent(id=1, name="测试暴雨", duration_h=6, total_mm=300.0,
                      hyetograph=[50.0] * 6),
        Township(id=1, name="上源镇", contact="刘水务", demand_m3=500),
        Township(id=2, name="下游乡", contact="赵水务", demand_m3=300),
        Township(id=3, name="山前镇", contact="钱水务", demand_m3=200),
        Supply(id=1, name="瓶装饮用水", unit="箱", stock=100, safety_stock=10),
    ])
    db.commit()


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    fac = sessionmaker(bind=engine)
    db = fac()
    _seed(db)
    db.close()
    yield fac
    engine.dispose()


def _submit(db, items=None, period=10, **kw):
    if items is None:
        items = [{"township_id": 1, "planned_m3": 300000},
                 {"township_id": 2, "planned_m3": 200000}]
    body = {"reservoir_id": 1, "period_days": period,
            "role": "manager", "operator": "孙库管",
            "reason": "连续少雨", "allocations": items}
    body.update(kw)
    return ws.submit_plan(db, body)


def _adjust(db, pid, items, period=0, role="manager", operator="孙库管", reason=""):
    return ws.adjust_plan(db, pid, {"allocations": items, "period_days": period,
                                    "reason": reason, "role": role,
                                    "operator": operator})


def _to_executing(db, pid):
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, pid, {"township_id": 2, "priority": 2, "role": "township"})
    return ws.confirm_priorities(db, pid, "刘水务", "township")


# ---------------- reviewed 状态滚动调整 ----------------
def test_adjust_in_reviewed_rolls_snapshot_and_resets_priority(factory):
    db = factory()
    plan = _submit(db)
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, pid, {"township_id": 2, "priority": 2, "role": "township"})

    # 调量乡镇 1（30万→35万）+ 新增乡镇 3 + 核减乡镇 2，周期 10→14
    plan = _adjust(db, pid, [{"township_id": 1, "planned_m3": 350000},
                             {"township_id": 3, "planned_m3": 100000}],
                   period=14, reason="旱情发展，山前镇纳入供水")
    assert plan["adjust_round"] == 1
    assert plan["period_days"] == 14
    assert plan["total_planned_m3"] == 450000
    by_tid = {a["township_id"]: a for a in plan["allocations"]}
    assert set(by_tid) == {1, 3}
    # 调量/新增乡镇须重新确认优先级
    assert by_tid[1]["priority_confirmed"] is False
    assert by_tid[3]["priority_confirmed"] is False
    # 快照滚动：轮次历史与调整差量
    snap = plan["plan"]
    assert snap["adjust_round"] == 1 and len(snap["rounds"]) == 1
    assert snap["rounds"][0]["total_planned_m3"] == 450000
    la = plan["last_adjust"]
    assert la["added"] == ["山前镇"] and la["removed"] == ["下游乡"]
    assert la["changed"] == ["上源镇"] and la["by"] == "孙库管"
    # 处置记录留痕
    assert "[滚动调整·第1轮]" in plan["remark"] and "旱情发展" in plan["remark"]
    # 预警按调整后方案刷新（等级保留、文案更新、状态保留 active）
    warn = db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == pid).one()
    assert "450000" in warn.message and "第1轮" in warn.message
    assert warn.status == "active"

    # 未重新确认齐全前不能启动执行
    with pytest.raises(HTTPException) as ei:
        ws.confirm_priorities(db, pid, "刘水务", "township")
    assert ei.value.status_code == 409
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, pid, {"township_id": 3, "priority": 2, "role": "township"})
    plan = ws.confirm_priorities(db, pid, "刘水务", "township")
    assert plan["status"] == "executing"
    db.close()


def test_adjust_keeps_unchanged_priority_confirmation(factory):
    """水量未变的乡镇保留优先级确认，仅调量乡镇重置。"""
    db = factory()
    plan = _submit(db)
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, pid, {"township_id": 2, "priority": 2, "role": "township"})

    plan = _adjust(db, pid, [{"township_id": 1, "planned_m3": 300000},
                             {"township_id": 2, "planned_m3": 250000}])
    by_tid = {a["township_id"]: a for a in plan["allocations"]}
    assert by_tid[1]["priority_confirmed"] is True   # 未变，保留确认
    assert by_tid[2]["priority_confirmed"] is False  # 调量，需重新确认
    db.close()


# ---------------- executing 状态滚动调整：保留已供水与应急出库 ----------------
def test_adjust_in_executing_keeps_delivery_and_emergency(factory):
    db = factory()
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 200000}])
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 120000,
                                 "role": "township"})
    # 欠供 80000 → 物资管理员追加应急物资（直接出库）
    stock_before = db.get(Supply, 1).stock
    ws.add_emergency(db, pid, {"supply_id": 1, "quantity": 30, "township_id": 1,
                               "role": "supply_manager", "operator": "陈物资"})

    # 滚动调整：乡镇 1 调量 20万→15万（≥已供 12万），新增乡镇 2 配 10万
    plan = _adjust(db, pid, [{"township_id": 1, "planned_m3": 150000},
                             {"township_id": 2, "planned_m3": 100000}],
                   role="dispatcher", operator="张调度")
    assert plan["adjust_round"] == 1 and plan["status"] == "executing"
    by_tid = {a["township_id"]: a for a in plan["allocations"]}
    # 已供水记录保留
    assert by_tid[1]["delivered_m3"] == 120000
    assert by_tid[1]["shortage_m3"] == 30000  # 欠供按新计划量重算
    # 应急出库记录保留、库存不因调整变动
    assert len(plan["emergencies"]) == 1
    assert plan["emergencies"][0]["quantity"] == 30
    assert db.get(Supply, 1).stock == stock_before - 30
    # 预警保持处置中
    warn = db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == pid).one()
    assert warn.status == "handling" and "250000" in warn.message

    # 新增乡镇未确认优先级前不能上报实供
    with pytest.raises(HTTPException) as ei:
        ws.report_delivery(db, pid, {"township_id": 2, "delivered_m3": 50000,
                                     "role": "township"})
    assert ei.value.status_code == 409
    # executing 中补确认优先级后可上报
    ws.set_priority(db, pid, {"township_id": 2, "priority": 2, "role": "township"})
    plan = ws.report_delivery(db, pid, {"township_id": 2, "delivered_m3": 50000,
                                        "role": "township"})
    by_tid = {a["township_id"]: a for a in plan["allocations"]}
    assert by_tid[2]["delivered_m3"] == 50000
    db.close()


def test_adjust_cannot_cut_below_delivered_or_remove_reported(factory):
    """已上报乡镇：计划量不得调低于已供水量、不得从方案中移除。"""
    db = factory()
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 200000},
                              {"township_id": 2, "planned_m3": 100000}])
    pid = plan["id"]
    _to_executing(db, pid)
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 150000,
                                 "role": "township"})

    # 调低于已供水量 409
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, [{"township_id": 1, "planned_m3": 100000},
                          {"township_id": 2, "planned_m3": 100000}])
    assert ei.value.status_code == 409
    # 移除已上报乡镇 409
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, [{"township_id": 2, "planned_m3": 100000}])
    assert ei.value.status_code == 409
    # 单据状态与明细未被破坏
    plan = ws.get_plan(db, pid)
    assert plan["status"] == "executing" and plan["adjust_round"] == 0
    assert plan["total_planned_m3"] == 300000
    db.close()


# ---------------- 死水位重校 ----------------
def test_adjust_over_available_water_rejected(factory):
    db = factory()
    plan = _submit(db)
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    # 死水位以上可用 150 万m³，调整后 160 万m³ 超限
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, [{"township_id": 1, "planned_m3": 1600000}])
    assert ei.value.status_code == 409
    assert "死水位" in ei.value.detail
    # 恰好可用上限可通过
    plan = _adjust(db, pid, [{"township_id": 1, "planned_m3": 1500000}])
    assert plan["total_planned_m3"] == 1500000
    db.close()


# ---------------- 幂等：重复提交不重复生效 ----------------
def test_adjust_idempotent_resubmit(factory):
    """重复提交同一调整：幂等归并，轮次/记录/预警不重复。"""
    db = factory()
    plan = _submit(db)
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")

    items = [{"township_id": 1, "planned_m3": 350000},
             {"township_id": 2, "planned_m3": 200000}]
    # 与现行方案完全一致 → 直接归并，不产生调整轮次
    plan = _adjust(db, pid, [{"township_id": 1, "planned_m3": 300000},
                             {"township_id": 2, "planned_m3": 200000}])
    assert plan["adjust_round"] == 0
    assert "[滚动调整" not in (plan["remark"] or "")

    # 第一次有效调整
    plan = _adjust(db, pid, items, reason="核增上源镇")
    assert plan["adjust_round"] == 1
    remark_after = plan["remark"]
    warn_msg = db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == pid).one().message

    # 重复提交同一调整（网络重试/双击）：幂等归并
    for _ in range(2):
        again = _adjust(db, pid, items, reason="核增上源镇")
        assert again["adjust_round"] == 1
        assert again["remark"] == remark_after
        assert again["total_planned_m3"] == 550000
    assert db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == pid).one().message == warn_msg
    # 不同内容才是新一轮
    plan = _adjust(db, pid, [{"township_id": 1, "planned_m3": 350000},
                             {"township_id": 2, "planned_m3": 250000}])
    assert plan["adjust_round"] == 2
    db.close()


def test_complete_after_adjust_deducts_storage_exactly_once(factory):
    """调整后完成：按调整后方案结算，库容只扣一次、调度记录只写一条。"""
    db = factory()
    plan = _submit(db)
    pid = plan["id"]
    _to_executing(db, pid)
    # 滚动调整：总量 50万→40万（乡镇1 调 25万、乡镇2 调 15万）
    plan = _adjust(db, pid, [{"township_id": 1, "planned_m3": 250000},
                             {"township_id": 2, "planned_m3": 150000}])
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, pid, {"township_id": 2, "priority": 2, "role": "township"})
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 250000,
                                 "role": "township"})
    ws.report_delivery(db, pid, {"township_id": 2, "delivered_m3": 150000,
                                 "role": "township"})

    storage_before = db.get(Reservoir, 1).current_storage
    plan = ws.complete_plan(db, pid, "刘水务", "township")
    assert plan["status"] == "completed"
    assert plan["completion"]["released_m3"] == 400000
    # 库容按调整后实供扣减一次：40 万m³ = 40 万m³→40 storage 单位
    assert db.get(Reservoir, 1).current_storage == pytest.approx(
        storage_before - 40.0, abs=0.01)
    logs = db.query(ReservoirDispatchLog).filter_by(ref_id=pid).all()
    assert len(logs) == 1 and logs[0].released_m3 == 400000

    # 重复完成 409：库容不重复扣减
    with pytest.raises(HTTPException) as ei:
        ws.complete_plan(db, pid, "刘水务", "township")
    assert ei.value.status_code == 409
    assert db.get(Reservoir, 1).current_storage == pytest.approx(
        storage_before - 40.0, abs=0.01)
    assert db.query(ReservoirDispatchLog).filter_by(ref_id=pid).count() == 1
    db.close()


# ---------------- 角色与状态机 ----------------
def test_adjust_role_and_status_guards(factory):
    db = factory()
    plan = _submit(db)
    pid = plan["id"]
    items = [{"township_id": 1, "planned_m3": 100000},
             {"township_id": 2, "planned_m3": 100000}]

    # 待审核（submitted）不能滚动调整
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, items)
    assert ei.value.status_code == 409
    # 越权：乡镇/物资管理员不能调整
    ws.review_plan(db, pid, "张调度", "dispatcher")
    for bad in ("township", "supply_manager"):
        with pytest.raises(HTTPException) as ei:
            _adjust(db, pid, items, role=bad)
        assert ei.value.status_code == 403
    # 调度员也可调整
    plan = _adjust(db, pid, items, role="dispatcher", operator="张调度")
    assert plan["adjust_round"] == 1

    # 闭环后不再滚动
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, pid, {"township_id": 2, "priority": 2, "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")
    ws.complete_plan(db, pid, "刘水务", "township")
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, items)
    assert ei.value.status_code == 409
    db.close()


def test_adjust_validation_errors(factory):
    db = factory()
    plan = _submit(db)
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    # 空方案 422
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, [])
    assert ei.value.status_code == 422
    db.rollback()
    # 乡镇重复 422
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, [{"township_id": 1, "planned_m3": 100},
                          {"township_id": 1, "planned_m3": 200}])
    assert ei.value.status_code == 422
    db.rollback()
    # 乡镇不存在 404 / 非正水量 422 / 非正周期 422
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, [{"township_id": 99, "planned_m3": 100}])
    assert ei.value.status_code == 404
    db.rollback()
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, [{"township_id": 1, "planned_m3": 0}])
    assert ei.value.status_code == 422
    db.rollback()
    with pytest.raises(HTTPException) as ei:
        _adjust(db, pid, [{"township_id": 1, "planned_m3": 100}], period=-3)
    assert ei.value.status_code == 422
    db.close()


def test_adjust_multi_round_snapshot_history(factory):
    """多轮滚动：轮次历史逐轮累积，快照始终对应现行方案。"""
    db = factory()
    plan = _submit(db)
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    _adjust(db, pid, [{"township_id": 1, "planned_m3": 280000},
                      {"township_id": 2, "planned_m3": 200000}])
    plan = _adjust(db, pid, [{"township_id": 1, "planned_m3": 280000},
                             {"township_id": 2, "planned_m3": 220000}])
    assert plan["adjust_round"] == 2
    rounds = plan["plan"]["rounds"]
    assert [r["round"] for r in rounds] == [1, 2]
    assert rounds[0]["total_planned_m3"] == 480000
    assert rounds[1]["total_planned_m3"] == 500000
    # 快照与现行方案一致
    assert plan["plan"]["total_planned_m3"] == 500000
    assert plan["remark"].count("[滚动调整·") == 2
    db.close()
