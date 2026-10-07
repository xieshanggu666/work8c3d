"""存量库迁移：枯水期供水保障。

在防汛处置与应急资源协同基础上再做四件事（脚本幂等，可重复执行）：

1. 建立供水保障表 townships / water_supply_plans / water_supply_allocations /
   water_supply_emergencies / reservoir_dispatch_logs
   （Base.metadata.create_all 自动补建，不动任何历史数据）；
2. reservoirs 增加 dead_level 死水位、drought_warn_level 枯水预警水位列
   （历史水库保持 0.0，服务层按正常蓄水位/死水位推算，不影响既有防洪演算）；
3. warning_records 增加 water_supply_plan_id 关联列
   （历史预警保持 NULL，原样展示，不参与既有唯一约束）；
4. 乡镇台账为空时补入演示受水乡镇（已有数据则跳过）。

用法：python scripts/migrate_water_supply.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core.database import Base, engine, SessionLocal
from app.models import Township  # noqa: F401  导入即注册到 Base.metadata

IMPORTED = True  # noqa: F841
from app import models  # noqa: E402,F401


def _columns(conn, table):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def _add_column_if_missing(conn, table, column, ddl):
    if column not in _columns(conn, table):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        print(f"  + {table}.{column} 已添加")
    else:
        print(f"  = {table}.{column} 已存在，跳过")


def _seed_demo_townships():
    """乡镇台账为空时补入演示受水乡镇（用户已录入则不覆盖）。"""
    db = SessionLocal()
    try:
        if db.query(Township).count() == 0:
            db.add_all([
                Township(id=1, name="白水渡镇", contact="刘水务 139-1001",
                         demand_m3=1200, x=330, y=440),
                Township(id=2, name="青源乡", contact="陈水务 139-1002",
                         demand_m3=650, x=180, y=300),
                Township(id=3, name="龙潭镇", contact="赵水务 139-1003",
                         demand_m3=900, x=520, y=470),
            ])
            db.commit()
            print("  + 已补入演示受水乡镇：白水渡镇 / 青源乡 / 龙潭镇")
        else:
            print("  = 乡镇台账已有数据，跳过演示数据补入")
    finally:
        db.close()


def main():
    # 1. 补建供水保障相关表（新表，不影响存量数据）
    Base.metadata.create_all(bind=engine)
    print("[1/4] 枯水期供水保障相关表已就绪")

    with engine.begin() as conn:
        print("[2/4] 补充水库枯水水位列（历史水库保持 0.0，按曲线推算）...")
        _add_column_if_missing(conn, "reservoirs", "dead_level",
                               "dead_level FLOAT DEFAULT 0.0")
        _add_column_if_missing(conn, "reservoirs", "drought_warn_level",
                               "drought_warn_level FLOAT DEFAULT 0.0")

        print("[3/4] 补充预警台账供水保障关联列（历史预警保持 NULL）...")
        _add_column_if_missing(conn, "warning_records", "water_supply_plan_id",
                               "water_supply_plan_id INTEGER")

        print("[4/4] 配水明细唯一索引（同一单同一乡镇至多一条）...")
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_water_supply_alloc "
            "ON water_supply_allocations (plan_id, township_id)"))
        print("  + uq_water_supply_alloc")

    _seed_demo_townships()
    print("迁移完成：枯水期供水保障（申请/审核/优先级/执行/完成扣减库容/应急物资）已启用。")


if __name__ == "__main__":
    main()
