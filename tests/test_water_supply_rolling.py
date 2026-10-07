"""枯水期供水保障：审核后滚动调整配水测试。

覆盖：
- 闭环后滚动调整：轮次 +1、保留累计已供水与应急出库记录、配水明细身份迁移；
- 重走 调度员审核（重新登记/激活供水预警）→ 乡镇重新确认优先级 → 执行 → 完成；
- 每轮完成只扣「累计实供 − 已结算」增量，重复提交零增量不重复扣库容、
  不重复写水库调度台账（幂等）；
- 死水位校验在调整/复核/完成三处按本轮尚需放水量重新校验；
- 滚动调整校验：仅闭环单可调整、仅水库管理员、原乡镇不得移除、
  新计划不得低于累计已供水；
- 多轮欠供冻结口径、历轮结算历史归档、调度台账轮次留痕；
- 滚动调整单与其它进行中供水单的跨单预留只计未结算部分。
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (Reservoir, ReservoirDispatchLog, RiverNode, RiverReach,
                        SubBasin, Supply, Township, WarningRecord,
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
        # 水位 11.5m，死水位 10m（100 万m³），当前 250 万m³，可用 150 万m³
        Reservoir(id=1, name="测试水库", node_id=2, normal_level=12.0,
                  flood_level=13.0, crest_level=16.0,
                  storage_curve=[[10, 100], [12, 300], [14, 600], [16, 1000], [18, 1500]],
                  discharge_curve=[[14, 0], [16, 200], [18, 600]],
                  gate_max=120.0, current_level=11.5, current_storage=250.0,
                  dead_level=10.0, drought_warn_level=12.5),
        Township(id=1, name="上源镇", contact="刘水务", demand_m3=500),
        Township(id=2, name="下游乡", contact="赵水务", demand_m3=300),
        Township(id=3, name="龙潭镇", contact="钱水务", demand_m3=200),
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


def _run_first_round(db, items=None):
    """首轮：两乡各 200000 m³，足额供水并完成。"""
    if items is None:
        items = [{"township_id": 1, "planned_m3": 200000},
                 {"township_id": 2, "planned_m3": 200000}]
    plan = ws.submit_plan(db, {"reservoir_id": 1, "period_days": 10, "role": "manager",
                               "operator": "孙库管", "allocations": items})
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    for i, it in enumerate(items, start=1):
        ws.set_priority(db, pid, {"township_id": it["township_id"], "priority": i,
                                  "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")
    for it in items:
        ws.report_delivery(db, pid, {"township_id": it["township_id"],
                                     "delivered_m3": it["planned_m3"], "role": "township"})
    ws.complete_plan(db, pid, "刘水务", "township")
    return pid


def _review_priority_start(db, pid, tids):
    ws.review_plan(db, pid, "张调度", "dispatcher")
    for i, tid in enumerate(tids, start=1):
        ws.set_priority(db, pid, {"township_id": tid, "priority": i, "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")


# ---------------- 滚动调整主链路 ----------------
def test_rolling_adjust_keeps_delivered_and_releases_only_increment(factory):
    db = factory()
    pid = _run_first_round(db)
    assert db.get(Reservoir, 1).current_storage == pytest.approx(210.0, abs=0.01)

    # 第二轮滚动调整：上源镇核增到 300000（已供 200000 保留），下游乡维持 200000，
    # 新增龙潭镇 100000
    plan = ws.adjust_plan(db, {
        "plan_id": pid, "role": "manager", "operator": "孙库管", "period_days": 15,
        "allocations": [{"township_id": 1, "planned_m3": 300000},
                        {"township_id": 2, "planned_m3": 200000},
                        {"township_id": 3, "planned_m3": 100000}],
        "remark": "旱情持续"})
    assert plan["status"] == "submitted" and plan["adjust_round"] == 2
    # 状态文案体现滚动轮次
    assert "第2轮" in plan["status_text"]

    # 已供水保留
    a1 = next(a for a in plan["allocations"] if a["township_id"] == 1)
    a3 = next(a for a in plan["allocations"] if a["township_id"] == 3)
    assert a1["delivered_m3"] == 200000 and a1["planned_m3"] == 300000
    assert not a1["priority_confirmed"] and not a3["priority_confirmed"]  # 需重新确认
    # 累计实供 400000 已保留；本轮待扣增量 = 600000 - 400000
    assert plan["total_delivered_m3"] == 400000
    assert plan["round_released_m3"] == 0  # 调整后尚未新增供水

    # 重新审核 → 优先级 → 执行：预警重新进入处置中
    _review_priority_start(db, pid, [1, 2, 3])
    warn = db.query(WarningRecord).filter_by(water_supply_plan_id=pid).one()
    assert warn.status == "handling"
    # 新轮上报采用累计口径：上源镇补 100000、龙潭镇足额 100000，下游乡维持 200000
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 300000,
                                 "role": "township"})
    ws.report_delivery(db, pid, {"township_id": 3, "delivered_m3": 100000,
                                 "role": "township"})

    storage_before = db.get(Reservoir, 1).current_storage
    plan = ws.complete_plan(db, pid, "刘水务", "township")
    # 本轮只扣增量 200000 m³ = 20 万m³（而非累计 600000）
    db.refresh(db.get(Reservoir, 1))
    res = db.get(Reservoir, 1)
    assert res.current_storage == pytest.approx(storage_before - 20.0, abs=0.01)
    assert res.current_storage == pytest.approx(190.0, abs=0.01)

    comp = plan["completion"]
    assert comp["round"] == 2
    assert comp["released_m3"] == 200000          # 本轮扣减
    assert comp["cumulative_delivered_m3"] == 600000
    assert comp["released_total_m3"] == 600000
    assert comp["idempotent"] is False

    # 调度台账两条，且仅第 2 条扣 200000 m³
    logs = (db.query(ReservoirDispatchLog).filter_by(ref_id=pid)
            .order_by(ReservoirDispatchLog.id).all())
    assert [l.released_m3 for l in logs] == [400000, 200000]
    assert logs[1].payload["round"] == 2
    assert logs[1].payload["released_before_m3"] == 400000
    db.close()


def test_repeat_complete_zero_increment_does_not_deduct_again(factory):
    """调整后无新增供水直接完成：零增量，不重复扣库容、不重复写调度台账。"""
    db = factory()
    pid = _run_first_round(db)
    ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                        "allocations": [{"township_id": 1, "planned_m3": 200000},
                                        {"township_id": 2, "planned_m3": 200000}]})
    _review_priority_start(db, pid, [1, 2])
    storage_before = db.get(Reservoir, 1).current_storage

    # 两乡累计实供仍为 200000（默认带出），本轮增量 = 0
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 200000,
                                 "role": "township"})
    ws.report_delivery(db, pid, {"township_id": 2, "delivered_m3": 200000,
                                 "role": "township"})
    plan = ws.complete_plan(db, pid, "刘水务", "township")
    res = db.get(Reservoir, 1)
    assert res.current_storage == pytest.approx(storage_before, abs=1e-9)
    comp = plan["completion"]
    assert comp["idempotent"] is True and comp["released_m3"] == 0
    assert comp["dispatch_log_id"] is None
    # 台账仍只有首轮一条（未重复写）
    assert db.query(ReservoirDispatchLog).filter_by(ref_id=pid).count() == 1
    # 零增量留痕
    assert "重复提交" in plan["remark"]
    db.close()


def test_third_round_accumulates_increment_correctly(factory):
    """第三轮继续滚动：台账累计口径稳定，每轮只扣增量。"""
    db = factory()
    pid = _run_first_round(db)
    storage0 = db.get(Reservoir, 1).current_storage  # 210 万m³

    def round2():
        ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                            "allocations": [{"township_id": 1, "planned_m3": 300000},
                                            {"township_id": 2, "planned_m3": 300000}]})
        _review_priority_start(db, pid, [1, 2])
        ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 300000,
                                     "role": "township"})
        ws.report_delivery(db, pid, {"township_id": 2, "delivered_m3": 300000,
                                     "role": "township"})
        ws.complete_plan(db, pid, "刘水务", "township")

    round2()
    # 第 2 轮扣 200000 m³（600000 - 400000）
    assert db.get(Reservoir, 1).current_storage == pytest.approx(storage0 - 20.0, abs=0.01)

    ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                        "allocations": [{"township_id": 1, "planned_m3": 400000},
                                        {"township_id": 2, "planned_m3": 300000}]})
    assert ws.get_plan(db, pid)["adjust_round"] == 3
    _review_priority_start(db, pid, [1, 2])
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 400000,
                                 "role": "township"})
    # 下游乡不上报：完成时按计划 300000 足额结算（累计不变）
    plan = ws.complete_plan(db, pid, "刘水务", "township")
    # 第 3 轮增量 = 700000 - 600000 = 100000 m³
    assert plan["completion"]["released_m3"] == 100000
    assert db.get(Reservoir, 1).current_storage == pytest.approx(storage0 - 30.0, abs=0.01)
    logs = db.query(ReservoirDispatchLog).filter_by(ref_id=pid).order_by(
        ReservoirDispatchLog.id).all()
    assert [l.released_m3 for l in logs] == [400000, 200000, 100000]
    # 历轮结算历史：快照 rounds 存前两轮
    assert [r["round"] for r in plan["completion"]["rounds"]] == [1, 2]
    db.close()


# ---------------- 应急出库记录保留 ----------------
def test_emergency_records_preserved_across_rounds(factory):
    db = factory()
    # 首轮欠供：计划 200000、实供 120000，追加 10 箱应急水
    pid = _run_first_round(db, items=[{"township_id": 1, "planned_m3": 200000},
                                      {"township_id": 2, "planned_m3": 200000}])
    # _run_first_round 足额；这里改成欠供场景手工跑
    # （重新构造一张已闭环欠供单）
    plan = ws.submit_plan(db, {"reservoir_id": 1, "period_days": 10, "role": "manager",
                               "allocations": [{"township_id": 1, "planned_m3": 200000}]})
    pid2 = plan["id"]
    ws.review_plan(db, pid2, "张调度", "dispatcher")
    ws.set_priority(db, pid2, {"township_id": 1, "priority": 1, "role": "township"})
    ws.confirm_priorities(db, pid2, "刘水务", "township")
    ws.report_delivery(db, pid2, {"township_id": 1, "delivered_m3": 120000,
                                  "role": "township"})
    ws.add_emergency(db, pid2, {"supply_id": 1, "quantity": 10, "township_id": 1,
                                "role": "supply_manager"})
    ws.complete_plan(db, pid2, "刘水务", "township")
    stock_after_r1 = db.get(Supply, 1).stock

    # 滚动调整：欠供乡核增计划、保留已供 120000
    plan = ws.adjust_plan(db, {"plan_id": pid2, "role": "manager", "period_days": 10,
                               "allocations": [{"township_id": 1, "planned_m3": 200000}]})
    # 首轮应急出库记录保留（库存未回滚）
    ems = db.query(WaterSupplyEmergency).filter_by(plan_id=pid2).all()
    assert len(ems) == 1 and ems[0].round == 1 and ems[0].quantity == 10
    assert db.get(Supply, 1).stock == stock_after_r1
    _review_priority_start(db, pid2, [1])
    ws.report_delivery(db, pid2, {"township_id": 1, "delivered_m3": 200000,
                                  "role": "township"})
    plan = ws.complete_plan(db, pid2, "刘水务", "township")
    # 第 2 轮增量放水 80000 m³；应急记录仍只有首轮一条，完成快照含累计 1 次
    assert plan["completion"]["released_m3"] == 80000
    assert plan["completion"]["emergency_cumulative"] == 1
    assert plan["completion"]["emergency_count"] == 0
    em = plan["emergencies"][0]
    assert em["round"] == 1
    db.close()


# ---------------- 死水位重新校验 ----------------
def test_adjust_revalidates_dead_level_by_future_need(factory):
    db = factory()
    # 首轮放到接近死水位：1450000 m³，库容 105 万m³（仅剩 5 万m³）
    pid = _run_first_round(db, items=[{"township_id": 1, "planned_m3": 1450000}])
    assert db.get(Reservoir, 1).current_storage == pytest.approx(105.0, abs=0.01)
    # 第二轮想再放 100000 m³：死水位以上仅剩 50000 m³，调整即 409
    with pytest.raises(HTTPException) as ei:
        ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                            "allocations": [{"township_id": 1, "planned_m3": 1550000}]})
    assert ei.value.status_code == 409
    # 计划不变（1450000 = 已结算）时可调整：本轮尚需放水 0
    plan = ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                               "allocations": [{"township_id": 1, "planned_m3": 1450000}]})
    assert plan["adjust_round"] == 2
    db.close()


def test_complete_below_dead_level_rejected_in_round_and_atomic(factory):
    """滚动轮次完成时越过死水位：调整阶段即按本轮尚需水量 409 拦截。"""
    db = factory()
    pid = _run_first_round(db, items=[{"township_id": 1, "planned_m3": 1400000}])
    assert db.get(Reservoir, 1).current_storage == pytest.approx(110.0, abs=0.01)
    # 第二轮想累计放 1510000 m³：增量 110000 > 死水位以上 100000 m³ → 调整 409
    with pytest.raises(HTTPException) as ei:
        ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                            "allocations": [{"township_id": 1, "planned_m3": 1510000}]})
    assert ei.value.status_code == 409
    # 单据仍是闭环态，库容未动
    assert db.get(WaterSupplyPlan, pid).status == "completed"
    assert db.get(Reservoir, 1).current_storage == pytest.approx(110.0, abs=0.01)
    db.close()


def test_final_dead_level_guard_on_complete_via_manual_alloc(factory):
    """完成阶段死水位最终防线（绕过调整校验的存量/手工数据）：409 且不扣库容。"""
    db = factory()
    pid = _run_first_round(db, items=[{"township_id": 1, "planned_m3": 1400000}])
    ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                        "allocations": [{"township_id": 1, "planned_m3": 1500000}]})
    _review_priority_start(db, pid, [1])
    alloc = ws.serialize_plan(db, db.get(WaterSupplyPlan, pid))
    aid = alloc["allocations"][0]["id"]
    from app.models import WaterSupplyAllocation
    row = db.get(WaterSupplyAllocation, aid)
    row.delivered_m3 = 1510000  # 增量 110000 m³ > 死水位以上 100000 m³
    db.commit()
    storage_before = db.get(Reservoir, 1).current_storage
    n_logs = db.query(ReservoirDispatchLog).filter_by(ref_id=pid).count()
    with pytest.raises(HTTPException) as ei:
        ws.complete_plan(db, pid, "刘水务", "township")
    assert ei.value.status_code == 409
    assert db.get(Reservoir, 1).current_storage == pytest.approx(storage_before, abs=1e-9)
    assert db.get(WaterSupplyPlan, pid).status == "executing"
    assert db.query(ReservoirDispatchLog).filter_by(ref_id=pid).count() == n_logs
    db.close()


# ---------------- 调整入口校验 ----------------
def test_adjust_guards(factory):
    db = factory()
    pid = _run_first_round(db)

    # 越权：仅水库管理员可调整
    with pytest.raises(HTTPException) as ei:
        ws.adjust_plan(db, {"plan_id": pid, "role": "dispatcher", "period_days": 10,
                            "allocations": [{"township_id": 1, "planned_m3": 200000},
                                            {"township_id": 2, "planned_m3": 200000}]})
    assert ei.value.status_code == 403

    # 非闭环单不能调整（新申报一张 submitted 单）
    other = ws.submit_plan(db, {"reservoir_id": 1, "period_days": 10, "role": "manager",
                                "allocations": [{"township_id": 3, "planned_m3": 1000}]})
    # 同库已有滚动中的单会先互斥；直接对 pid 在其滚动中再调整 → 409
    ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                        "allocations": [{"township_id": 1, "planned_m3": 200000},
                                        {"township_id": 2, "planned_m3": 200000}]})
    with pytest.raises(HTTPException) as ei:
        ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                            "allocations": [{"township_id": 1, "planned_m3": 200000},
                                            {"township_id": 2, "planned_m3": 200000}]})
    assert ei.value.status_code == 409
    db.rollback()

    # 原乡镇不得移除
    plan = db.get(WaterSupplyPlan, pid)
    plan.status = "completed"  # 回到闭环态尝试
    db.commit()
    with pytest.raises(HTTPException) as ei:
        ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                            "allocations": [{"township_id": 1, "planned_m3": 200000}]})
    assert ei.value.status_code == 422

    # 新计划不得低于累计已供水
    with pytest.raises(HTTPException) as ei:
        ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                            "allocations": [{"township_id": 1, "planned_m3": 199999},
                                            {"township_id": 2, "planned_m3": 200000}]})
    assert ei.value.status_code == 422

    # 不存在的单 404
    with pytest.raises(HTTPException) as ei:
        ws.adjust_plan(db, {"plan_id": 9999, "role": "manager", "period_days": 10,
                            "allocations": [{"township_id": 1, "planned_m3": 1}]})
    assert ei.value.status_code == 404
    db.close()


def test_review_in_round_reactivates_cleared_warning(factory):
    """首轮销警后滚动调整，重新审核时预警按身份激活（不新增重复预警）。"""
    db = factory()
    res = db.get(Reservoir, 1)
    # 抬高水位到预警线以上：首轮完成销警
    res.current_level = 13.0
    res.current_storage = res.storage_at(13.0)  # 450 万m³
    db.commit()
    pid = _run_first_round(db, items=[{"township_id": 1, "planned_m3": 100}])
    warn = db.query(WarningRecord).filter_by(water_supply_plan_id=pid).first()
    # 审核时水位高于预警线无预警记录
    if warn is not None:
        assert warn.status == "cleared"

    # 水位回落至预警线以下再滚动
    res.current_level = 11.5
    res.current_storage = 250.0
    db.commit()
    ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                        "allocations": [{"township_id": 1, "planned_m3": 100000}]})
    ws.review_plan(db, pid, "张调度", "dispatcher")
    warns = db.query(WarningRecord).filter_by(water_supply_plan_id=pid).all()
    assert len(warns) == 1
    assert warns[0].status == "active"
    db.close()


def test_cross_plan_reservation_excludes_settled_rounds(factory):
    """滚动调整单历轮已结算水量不重复占用：第二张单可按真实剩余可用量申报。"""
    db = factory()
    # 首轮用掉 400000 m³，库容 210 万m³，死水位以上剩 110 万m³
    pid = _run_first_round(db)
    # 原单滚动调整，计划维持 400000（已结算），不占未结算预留
    ws.adjust_plan(db, {"plan_id": pid, "role": "manager", "period_days": 10,
                        "allocations": [{"township_id": 1, "planned_m3": 200000},
                                        {"township_id": 2, "planned_m3": 200000}]})
    ws.review_plan(db, pid, "张调度", "dispatcher")  # reviewed：进行中
    # 同库不能再申报第二张（互斥）；直接校验可供水口径：滚动单未结算预留为 0
    res = db.get(Reservoir, 1)
    assert ws._available_water_m3(db, res, exclude_plan_id=pid) == pytest.approx(
        1100000.0, abs=1.0)
    db.close()
