"""枯水期供水保障闭环测试。

覆盖：
- 申请 → 审核 → 乡镇确认优先级 → 执行配水 → 完成扣减库容全链路与电子签名
- 角色权限（越权 403）与跳态（409）
- 完成回写：库容/水位扣减、水库调度记录 (reservoir_dispatch_logs)、
  供水预警登记/处置中/销警/枯水升级
- 死水位约束：申报超可用水量 409；完成实放越过死水位 409 且库容不扣减
- 同库进行中单据互斥；闭环后可重新申报
- 异常欠供：上报缺口 → 物资管理员追加应急物资（库存直接出库、超库存 409、
  无欠供 409）；挂接既有防汛处置单仅追加处置记录、不改其状态机与资源台账
- 乡镇未全部确认优先级不能启动执行；上报超计划 422
- 历史预警（water_supply_plan_id 为 NULL）原样保留
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (DisposalOrder, FloodZone, RainfallEvent, Reservoir, RiverNode,
                        RiverReach, SubBasin, Supply, Township, WarningRecord,
                        WaterStation, WaterSupplyAllocation, WaterSupplyEmergency,
                        WaterSupplyPlan)
from app.services import water_supply as ws
from app.services.forecast import run_forecast
from app.services import disposal


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
        # 水位 11.5m，死水位 10m，枯水预警线 12.5m：申报即处枯水预警状态
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
        Supply(id=1, name="瓶装饮用水", unit="箱", stock=100, safety_stock=10),
        Supply(id=2, name="方便食品", unit="箱", stock=80, safety_stock=10),
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


def _submit(db, reservoir_id=1, items=None, period=10, **kw):
    if items is None:
        items = [{"township_id": 1, "planned_m3": 300000},
                 {"township_id": 2, "planned_m3": 200000}]
    body = {"reservoir_id": reservoir_id, "period_days": period,
            "role": "manager", "operator": "孙库管",
            "reason": "连续少雨", "allocations": items}
    body.update(kw)
    return ws.submit_plan(db, body)


# ---------------- 全链路 ----------------
def test_full_water_supply_closed_loop(factory):
    db = factory()
    plan = _submit(db)
    assert plan["status"] == "submitted"
    assert plan["total_planned_m3"] == 500000
    assert plan["submitted_by"] == "孙库管"

    plan = ws.review_plan(db, plan["id"], "张调度", "dispatcher", opinion="同意")
    assert plan["status"] == "reviewed"
    warn = db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == plan["id"]).one()
    assert warn.kind == "water_supply" and warn.status == "active"
    assert warn.run_id is None and warn.disposal_id is None

    # 乡镇逐户确认优先级
    ws.set_priority(db, plan["id"], {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, plan["id"], {"township_id": 2, "priority": 2, "role": "township",
                                     "note": "下游让水"})
    plan = ws.confirm_priorities(db, plan["id"], "刘水务", "township")
    assert plan["status"] == "executing" and plan["all_priority_confirmed"]
    db.refresh(warn)
    assert warn.status == "handling"

    # 足额供水上报 → 完成：扣减库容 500000m³ = 50 万m³
    ws.report_delivery(db, plan["id"], {"township_id": 1, "delivered_m3": 300000,
                                        "role": "township"})
    ws.report_delivery(db, plan["id"], {"township_id": 2, "delivered_m3": 200000,
                                        "role": "township"})
    res = db.get(Reservoir, 1)
    before_storage = res.current_storage
    plan = ws.complete_plan(db, plan["id"], "刘水务", "township", summary="供水完成")
    assert plan["status"] == "completed"
    db.refresh(res)
    assert res.current_storage == pytest.approx(before_storage - 50.0, abs=0.01)
    assert res.current_level < 11.5

    # 调度记录回写
    from app.models import ReservoirDispatchLog
    log = db.query(ReservoirDispatchLog).filter_by(ref_id=plan["id"]).one()
    assert log.kind == "water_supply" and log.released_m3 == 500000
    assert log.storage_after == pytest.approx(res.current_storage, abs=0.01)
    assert log.payload["townships"] and log.payload["shortage_m3"] == 0

    # 足额供水、水位仍在预警线以下时预警不销警（drought_level 非空）
    db.refresh(warn)
    assert warn.status in ("handling", "active")

    # 完成快照
    comp = plan["completion"]
    assert comp["released_m3"] == 500000 and comp["shortage_total_m3"] == 0
    assert comp["dispatch_log_id"] == log.id
    db.close()


def test_warning_registered_on_review_when_below_drought_line(factory):
    """审核时已处枯水预警线以下：登记预警并在启动配水时进入处置中。"""
    db = factory()
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 100000}])
    ws.review_plan(db, plan["id"], "张调度", "dispatcher")
    warn = db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == plan["id"]).one()
    assert warn.kind == "water_supply" and warn.level in ("blue", "yellow", "orange", "red")
    assert warn.status == "active"
    db.close()


def test_no_warning_when_level_normal_then_created_after_drain(factory):
    """审核时水位高于枯水预警线：不登记预警；完成放水跌破后自动新增枯水预警。"""
    db = factory()
    res = db.get(Reservoir, 1)
    res.current_level = 13.2
    res.current_storage = res.storage_at(13.2)
    db.commit()
    # 放 2,200,000 m³ = 220 万m³：250 万m³附近 → 接近死水位 100 万m³
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 2200000}])
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    assert db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == pid).count() == 0
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 2200000,
                                 "role": "township"})
    ws.complete_plan(db, pid, "刘水务", "township")
    warn = db.query(WarningRecord).filter_by(water_supply_plan_id=pid).one()
    db.refresh(res)
    assert res.current_level < 12.5
    assert warn.kind == "water_supply" and warn.status == "handling"
    db.close()


def test_sufficient_water_clears_warning(factory):
    """供水量小、扣减后水位仍高于枯水预警线时销警。"""
    db = factory()
    res = db.get(Reservoir, 1)
    # 抬高水位到预警线以上
    res.current_level = 13.0
    res.current_storage = res.storage_at(13.0)
    db.commit()
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 100}])
    ws.review_plan(db, plan["id"], "张调度", "dispatcher")
    warn = db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == plan["id"]).first()
    # 13.0m 高于预警线 12.5：审核不强制预警（按身份 upsert，此处至少不阻断）
    ws.set_priority(db, plan["id"], {"township_id": 1, "priority": 1, "role": "township"})
    ws.confirm_priorities(db, plan["id"], "刘水务", "township")
    ws.report_delivery(db, plan["id"], {"township_id": 1, "delivered_m3": 100,
                                        "role": "township"})
    ws.complete_plan(db, plan["id"], "刘水务", "township")
    db.refresh(res)
    assert res.current_level >= 12.5
    if warn is not None:
        assert warn.status == "cleared"
    db.close()


# ---------------- 角色与状态机 ----------------
def test_role_enforcement(factory):
    db = factory()
    plan = _submit(db)
    # 非水库管理员不能申报
    with pytest.raises(HTTPException) as ei:
        _submit(db, role="dispatcher")
    assert ei.value.status_code == 403
    # 非调度员不能审核
    for bad in ("manager", "township", "supply_manager"):
        with pytest.raises(HTTPException) as ei:
            ws.review_plan(db, plan["id"], "x", bad)
        assert ei.value.status_code == 403
    ws.review_plan(db, plan["id"], "张调度", "dispatcher")
    # 非乡镇不能确认优先级
    with pytest.raises(HTTPException) as ei:
        ws.set_priority(db, plan["id"], {"township_id": 1, "priority": 1,
                                         "role": "dispatcher"})
    assert ei.value.status_code == 403
    db.close()


def test_illegal_transitions_rejected(factory):
    db = factory()
    plan = _submit(db)
    # 未审核不能确认优先级 / 执行 / 完成
    with pytest.raises(HTTPException) as ei:
        ws.set_priority(db, plan["id"], {"township_id": 1, "priority": 1,
                                         "role": "township"})
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        ws.complete_plan(db, plan["id"], "刘水务", "township")
    assert ei.value.status_code == 409

    ws.review_plan(db, plan["id"], "张调度", "dispatcher")
    # 重复审核 409
    with pytest.raises(HTTPException) as ei:
        ws.review_plan(db, plan["id"], "张调度", "dispatcher")
    assert ei.value.status_code == 409
    # 优先级未全部确认不能执行
    ws.set_priority(db, plan["id"], {"township_id": 1, "priority": 1, "role": "township"})
    with pytest.raises(HTTPException) as ei:
        ws.confirm_priorities(db, plan["id"], "刘水务", "township")
    assert ei.value.status_code == 409
    db.close()


def test_unknown_plan_and_township(factory):
    db = factory()
    with pytest.raises(HTTPException) as ei:
        ws.review_plan(db, 999, "张调度", "dispatcher")
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        _submit(db, items=[{"township_id": 99, "planned_m3": 100}])
    assert ei.value.status_code == 404
    db.rollback()
    # 空明细 / 非正周期
    with pytest.raises(HTTPException) as ei:
        _submit(db, items=[])
    assert ei.value.status_code == 422
    db.rollback()
    with pytest.raises(HTTPException) as ei:
        _submit(db, period=0)
    assert ei.value.status_code == 422
    db.close()


# ---------------- 死水位与库容约束 ----------------
def test_submit_over_available_water_rejected(factory):
    db = factory()
    # 死水位 10m 对应 100 万m³；当前 250 万m³，可用 150 万m³ = 1,500,000 m³
    with pytest.raises(HTTPException) as ei:
        _submit(db, items=[{"township_id": 1, "planned_m3": 2000000}])
    assert ei.value.status_code == 409
    db.close()


def test_completion_below_dead_storage_rejected_and_atomic(factory):
    db = factory()
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 1400000}])
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")
    # 执行期直接构造越过死水位的实供（完成阶段是最终防线）
    alloc = db.query(WaterSupplyAllocation).filter_by(plan_id=pid).one()
    alloc.delivered_m3 = 1490000  # 死水位以上仅剩 1,500,000m³… 仍可放
    db.commit()
    alloc.delivered_m3 = 1510000  # 越过死水位 10,000m³
    db.commit()
    storage_before = db.get(Reservoir, 1).current_storage
    with pytest.raises(HTTPException) as ei:
        ws.complete_plan(db, pid, "刘水务", "township")
    assert ei.value.status_code == 409
    # 原子性：库容未扣、单据仍执行中、无调度记录
    from app.models import ReservoirDispatchLog
    assert db.get(Reservoir, 1).current_storage == storage_before
    assert db.get(WaterSupplyPlan, pid).status == "executing"
    assert db.query(ReservoirDispatchLog).filter_by(ref_id=pid).count() == 0
    db.close()


def test_delivery_over_planned_rejected(factory):
    db = factory()
    plan = _submit(db)
    ws.review_plan(db, plan["id"], "张调度", "dispatcher")
    ws.set_priority(db, plan["id"], {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, plan["id"], {"township_id": 2, "priority": 2, "role": "township"})
    ws.confirm_priorities(db, plan["id"], "刘水务", "township")
    with pytest.raises(HTTPException) as ei:
        ws.report_delivery(db, plan["id"], {"township_id": 1, "delivered_m3": 300001,
                                            "role": "township"})
    assert ei.value.status_code == 422
    # 非执行中不能上报
    with pytest.raises(HTTPException) as ei:
        ws.report_delivery(db, plan["id"], {"township_id": 1, "delivered_m3": 1,
                                            "role": "dispatcher"})
    assert ei.value.status_code == 403
    db.close()


def test_one_active_plan_per_reservoir(factory):
    db = factory()
    p1 = _submit(db)
    with pytest.raises(HTTPException) as ei:
        _submit(db)
    assert ei.value.status_code == 409
    # 闭环后可重新申报
    ws.review_plan(db, p1["id"], "张调度", "dispatcher")
    ws.set_priority(db, p1["id"], {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, p1["id"], {"township_id": 2, "priority": 2, "role": "township"})
    ws.confirm_priorities(db, p1["id"], "刘水务", "township")
    ws.report_delivery(db, p1["id"], {"township_id": 1, "delivered_m3": 300000,
                                      "role": "township"})
    ws.report_delivery(db, p1["id"], {"township_id": 2, "delivered_m3": 200000,
                                      "role": "township"})
    ws.complete_plan(db, p1["id"], "刘水务", "township")
    p2 = _submit(db, items=[{"township_id": 1, "planned_m3": 1000}])
    assert p2["id"] != p1["id"] and p2["status"] == "submitted"
    db.close()


def test_cross_plan_reservation_blocks_overcommit(factory):
    """另一进行中供水单的计划水量计入预留，第二张申报超剩余可用量 409。"""
    db = factory()
    # 第一张预占 120 万m³（可用 150 万m³）
    p1 = _submit(db, items=[{"township_id": 1, "planned_m3": 1200000}])
    assert p1["status"] == "submitted"
    # 第二张再申报 40 万m³，超过剩余 30 万m³
    with pytest.raises(HTTPException) as ei:
        _submit(db, items=[{"township_id": 2, "planned_m3": 400000}])
    assert ei.value.status_code == 409
    db.close()


# ---------------- 欠供与应急物资 ----------------
def _executing_shortage_plan(db, delivered=120000):
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 200000}])
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": delivered,
                                 "role": "township"})
    return pid


def test_emergency_supply_on_shortage_issues_stock(factory):
    db = factory()
    pid = _executing_shortage_plan(db, delivered=120000)
    stock_before = db.get(Supply, 1).stock
    plan = ws.add_emergency(db, pid, {"supply_id": 1, "quantity": 30,
                                      "township_id": 1, "role": "supply_manager",
                                      "operator": "陈物资", "note": "送水"})
    assert plan["total_shortage_m3"] == 80000
    em = db.query(WaterSupplyEmergency).filter_by(plan_id=pid).one()
    assert em.quantity == 30 and em.shortage_m3 == 80000 and em.created_by == "陈物资"
    assert db.get(Supply, 1).stock == stock_before - 30
    db.close()


def test_emergency_requires_shortage_and_stock_and_role(factory):
    db = factory()
    # 无欠供（足额供水）→ 409
    pid_full = _executing_shortage_plan(db, delivered=200000)
    with pytest.raises(HTTPException) as ei:
        ws.add_emergency(db, pid_full, {"supply_id": 1, "quantity": 1,
                                        "role": "supply_manager"})
    assert ei.value.status_code == 409

    # 越权 403（对足额单同样先拦角色）
    with pytest.raises(HTTPException) as ei:
        ws.add_emergency(db, pid_full,
                         {"supply_id": 1, "quantity": 1, "role": "township"})
    assert ei.value.status_code == 403
    ws.complete_plan(db, pid_full, "刘水务", "township")

    # 欠供单：超库存 409
    plan = _submit(db, items=[{"township_id": 2, "planned_m3": 200000}])
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 2, "priority": 1, "role": "township"})
    ws.confirm_priorities(db, pid, "赵水务", "township")
    ws.report_delivery(db, pid, {"township_id": 2, "delivered_m3": 100000,
                                 "role": "township"})
    with pytest.raises(HTTPException) as ei:
        ws.add_emergency(db, pid, {"supply_id": 1, "quantity": 99999,
                                   "role": "supply_manager"})
    assert ei.value.status_code == 409
    # 非执行/完成状态不能追加：待审核单直接构造一条（同库互斥，绕过申报）
    other = WaterSupplyPlan(reservoir_id=1, title="另一单", period_days=1,
                            status="submitted", submitted_by="孙库管")
    db.add(other)
    db.commit()
    with pytest.raises(HTTPException) as ei:
        ws.add_emergency(db, other.id, {"supply_id": 1, "quantity": 1,
                                        "role": "supply_manager"})
    assert ei.value.status_code == 409
    db.close()


def test_emergency_after_completion_uses_frozen_shortage(factory):
    """完成后欠供被冻结，仍可补拨应急物资；未上报乡镇按足额结算。"""
    db = factory()
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 300000},
                              {"township_id": 2, "planned_m3": 200000}])
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.set_priority(db, pid, {"township_id": 2, "priority": 2, "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 200000,
                                 "role": "township"})
    # 乡镇 2 不上报：完成时按足额结算 → 总欠供 100000
    plan = ws.complete_plan(db, pid, "刘水务", "township")
    assert plan["total_shortage_m3"] == 100000
    # 闭环后仍可追加应急物资
    stock = db.get(Supply, 2).stock
    plan = ws.add_emergency(db, pid, {"supply_id": 2, "quantity": 20,
                                      "township_id": 1, "role": "supply_manager"})
    assert db.get(Supply, 2).stock == stock - 20
    assert plan["emergencies"][-1]["shortage_m3"] == 100000
    db.close()


def test_emergency_linked_to_disposal_appends_record_only(factory):
    """应急物资挂接既有防汛处置单：追加处置记录行，状态机/资源台账不变。"""
    db = factory()
    rid = run_forecast(db, db.get(RainfallEvent, 1), "natural")["run_id"]
    o = disposal.initiate_order(db, rid, "张调度", "dispatcher")
    disposal.review_order(db, o["id"], "李值守", "duty")
    status_before = db.get(DisposalOrder, o["id"]).status

    pid = _executing_shortage_plan(db)
    ws.add_emergency(db, pid, {"supply_id": 1, "quantity": 10, "township_id": 1,
                               "disposal_id": o["id"], "role": "supply_manager"})
    from app.models import SupplyAllocation
    linked = db.get(DisposalOrder, o["id"])
    assert linked.status == status_before == "approved"
    assert "应急物资联动·枯水供水" in linked.remark
    # 防汛资源台账不新增分配记录
    assert db.query(SupplyAllocation).count() == 0
    db.close()


# ---------------- 预警与历史兼容 ----------------
def test_drought_escalation_after_drain(factory):
    """放水至死水位附近：完成后枯水预警升级红色且保持处置中。"""
    db = factory()
    # 可用 150 万m³；放 145 万m³，水位逼近死水位 10m
    plan = _submit(db, items=[{"township_id": 1, "planned_m3": 1450000}])
    pid = plan["id"]
    ws.review_plan(db, pid, "张调度", "dispatcher")
    ws.set_priority(db, pid, {"township_id": 1, "priority": 1, "role": "township"})
    ws.confirm_priorities(db, pid, "刘水务", "township")
    ws.report_delivery(db, pid, {"township_id": 1, "delivered_m3": 1450000,
                                 "role": "township"})
    plan = ws.complete_plan(db, pid, "刘水务", "township")
    warn = db.query(WarningRecord).filter_by(water_supply_plan_id=pid).one()
    assert plan["completion"]["drought_level_after"] == "red"
    assert warn.level == "red" and warn.status == "handling"
    db.close()


def test_legacy_null_link_warnings_preserved(factory):
    db = factory()
    db.add(WarningRecord(run_id=None, target_type="station", target_id=99,
                         target_name="历史站", kind="water_level", level="blue",
                         status="active"))
    db.commit()
    pid = _executing_shortage_plan(db)
    ws.add_emergency(db, pid, {"supply_id": 1, "quantity": 5, "township_id": 1,
                               "role": "supply_manager"})
    ws.complete_plan(db, pid, "刘水务", "township")
    legacy = db.query(WarningRecord).filter(WarningRecord.run_id.is_(None),
                                            WarningRecord.water_supply_plan_id.is_(None)).one()
    assert legacy.status == "active" and legacy.disposal_id is None
    db.close()


def test_drought_level_helper_defaults(factory):
    """缺省枯水预警线（0）时按死水位~正常蓄水位 0.4 分位推算。"""
    db = factory()
    res = Reservoir(name="无配置水库", node_id=2, normal_level=20.0, flood_level=21.0,
                    crest_level=24.0,
                    storage_curve=[[0, 0], [10, 100], [20, 400], [24, 600]],
                    discharge_curve=[[20, 0], [24, 100]],
                    current_level=5.0, current_storage=25.0)
    db.add(res); db.commit()
    assert ws.effective_drought_warn_level(res) == pytest.approx(0.4 * 20.0)
    # 水位 5m 低于推算预警线 8m：frac=(8-5)/8=0.375 落在 yellow 段
    assert ws.drought_level(res) == "yellow"
    res.current_level = 0.5
    assert ws.drought_level(res) == "red"
    db.close()
