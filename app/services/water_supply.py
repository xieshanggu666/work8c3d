"""枯水期供水保障：水库管理员申报 → 调度员审核 → 乡镇确认优先级 → 乡镇执行配水 → 完成。

角色与状态机
    水库管理员 manager  submit_plan       → submitted（待审核，申报配水计划）
    调度员     dispatcher review_plan     → reviewed（审核通过，登记供水预警）
    乡镇       township  set_priority /
                          confirm_priorities → executing（逐乡镇确认优先级后执行配水）
    乡镇       township  report_delivery  → 执行期上报实际供水量（可欠供）
    乡镇       township  complete_plan     → completed（结算欠供、扣减库容、
                                            回写水库调度记录与预警）
    物资管理员 supply_manager add_emergency → 异常欠供时追加应急物资（直接出库，
                                            可挂接原有防汛处置单，兼容处置记录）
    水库管理员 manager  adjust_plan        → completed 后滚动调整配水（轮次 +1，
                                            保留已供水与应急出库记录，重走
                                            审核→优先级→执行→完成）

约束
    - 同一水库至多一张未闭环供水保障单（submitted/reviewed/executing），闭环后可重新申报，
      闭环后也可在原单上滚动调整配水（轮次递增，已供水/应急出库记录保留）；
    - 计划/审核按「当前库容 − 死水位库容」做可放水量校验，跨进行中单据统一预留，
      滚动调整单的历轮已结算水量已从库容扣减，预留只计未结算部分；
    - 滚动调整时计划水量不得低于该乡镇累计已供水量，原保障乡镇不得移除
      （已供水记录须保留），可核增并追加新乡镇；
    - 完成时按「累计实供 − 已结算水量」只扣本轮增量，增量为 0 不扣库容、
      不重复写调度台账（重复提交幂等）；累计放水量不得突破死水位库容
      （突破 409，须按欠供上报而非超放）；
    - 应急物资复用 supplies 库存池出库，与防汛处置的物资占用串行（同物资锁）。

历史兼容
    - Reservoir.dead_level / drought_warn_level 缺省（0）时按死水位=0、
      枯水预警线=死水位与正常蓄水位间 0.4 分位推算；
    - 供水预警写入 warning_records（kind=water_supply，water_supply_plan_id 挂接），
      run_id/disposal_id 为 NULL 的历史预警原样保留；
    - 应急追加可携带 disposal_id 挂接既有防汛处置单，仅追加处置记录行，
      不改动其状态机与资源台账；
    - adjust_round 缺列（老库）按首轮处理，迁移脚本补列后即支持滚动调整。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (DisposalOrder, Reservoir, ReservoirDispatchLog, Supply,
                        Township, WarningRecord, WaterSupplyAllocation,
                        WaterSupplyEmergency, WaterSupplyPlan)
from app.services import resources as resource_svc

STATUS_TEXT = {
    "submitted": "待审核",
    "reviewed": "已审核·待确认优先级",
    "executing": "执行配水中",
    "completed": "已完成",
}
# 滚动调整轮次中各状态的展示文案（首轮沿用 STATUS_TEXT）
ROUND_STATUS_TEXT = {
    "submitted": "滚动调整·待审核",
    "reviewed": "滚动调整·已审核·待确认优先级",
    "executing": "滚动调整·执行配水中",
}
ACTIVE_STATUSES = ("submitted", "reviewed", "executing")
ROLE_TEXT = {"manager": "水库管理员", "dispatcher": "调度员", "township": "乡镇",
             "supply_manager": "物资管理员"}
LEVEL_TEXT = {"blue": "蓝色预警", "yellow": "黄色预警",
              "orange": "橙色预警", "red": "红色预警"}

# 同一水库的供水保障操作串行化（申报/审核/执行/完成共用一把库锁）
_plan_locks_guard = threading.Lock()
_plan_locks: Dict[int, threading.Lock] = {}


def _reservoir_lock(reservoir_id: int) -> threading.Lock:
    with _plan_locks_guard:
        return _plan_locks.setdefault(reservoir_id, threading.Lock())


# ---------------- 水库枯水工況 ----------------
def effective_drought_warn_level(res: Reservoir) -> float:
    """有效枯水预警水位：显式配置优先；缺省取死水位与正常蓄水位间 0.4 分位。"""
    if res.drought_warn_level:
        return float(res.drought_warn_level)
    dead = res.dead_level or 0.0
    normal = res.normal_level or dead
    if normal <= dead:
        return dead
    return round(dead + 0.4 * (normal - dead), 2)


def drought_level(res: Reservoir, level: float | None = None) -> str:
    """按水位在枯水预警线与死水位间的位置判定枯水等级。

    预警线以上不预警；至死水位等分为 blue/yellow/orange/red 四段，
    水位不高于死水位直接红色。
    """
    lv = res.current_level if level is None else level
    warn = effective_drought_warn_level(res)
    dead = res.dead_level or 0.0
    if lv is None or lv >= warn:
        return ""
    if lv <= dead or warn <= dead:
        return "red"
    frac = (warn - lv) / (warn - dead)
    if frac >= 0.75:
        return "red"
    if frac >= 0.5:
        return "orange"
    if frac >= 0.25:
        return "yellow"
    return "blue"


def _released_total_m3(db: Session, plan: WaterSupplyPlan) -> float:
    """该保障单历轮已结算放水量（m³）：来自供水调度台账累计值。

    滚动调整后轮次间按台账累计，避免重复完成时重复扣库容。
    """
    total = (db.query(func.coalesce(func.sum(ReservoirDispatchLog.released_m3), 0.0))
             .filter(ReservoirDispatchLog.kind == "water_supply",
                     ReservoirDispatchLog.ref_id == plan.id).scalar())
    return float(total or 0.0)


def _available_water_m3(db: Session, res: Reservoir,
                        exclude_plan_id: int | None = None) -> float:
    """死水位以上可用水量（m³），扣除其它进行中供水单尚未结算的预留水量。

    已闭环单的已结算水量已实际扣减库容；滚动调整中的保障单，其历轮已结算
    部分同样已扣减，预留只计「计划水量 − 已结算水量」，不重复占库容。
    """
    dead_storage = res.storage_at(res.dead_level or 0.0)  # 万m³
    available = (res.current_storage - dead_storage) * 10000.0
    reserved = 0.0
    q = (db.query(WaterSupplyPlan, WaterSupplyAllocation)
         .join(WaterSupplyAllocation, WaterSupplyAllocation.plan_id == WaterSupplyPlan.id)
         .filter(WaterSupplyPlan.reservoir_id == res.id,
                 WaterSupplyPlan.status.in_(ACTIVE_STATUSES)))
    if exclude_plan_id is not None:
        q = q.filter(WaterSupplyPlan.id != exclude_plan_id)
    for plan, alloc in q.all():
        unsettled = (alloc.planned_m3 or 0.0) - _released_total_m3(db, plan)
        # 正常口径下不会为负（死水位底线拦截），极端存量数据以 0 兜底
        reserved += max(unsettled, 0.0)
    return max(available - reserved, 0.0)


def reservoir_drought_brief(db: Session, res: Reservoir) -> dict:
    """水库枯水态势（总览/台账接口复用）。"""
    warn = effective_drought_warn_level(res)
    dead = res.dead_level or 0.0
    lv = drought_level(res)
    dead_storage = res.storage_at(dead)
    return {
        "dead_level": dead,
        "drought_warn_level": warn,
        "drought_level": lv,
        "drought_level_text": LEVEL_TEXT.get(lv, "") if lv else "",
        "dead_storage": round(dead_storage, 1),
        "available_water_m3": round(_available_water_m3(db, res), 1),
        "latest_dispatch": _latest_dispatch(db, res.id),
    }


def _latest_dispatch(db: Session, reservoir_id: int) -> dict | None:
    log = (db.query(ReservoirDispatchLog)
           .filter(ReservoirDispatchLog.reservoir_id == reservoir_id)
           .order_by(ReservoirDispatchLog.id.desc()).first())
    if log is None:
        return None
    return {
        "id": log.id, "kind": log.kind, "title": log.title,
        "released_m3": round(log.released_m3, 1),
        "level_after": log.level_after, "storage_after": log.storage_after,
        "created_by": log.created_by,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


# ---------------- 快照 ----------------
def _build_snapshot(db: Session, res: Reservoir, plan: WaterSupplyPlan,
                    allocs: list) -> dict:
    """申报/审核快照：申报时水库工况 + 分乡镇配水计划 + 可放水量校验口径。

    滚动调整时保留历轮调整历史（不覆盖），并记录当前轮次与累计已结算水量。
    """
    alloc_items = [{
        "allocation_id": a.id, "township_id": a.township_id,
        "township_name": a.township_name, "planned_m3": a.planned_m3,
        "delivered_m3": a.delivered_m3,
    } for a in allocs]
    total = sum(a.planned_m3 for a in allocs)
    warn = effective_drought_warn_level(res)
    prior_history = list((plan.plan_snapshot or {}).get("adjust_history") or [])
    return {
        "adjust_round": plan.adjust_round or 1,
        "released_total_m3": round(_released_total_m3(db, plan), 1),
        "reservoir": {
            "id": res.id, "name": res.name,
            "level": round(res.current_level, 2),
            "storage": round(res.current_storage, 1),
            "dead_level": res.dead_level or 0.0,
            "drought_warn_level": warn,
            "drought_level": drought_level(res),
        },
        "period_days": plan.period_days,
        "total_planned_m3": round(total, 1),
        "daily_planned_m3": round(total / plan.period_days, 1) if plan.period_days else total,
        "available_water_m3": round(_available_water_m3(db, res, exclude_plan_id=plan.id), 1),
        "allocations": alloc_items,
        "adjust_history": prior_history,
    }


# ---------------- 序列化 ----------------
def serialize_plan(db: Session, plan: WaterSupplyPlan) -> dict:
    res = db.get(Reservoir, plan.reservoir_id)
    allocs = (db.query(WaterSupplyAllocation)
              .filter(WaterSupplyAllocation.plan_id == plan.id)
              .order_by(WaterSupplyAllocation.priority.desc(), WaterSupplyAllocation.id).all())
    emerg_rows = (db.query(WaterSupplyEmergency)
                  .filter(WaterSupplyEmergency.plan_id == plan.id)
                  .order_by(WaterSupplyEmergency.id).all())
    supply_by_id = {s.id: s for s in db.query(Supply).all()}
    warnings = db.query(WarningRecord).filter(
        WarningRecord.water_supply_plan_id == plan.id).count()

    total_planned = sum(a.planned_m3 or 0.0 for a in allocs)
    total_delivered = sum((a.delivered_m3 or 0.0) for a in allocs
                          if a.delivered_m3 is not None)
    released_total = _released_total_m3(db, plan)
    if plan.status == "completed":
        total_shortage = float((plan.completion_snapshot or {})
                               .get("shortage_total_m3", 0.0))
    else:
        total_shortage = sum(max((a.planned_m3 or 0.0) - (a.delivered_m3 or 0.0), 0.0)
                             for a in allocs if a.delivered_m3 is not None)

    alloc_out = []
    for a in allocs:
        shortage = None
        if plan.status == "completed":
            shortage = max((a.planned_m3 or 0.0) - (a.delivered_m3 or 0.0), 0.0)
        elif a.delivered_m3 is not None:
            shortage = max((a.planned_m3 or 0.0) - a.delivered_m3, 0.0)
        alloc_out.append({
            "id": a.id, "township_id": a.township_id,
            "township_name": a.township_name,
            "planned_m3": a.planned_m3,
            "delivered_m3": a.delivered_m3,
            "shortage_m3": shortage,
            "priority": a.priority,
            "priority_note": a.priority_note,
            "priority_confirmed": bool(a.priority_confirmed),
            "created_by": a.created_by,
        })

    emergencies = [{
        "id": e.id, "supply_id": e.supply_id,
        "supply_name": supply_by_id[e.supply_id].name if supply_by_id.get(e.supply_id) else "（已删除）",
        "unit": supply_by_id[e.supply_id].unit if supply_by_id.get(e.supply_id) else "",
        "township_id": e.township_id,
        "shortage_m3": e.shortage_m3, "quantity": e.quantity,
        "disposal_id": e.disposal_id, "note": e.note,
        "created_by": e.created_by,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "round": e.round,
    } for e in emerg_rows]

    adjust_round = plan.adjust_round or 1
    # 滚动调整轮次（第 2 轮起）在状态文案中标注轮次，首轮沿用基础文案
    if adjust_round > 1 and plan.status in ROUND_STATUS_TEXT:
        status_text = f"{ROUND_STATUS_TEXT[plan.status]}（第{adjust_round}轮）"
    elif adjust_round > 1 and plan.status == "completed":
        status_text = f"已完成（第{adjust_round}轮）"
    else:
        status_text = STATUS_TEXT.get(plan.status, plan.status)

    return {
        "id": plan.id,
        "reservoir_id": plan.reservoir_id,
        "reservoir_name": res.name if res else "（水库已删除）",
        "title": plan.title, "reason": plan.reason,
        "period_days": plan.period_days,
        "status": plan.status,
        "status_text": status_text,
        "adjust_round": adjust_round,
        "remark": plan.remark,
        "plan": plan.plan_snapshot or None,
        "completion": plan.completion_snapshot or None,
        "allocations": alloc_out,
        "emergencies": emergencies,
        "total_planned_m3": round(total_planned, 1),
        "total_delivered_m3": round(total_delivered, 1),
        "total_shortage_m3": round(total_shortage, 1),
        "released_total_m3": round(released_total, 1),
        "round_released_m3": round(max(total_delivered - released_total, 0.0), 1),
        "all_priority_confirmed": all(a.priority_confirmed for a in allocs) and bool(allocs),
        "warning_count": warnings,
        "submitted_by": plan.submitted_by, "reviewed_by": plan.reviewed_by,
        "priority_by": plan.priority_by, "executed_by": plan.executed_by,
        "completed_by": plan.completed_by,
        "submitted_at": plan.submitted_at.isoformat() if plan.submitted_at else None,
        "reviewed_at": plan.reviewed_at.isoformat() if plan.reviewed_at else None,
        "priority_at": plan.priority_at.isoformat() if plan.priority_at else None,
        "executed_at": plan.executed_at.isoformat() if plan.executed_at else None,
        "completed_at": plan.completed_at.isoformat() if plan.completed_at else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }


# ---------------- 通用校验 ----------------
def _get_plan(db: Session, plan_id: int) -> WaterSupplyPlan:
    plan = db.get(WaterSupplyPlan, plan_id)
    if plan is None:
        raise HTTPException(404, f"供水保障单 #{plan_id} 不存在")
    return plan


def _require_status(plan: WaterSupplyPlan, allowed: tuple, action: str) -> None:
    if plan.status not in allowed:
        need = "、".join(STATUS_TEXT[s] for s in allowed)
        raise HTTPException(
            409, f"供水保障单当前为「{STATUS_TEXT.get(plan.status, plan.status)}」，"
                 f"不能{action}（须为「{need}」）")


def _require_role(role: str, allowed: tuple, action: str) -> None:
    if role not in allowed:
        need = "、".join(ROLE_TEXT[r] for r in allowed)
        raise HTTPException(403, f"{action}需由{need}执行（当前角色："
                                f"{ROLE_TEXT.get(role, role)}）")


# ---------------- 1. 水库管理员：提交供水计划 ----------------
def submit_plan(db: Session, body: dict) -> dict:
    """水库管理员提交枯水期供水保障计划（含分乡镇配水量）。"""
    _require_role(body.get("role", ""), ("manager",), "提交供水计划")
    reservoir_id = int(body.get("reservoir_id") or 0)
    res = db.get(Reservoir, reservoir_id)
    if res is None or not res.active:
        raise HTTPException(404, "水库不存在或已停用")

    items = body.get("allocations") or []
    clean = []
    seen = set()
    for it in items:
        tid = int(it.get("township_id") or 0)
        planned = float(it.get("planned_m3") or 0)
        town = db.get(Township, tid)
        if town is None or not town.active:
            raise HTTPException(404, f"受水乡镇 #{tid} 不存在或已停用")
        if planned <= 0:
            raise HTTPException(422, f"乡镇「{town.name}」计划配水量须大于 0")
        if tid in seen:
            raise HTTPException(422, f"乡镇「{town.name}」重复申报，请合并为一条")
        seen.add(tid)
        clean.append((town, planned))
    if not clean:
        raise HTTPException(422, "申报计划至少包含一个乡镇的配水量")
    period = int(body.get("period_days") or 0)
    if period <= 0:
        raise HTTPException(422, "计划供水周期须大于 0（日）")

    with _reservoir_lock(reservoir_id):
        clash = (db.query(WaterSupplyPlan)
                 .filter(WaterSupplyPlan.reservoir_id == reservoir_id,
                         WaterSupplyPlan.status.in_(ACTIVE_STATUSES))
                 .first())
        if clash is not None:
            raise HTTPException(
                409, f"水库「{res.name}」已有进行中供水保障单 #{clash.id}"
                     f"（{STATUS_TEXT[clash.status]}），闭环后可重新申报")

        total = sum(p for _, p in clean)
        available = _available_water_m3(db, res)
        if total > available:
            raise HTTPException(
                409, f"水库「{res.name}」死水位以上可用水量仅 {available:.0f} m³，"
                     f"本次申报 {total:.0f} m³ 将突破供水极限，请核减配水量")

        plan = WaterSupplyPlan(
            reservoir_id=reservoir_id,
            title=(body.get("title") or "").strip()
                  or f"{res.name}枯水期供水保障计划",
            reason=(body.get("reason") or "").strip(),
            period_days=period,
            status="submitted",
            submitted_by=(body.get("operator") or "").strip() or "水库管理员")
        db.add(plan)
        db.flush()
        for town, planned in clean:
            db.add(WaterSupplyAllocation(
                plan_id=plan.id, township_id=town.id, township_name=town.name,
                planned_m3=planned, created_by=plan.submitted_by))
        db.flush()
        allocs = db.query(WaterSupplyAllocation).filter(
            WaterSupplyAllocation.plan_id == plan.id).all()
        plan.plan_snapshot = _build_snapshot(db, res, plan, allocs)
        if (body.get("remark") or "").strip():
            plan.remark = f"[申报说明] {body['remark'].strip()}"
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


# ---------------- 1b. 水库管理员：闭环后滚动调整配水 ----------------
def _parse_alloc_items(db: Session, items: list) -> list:
    """解析并校验分乡镇配水条目，返回 [(town, planned_m3)]。"""
    clean = []
    seen = set()
    for it in items:
        tid = int(it.get("township_id") or 0)
        planned = float(it.get("planned_m3") or 0)
        town = db.get(Township, tid)
        if town is None or not town.active:
            raise HTTPException(404, f"受水乡镇 #{tid} 不存在或已停用")
        if planned <= 0:
            raise HTTPException(422, f"乡镇「{town.name}」计划配水量须大于 0")
        if tid in seen:
            raise HTTPException(422, f"乡镇「{town.name}」重复申报，请合并为一条")
        seen.add(tid)
        clean.append((town, planned))
    if not clean:
        raise HTTPException(422, "配水计划至少包含一个乡镇的配水量")
    return clean


def adjust_plan(db: Session, body: dict) -> dict:
    """已闭环保障单滚动调整配水：轮次 +1，保留已供水与应急出库记录。

    - 仅 completed 单可由水库管理员发起；调整后回到 submitted 重新走
      调度员审核 → 乡镇确认优先级 → 执行配水 → 完成结算；
    - 原保障乡镇不得移除（已供水记录保留），其计划水量不得低于累计已供水；
      可核增原乡镇计划水量并追加新受水乡镇；
    - 死水位可供水按「新轮计划总水量 − 历轮已结算水量」重新校验
      （历轮水量已从库容扣减，不重复占用）；
    - 乡镇优先级需重新确认（保留上一轮顺位与说明作参考，确认标记重置）；
    - 上一轮完成快照归档进历轮结算历史，应急出库记录原样保留。
    """
    _require_role(body.get("role", ""), ("manager",), "滚动调整配水")
    plan = _get_plan(db, int(body.get("plan_id") or 0))
    _require_status(plan, ("completed",), "滚动调整配水")

    clean = _parse_alloc_items(db, body.get("allocations") or [])
    period = int(body.get("period_days") or 0)
    if period <= 0:
        raise HTTPException(422, "计划供水周期须大于 0（日）")

    with _reservoir_lock(plan.reservoir_id):
        db.refresh(plan)
        _require_status(plan, ("completed",), "滚动调整配水")
        res = db.get(Reservoir, plan.reservoir_id)
        prev_round = plan.adjust_round or 1
        allocs = db.query(WaterSupplyAllocation).filter(
            WaterSupplyAllocation.plan_id == plan.id).all()
        old_by_tid = {a.township_id: a for a in allocs}

        # 身份保留：原保障乡镇不得移除（已供水记录须保留）
        new_tids = {town.id for town, _ in clean}
        missing = [a.township_name for tid, a in old_by_tid.items() if tid not in new_tids]
        if missing:
            raise HTTPException(
                422, f"原受水乡镇（{'、'.join(missing)}）已有供水记录，滚动调整不得移除；"
                     f"如需停止供水请在其计划水量不低于累计已供水的前提下保留条目")

        # 计划水量不得低于累计已供水（防重复扣库容/负欠供的口径底线）
        for town, planned in clean:
            old = old_by_tid.get(town.id)
            if old is not None and planned + 1e-6 < (old.delivered_m3 or 0.0):
                raise HTTPException(
                    422, f"乡镇「{town.name}」新计划水量 {planned:.0f} m³ 不得低于其"
                         f"累计已供水 {old.delivered_m3:.0f} m³（已供水记录保留不回退）")

        total = sum(p for _, p in clean)
        released_total = _released_total_m3(db, plan)
        future_need = total - released_total
        available = _available_water_m3(db, res, exclude_plan_id=plan.id)
        if future_need > available:
            raise HTTPException(
                409, f"滚动调整未通过：历轮已结算放水 {released_total:.0f} m³，"
                     f"新一轮计划 {total:.0f} m³ 中尚需放水 {future_need:.0f} m³，"
                     f"而水库「{res.name}」死水位以上仅剩 {available:.0f} m³ 可放，"
                     f"请核减配水量或转应急供水")

        # 归档上一轮完成快照（历轮结算历史）
        prev_completion = dict(plan.completion_snapshot or {})
        prior_rounds = list(prev_completion.pop("rounds", []))
        prev_completion.setdefault("round", prev_round)
        prior_rounds.append(prev_completion)

        # 配水明细：原条目核增（重置优先级确认标记、保留已供水），新乡镇增量挂接
        for town, planned in clean:
            old = old_by_tid.get(town.id)
            if old is not None:
                old.planned_m3 = planned
                old.township_name = town.name
                old.priority_confirmed = 0   # 轮次变更后乡镇重新确认优先级
            else:
                db.add(WaterSupplyAllocation(
                    plan_id=plan.id, township_id=town.id, township_name=town.name,
                    planned_m3=planned,
                    created_by=(body.get("operator") or "").strip() or "水库管理员"))

        plan.period_days = period
        if (body.get("title") or "").strip():
            plan.title = body["title"].strip()
        if body.get("reason") is not None:
            plan.reason = (body.get("reason") or "").strip()

        new_round = prev_round + 1
        plan.adjust_round = new_round
        plan.status = "submitted"
        # 新一轮签名/时间戳重置（申报人保留），等待重走审核→优先级→执行→完成
        plan.reviewed_by, plan.reviewed_at = "", None
        plan.priority_by, plan.priority_at = "", None
        plan.executed_by, plan.executed_at = "", None
        plan.completed_by, plan.completed_at = "", None
        plan.completion_snapshot = {"rounds": prior_rounds}

        db.flush()
        allocs = db.query(WaterSupplyAllocation).filter(
            WaterSupplyAllocation.plan_id == plan.id).all()
        snapshot = _build_snapshot(db, res, plan, allocs)
        # 记录本轮调整前的上一轮结算，形成滚动调整链
        snapshot["adjust_history"].append({
            "round": prev_round,
            "at": prev_completion.get("at"),
            "period_days": prev_completion.get("period_days"),
            "planned_total_m3": prev_completion.get("planned_total_m3"),
            "released_m3": prev_completion.get("released_m3"),
            "cumulative_delivered_m3": prev_completion.get("cumulative_delivered_m3"),
            "shortage_total_m3": prev_completion.get("shortage_total_m3"),
            "level_after": prev_completion.get("level_after"),
            "storage_after": prev_completion.get("storage_after"),
            "by": (body.get("operator") or "").strip() or "水库管理员",
            "note": (body.get("remark") or "").strip(),
        })
        plan.plan_snapshot = snapshot

        line = (f"[滚动调整·第{new_round}轮] {snapshot['adjust_history'][-1]['by']} "
                f"在第{prev_round}轮闭环后调整配水：新一轮计划 {total:.0f} m³"
                f"（历轮已结算 {released_total:.0f} m³，本轮尚需放水 "
                f"{future_need:.0f} m³），待调度员重新审核")
        if (body.get("remark") or "").strip():
            line += f"。调整说明：{body['remark'].strip()}"
        plan.remark = (plan.remark + "\n" + line).strip()

        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


# ---------------- 2. 调度员：审核 ----------------
def review_plan(db: Session, plan_id: int, operator: str, role: str,
                opinion: str = "") -> dict:
    """调度员审核供水计划：复核可放水量并登记枯水/供水预警。"""
    _require_role(role, ("dispatcher",), "审核供水计划")
    plan = _get_plan(db, plan_id)
    _require_status(plan, ("submitted",), "审核")

    with _reservoir_lock(plan.reservoir_id):
        db.refresh(plan)
        _require_status(plan, ("submitted",), "审核")
        res = db.get(Reservoir, plan.reservoir_id)
        allocs = db.query(WaterSupplyAllocation).filter(
            WaterSupplyAllocation.plan_id == plan.id).all()
        total = sum(a.planned_m3 for a in allocs)
        released_total = _released_total_m3(db, plan)
        future_need = max(total - released_total, 0.0)
        available = _available_water_m3(db, res, exclude_plan_id=plan.id)
        if future_need > available:
            raise HTTPException(
                409, f"复核未通过：水库「{res.name}」死水位以上可用水量仅 "
                     f"{available:.0f} m³，本轮尚需放水 {future_need:.0f} m³"
                     f"（计划 {total:.0f} m³，历轮已结算 {released_total:.0f} m³），"
                     f"不足以保障计划供水")

        # 审核时按当前工况滚动快照（保留历轮调整历史）
        plan.plan_snapshot = _build_snapshot(db, res, plan, allocs)

        # 登记/刷新供水（枯水）预警：水位已低于枯水预警线时才登记，按身份幂等。
        # 新一轮审核重新激活上轮已销警的预警（滚动配水期间持续抗旱）。
        lv = drought_level(res)
        if lv:
            rnd = plan.adjust_round or 1
            round_txt = "" if rnd <= 1 else f"（第{rnd}轮滚动配水）"
            msg = (f"{res.name}当前水位{res.current_level:.2f}m（枯水预警线"
                   f"{effective_drought_warn_level(res):.2f}m），"
                   f"已启动枯水期供水保障{round_txt}：计划供水{total:.0f}m³"
                   f"/{plan.period_days}日，历轮已供水{released_total:.0f}m³")
            _upsert_supply_warning(db, plan, res, lv, msg, status="active")

        plan.status = "reviewed"
        plan.reviewed_by = operator.strip() or "值班调度员"
        plan.reviewed_at = datetime.now()
        if opinion.strip():
            plan.remark = (plan.remark + f"\n[审核意见·第{plan.adjust_round or 1}轮] "
                                         f"{opinion.strip()}").strip()
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


def _upsert_supply_warning(db: Session, plan: WaterSupplyPlan, res: Reservoir,
                           level: str, message: str, status: str) -> WarningRecord:
    """供水保障预警按 plan_id 幂等写入（刷新等级/文案，保留首次触发时间）。"""
    rec = (db.query(WarningRecord)
           .filter(WarningRecord.water_supply_plan_id == plan.id).first())
    if rec is None:
        rec = WarningRecord(
            run_id=None, water_supply_plan_id=plan.id,
            target_type="reservoir", target_id=res.id, target_name=res.name,
            kind="water_supply", level=level,
            value=round(res.current_level, 2),
            threshold=effective_drought_warn_level(res),
            message=message, status=status)
        db.add(rec)
        return rec
    rec.target_name = res.name
    rec.level = level
    rec.value = round(res.current_level, 2)
    rec.threshold = effective_drought_warn_level(res)
    rec.message = message
    if status:
        rec.status = status
    return rec


# ---------------- 3. 乡镇：确认优先级 ----------------
def set_priority(db: Session, plan_id: int, body: dict) -> dict:
    """乡镇为所辖配水条目确认供水优先级（1 最高，数字越小越优先）。"""
    _require_role(body.get("role", ""), ("township",), "确认供水优先级")
    plan = _get_plan(db, plan_id)
    _require_status(plan, ("reviewed",), "确认优先级")
    alloc = (db.query(WaterSupplyAllocation)
             .filter(WaterSupplyAllocation.plan_id == plan_id,
                     WaterSupplyAllocation.township_id == int(body.get("township_id") or 0))
             .first())
    if alloc is None:
        raise HTTPException(404, "该乡镇不在本供水保障单配水范围中")
    priority = int(body.get("priority") or 0)
    if priority < 1:
        raise HTTPException(422, "优先级须为大于 0 的整数（1 为最高）")
    alloc.priority = priority
    alloc.priority_note = (body.get("note") or "").strip()
    alloc.priority_confirmed = 1
    db.commit()
    db.refresh(plan)
    return serialize_plan(db, plan)


def confirm_priorities(db: Session, plan_id: int, operator: str, role: str,
                       note: str = "") -> dict:
    """乡镇确认全部优先级并启动执行配水（reviewed → executing）。"""
    _require_role(role, ("township",), "启动配水执行")
    plan = _get_plan(db, plan_id)
    _require_status(plan, ("reviewed",), "启动配水执行")

    with _reservoir_lock(plan.reservoir_id):
        db.refresh(plan)
        _require_status(plan, ("reviewed",), "启动配水执行")
        allocs = db.query(WaterSupplyAllocation).filter(
            WaterSupplyAllocation.plan_id == plan.id).all()
        pending = [a for a in allocs if not a.priority_confirmed]
        if pending:
            names = "、".join(a.township_name for a in pending)
            raise HTTPException(409, f"尚有乡镇未确认供水优先级：{names}，全部确认后方可执行配水")

        # 执行启动：供水预警进入处置中
        warnings = db.query(WarningRecord).filter(
            WarningRecord.water_supply_plan_id == plan.id).all()
        for w in warnings:
            if w.status == "active":
                w.status = "handling"

        plan.status = "executing"
        plan.executed_by = operator.strip() or "乡镇水务站"
        plan.executed_at = datetime.now()
        # 优先级整体确认以最后一位签名乡镇为准留痕
        plan.priority_by = operator.strip() or "乡镇水务站"
        plan.priority_at = datetime.now()
        if note.strip():
            plan.remark = (plan.remark
                           + f"\n[优先级确认·第{plan.adjust_round or 1}轮] {note.strip()}").strip()
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


# ---------------- 4. 乡镇：执行期上报实际供水 ----------------
def report_delivery(db: Session, plan_id: int, body: dict) -> dict:
    """乡镇上报某乡镇实际供水量（可小于计划量，差额计为欠供）。"""
    _require_role(body.get("role", ""), ("township",), "上报实际供水")
    plan = _get_plan(db, plan_id)
    _require_status(plan, ("executing",), "上报实际供水")
    alloc = (db.query(WaterSupplyAllocation)
             .filter(WaterSupplyAllocation.plan_id == plan_id,
                     WaterSupplyAllocation.township_id == int(body.get("township_id") or 0))
             .first())
    if alloc is None:
        raise HTTPException(404, "该乡镇不在本供水保障单配水范围中")
    delivered = float(body.get("delivered_m3"))
    if delivered < 0:
        raise HTTPException(422, "实际供水量不能为负")
    if delivered > (alloc.planned_m3 or 0.0) + 1e-6:
        raise HTTPException(
            422, f"实际供水量 {delivered:.0f} m³ 超过计划量 {alloc.planned_m3:.0f} m³，"
                 f"超计划用水请先变更保障计划")
    alloc.delivered_m3 = delivered
    db.commit()
    db.refresh(plan)
    return serialize_plan(db, plan)


def _plan_shortage_m3(plan: WaterSupplyPlan, allocs: list) -> float:
    """当前欠供水量：执行中未上报按 0 计；完成后以完成快照冻结值为准。"""
    if plan.status == "completed":
        return float((plan.completion_snapshot or {}).get("shortage_total_m3", 0.0))
    return sum(max((a.planned_m3 or 0.0) - (a.delivered_m3 or 0.0), 0.0)
               for a in allocs if a.delivered_m3 is not None)


# ---------------- 5. 物资管理员：异常欠供追加应急物资 ----------------
def add_emergency(db: Session, plan_id: int, body: dict) -> dict:
    """异常欠供时物资管理员追加应急物资（直接出库，可挂接既有防汛处置单）。"""
    _require_role(body.get("role", ""), ("supply_manager",), "追加应急物资")
    plan = _get_plan(db, plan_id)
    _require_status(plan, ("executing", "completed"), "追加应急物资")
    supply_id = int(body.get("supply_id") or 0)
    quantity = int(body.get("quantity") or 0)
    if quantity <= 0:
        raise HTTPException(422, "追加数量须大于 0")

    allocs = db.query(WaterSupplyAllocation).filter(
        WaterSupplyAllocation.plan_id == plan.id).all()
    shortage = _plan_shortage_m3(plan, allocs)
    if shortage <= 0:
        raise HTTPException(409, "当前无欠供：实际供水不小于计划量，无需追加应急物资")

    township_id = body.get("township_id")
    if township_id:
        township_id = int(township_id)
        exists = any(a.township_id == township_id for a in allocs)
        if not exists:
            raise HTTPException(404, "该乡镇不在本供水保障单配水范围中")

    disposal_id = body.get("disposal_id")
    if disposal_id:
        disposal_id = int(disposal_id)
        if db.get(DisposalOrder, disposal_id) is None:
            raise HTTPException(404, f"挂接的防汛处置单 #{disposal_id} 不存在")

    # 与防汛处置共用物资库存池：同物资的占用检查/出库串行化
    with resource_svc._resource_lock("supply", supply_id):
        supply = db.get(Supply, supply_id)
        if supply is None or not supply.active:
            raise HTTPException(404, "应急物资不存在或已停用")
        if quantity > supply.stock:
            raise HTTPException(
                409, f"物资「{supply.name}」当前库存 {supply.stock}{supply.unit}，"
                     f"不足以追加 {quantity}{supply.unit}")

        rnd = plan.adjust_round or 1
        rec = WaterSupplyEmergency(
            plan_id=plan.id, supply_id=supply.id, township_id=township_id,
            shortage_m3=round(shortage, 1), quantity=quantity,
            disposal_id=disposal_id, note=(body.get("note") or "").strip(),
            round=rnd,
            created_by=(body.get("operator") or "").strip() or "物资管理员")
        db.add(rec)
        supply.stock -= quantity

        # 兼容原有处置记录：挂接时仅在处置记录追加一行，不改动处置单状态机
        if disposal_id:
            order = db.get(DisposalOrder, disposal_id)
            town_name = ""
            if township_id:
                town = db.get(Township, township_id)
                town_name = f"「{town.name}」" if town else ""
            round_txt = "" if rnd <= 1 else f"第{rnd}轮滚动配水 "
            line = (f"[应急物资联动·枯水供水 #{plan.id}]{round_txt}{rec.created_by} "
                    f"因供水欠供{shortage:.0f}m³，向{town_name}追加「{supply.name}」"
                    f"{quantity}{supply.unit}（直接出库）")
            if rec.note:
                line += f"。说明：{rec.note}"
            order.remark = ((order.remark or "") + "\n" + line).strip()

        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


# ---------------- 6. 乡镇：完成（扣减库容、回写调度与预警） ----------------
def complete_plan(db: Session, plan_id: int, operator: str, role: str,
                  summary: str = "") -> dict:
    """完成供水保障：结算欠供、只按本轮增量扣减库容、回写调度记录与预警。

    滚动调整口径（重复提交不重复扣库容）：
        本轮放水量 = 各乡累计实供合计 − 历轮已结算放水量（调度台账累计）。
    - 增量 > 0：扣减本轮水量，追加调度记录；
    - 增量 = 0：重复完成（滚动后无新增供水），只结算状态，不扣库容、
      不重复写调度台账（幂等返回）；
    - 累计放水量仍以死水位为不可突破底线（409 拦截、事务回滚）。
    """
    _require_role(role, ("township",), "完成供水保障")
    plan = _get_plan(db, plan_id)
    _require_status(plan, ("executing",), "完成")

    with _reservoir_lock(plan.reservoir_id):
        db.refresh(plan)
        _require_status(plan, ("executing",), "完成")
        res = db.get(Reservoir, plan.reservoir_id)
        allocs = db.query(WaterSupplyAllocation).filter(
            WaterSupplyAllocation.plan_id == plan.id).all()

        # 未上报实际供水的乡镇按足额供水结算（欠供以已上报缺口为准）。
        # 滚动轮次中已上报的累计实供保留，新增条目/未上报条目按其当前计划足额。
        for a in allocs:
            if a.delivered_m3 is None:
                a.delivered_m3 = a.planned_m3

        rnd = plan.adjust_round or 1
        total_planned = sum(a.planned_m3 for a in allocs)
        total_delivered = sum(a.delivered_m3 for a in allocs)
        total_shortage = sum(max(a.planned_m3 - a.delivered_m3, 0.0) for a in allocs)
        released_before = _released_total_m3(db, plan)
        # 本轮增量（重复完成/滚动后无新增供水时为 0，不重复扣库容）
        round_released = max(total_delivered - released_before, 0.0)

        level_before = res.current_level
        storage_before = res.current_storage
        dead_storage = res.storage_at(res.dead_level or 0.0)  # 万m³
        round_storage = round_released / 10000.0              # m³ → 万m³
        if storage_before - round_storage < dead_storage - 1e-9:
            allow_m3 = max((storage_before - dead_storage) * 10000.0, 0.0)
            raise HTTPException(
                409, f"本轮放水 {round_released:.0f} m³（累计实供 {total_delivered:.0f} m³，"
                     f"历轮已结算 {released_before:.0f} m³）将使水库「{res.name}」"
                     f"低于死水位库容 {dead_storage:.1f} 万m³（死水位以上仅剩 "
                     f"{allow_m3:.0f} m³ 可放）；请按实供水量上报、缺口转应急供水")

        town_results = [{
            "township_id": a.township_id, "township_name": a.township_name,
            "planned_m3": round(a.planned_m3, 1),
            "delivered_m3": round(a.delivered_m3, 1),
            "shortage_m3": round(max(a.planned_m3 - a.delivered_m3, 0.0), 1),
            "priority": a.priority,
        } for a in allocs]
        emergency_round_n = db.query(WaterSupplyEmergency).filter(
            WaterSupplyEmergency.plan_id == plan.id,
            WaterSupplyEmergency.round == rnd).count()
        emergency_n = db.query(WaterSupplyEmergency).filter(
            WaterSupplyEmergency.plan_id == plan.id).count()

        new_storage = storage_before
        new_level = level_before
        log = None

        if round_released > 0:
            # ---- 回写 1：只扣减本轮增量库容/水位 ----
            new_storage = storage_before - round_storage
            new_level = res.level_at(new_storage)
            res.current_storage = new_storage
            res.current_level = new_level

            # ---- 回写 2：水库调度记录台账（只追加本轮增量，与防汛调度并存）----
            round_txt = "" if rnd <= 1 else f"（第{rnd}轮）"
            log = ReservoirDispatchLog(
                reservoir_id=res.id, kind="water_supply", ref_id=plan.id,
                title=f"{plan.title}{round_txt}", released_m3=round_released,
                level_before=round(level_before, 2), level_after=round(new_level, 2),
                storage_before=round(storage_before, 1),
                storage_after=round(new_storage, 1),
                payload={"round": rnd,
                         "period_days": plan.period_days,
                         "planned_m3": round(total_planned, 1),
                         "cumulative_delivered_m3": round(total_delivered, 1),
                         "released_before_m3": round(released_before, 1),
                         "shortage_m3": round(total_shortage, 1),
                         "emergency_count": emergency_round_n,
                         "emergency_cumulative": emergency_n,
                         "townships": town_results},
                created_by=operator.strip() or "乡镇水务站")
            db.add(log)

        # ---- 回写 3：预警（按最新水位重新校验，滚动轮次持续跟进）----
        # 供水后仍处枯水预警线以下：升级/保留枯水预警；否则本单供水预警销警。
        post_lv = drought_level(res)
        warn = db.query(WarningRecord).filter(
            WarningRecord.water_supply_plan_id == plan.id).first()
        if post_lv:
            msg = (f"{res.name}{('第%d轮' % rnd) if rnd > 1 else ''}累计供水"
                   f"{total_delivered:.0f}m³后水位{new_level:.2f}m"
                   f"（死水位{res.dead_level or 0:.2f}m），"
                   f"达{LEVEL_TEXT[post_lv]}，需继续抗旱补水")
            if warn is not None:
                warn.level = post_lv
                warn.value = round(new_level, 2)
                warn.threshold = effective_drought_warn_level(res)
                warn.message = msg
                # 已销警后水位再度跌破预警线（如滚动调整新一轮）：重新激活；
                # 处置中状态保持
                if warn.status == "cleared":
                    warn.status = "active"
            else:
                db.add(WarningRecord(
                    run_id=None, water_supply_plan_id=plan.id,
                    target_type="reservoir", target_id=res.id, target_name=res.name,
                    kind="water_supply", level=post_lv, value=round(new_level, 2),
                    threshold=effective_drought_warn_level(res),
                    message=msg, status="handling"))
        elif warn is not None:
            warn.status = "cleared"
            warn.message = (f"{res.name}枯水期供水保障第{rnd}轮结束：累计供水"
                            f"{total_delivered:.0f}m³，水位恢复至枯水预警线以上")

        # 历轮结算历史（滚动调整时从上一轮完成快照迁移而来）
        prior_rounds = list((plan.completion_snapshot or {}).get("rounds") or [])

        completion = {
            "round": rnd,
            "at": datetime.now().isoformat(),
            "released_m3": round(round_released, 1),                 # 本轮实际扣减量
            "cumulative_delivered_m3": round(total_delivered, 1),    # 累计实供
            "released_total_m3": round(released_before + round_released, 1),
            "idempotent": round_released <= 0,                       # 零增量=重复提交
            "planned_total_m3": round(total_planned, 1),
            "shortage_total_m3": round(total_shortage, 1),
            "emergency_count": emergency_round_n,
            "emergency_cumulative": emergency_n,
            "period_days": plan.period_days,
            "level_before": round(level_before, 2),
            "level_after": round(new_level, 2),
            "storage_before": round(storage_before, 1),
            "storage_after": round(new_storage, 1),
            "dead_level": res.dead_level or 0.0,
            "drought_level_after": post_lv,
            "townships": town_results,
            "dispatch_log_id": None,  # flush 后回填；零增量保持 None
            "rounds": prior_rounds,
        }

        plan.status = "completed"
        plan.completed_by = operator.strip() or "乡镇水务站"
        plan.completed_at = datetime.now()
        if summary.strip():
            plan.remark = (plan.remark
                           + f"\n[完成小结·第{rnd}轮] {summary.strip()}").strip()
        if round_released <= 0:
            plan.remark = (plan.remark
                           + f"\n[重复提交·第{rnd}轮] {plan.completed_by} 再次完成确认："
                             f"本轮无新增供水（累计已结算 {released_before:.0f} m³），"
                             f"不重复扣减库容、不重复登记调度台账").strip()
        db.flush()
        if log is not None:
            completion["dispatch_log_id"] = log.id
        plan.completion_snapshot = completion
        db.commit()
        db.refresh(plan)
        return serialize_plan(db, plan)


# ---------------- 台账查询 ----------------
def list_plans(db: Session, limit: int = 50) -> list:
    rows = (db.query(WaterSupplyPlan)
            .order_by(WaterSupplyPlan.id.desc()).limit(limit).all())
    return [serialize_plan(db, p) for p in rows]


def get_plan(db: Session, plan_id: int) -> dict:
    return serialize_plan(db, _get_plan(db, plan_id))


def list_townships(db: Session) -> list:
    return [{"id": t.id, "name": t.name, "contact": t.contact,
             "demand_m3": t.demand_m3, "active": t.active, "x": t.x, "y": t.y}
            for t in db.query(Township).filter(Township.active == 1)
            .order_by(Township.id).all()]


def list_dispatch_logs(db: Session, reservoir_id: int | None = None) -> list:
    q = db.query(ReservoirDispatchLog).order_by(ReservoirDispatchLog.id.desc())
    if reservoir_id:
        q = q.filter(ReservoirDispatchLog.reservoir_id == reservoir_id)
    res_by_id = {r.id: r for r in db.query(Reservoir).all()}
    out = []
    for log in q.limit(100).all():
        out.append({
            "id": log.id, "reservoir_id": log.reservoir_id,
            "reservoir_name": res_by_id[log.reservoir_id].name
            if res_by_id.get(log.reservoir_id) else "（已删除）",
            "kind": log.kind, "ref_id": log.ref_id, "title": log.title,
            "kind_text": "供水调度" if log.kind == "water_supply" else "防洪调度",
            "released_m3": round(log.released_m3, 1),
            "level_before": log.level_before, "level_after": log.level_after,
            "storage_before": log.storage_before, "storage_after": log.storage_after,
            "payload": log.payload,
            "created_by": log.created_by,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        })
    return out
