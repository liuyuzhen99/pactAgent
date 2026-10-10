"""采购计划（T3）的验收单测。

对应 R2（任务 + 总预算进系统）、R3（Agent 自己算出要买哪两笔）、
R13（规划逻辑**可替换**、不是硬编码在流程里）、以及 R-3 的单位口径。

金额一律用 `Decimal` 断言，**不用 float**——0.005/0.05 在二进制浮点里不可精确表示，
用 float 相等断言要么恒真要么脆。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pactpay.planner import Purchase, default_planner, make_plan, usd_to_atomic

TASK = "帮我买一份数据报告"
BUDGET = Decimal("0.1")


def test_plan_has_exactly_the_two_purchases_from_the_requirement():
    plan = make_plan(TASK, BUDGET)

    assert plan.task == TASK
    assert plan.budget_usd == Decimal("0.1")
    assert [p.resource for p in plan.purchases] == ["/api/cheap", "/api/premium"]
    assert [p.amount_usd for p in plan.purchases] == [Decimal("0.005"), Decimal("0.05")]


def test_total_of_the_plan_is_within_the_budget():
    """两笔加起来 0.055 <= 0.1——所以"总量超预算"不是这个 demo 要演的戏，
    单笔超限才是。这条断言把"计划本身是合理的"钉住。"""
    plan = make_plan(TASK, BUDGET)

    assert sum(p.amount_usd for p in plan.purchases) == Decimal("0.055")
    assert sum(p.amount_usd for p in plan.purchases) <= plan.budget_usd


# ---- R13：规划逻辑必须可替换，不是硬编码在流程里 ----

def test_injected_planner_is_actually_used():
    """注入一个假 planner（只买 1 笔、金额也不一样）——下游必须真的用它。

    这条是 R13 的核心断言：如果流程把 planner 写死，这个测试会红。
    """
    calls: list[tuple[str, Decimal]] = []

    def fake_planner(task: str, budget_usd: Decimal) -> tuple[Purchase, ...]:
        calls.append((task, budget_usd))
        return (Purchase(resource="/api/premium", amount_usd=Decimal("0.05"), description="只买贵的"),)

    plan = make_plan(TASK, BUDGET, planner=fake_planner)

    assert calls == [(TASK, BUDGET)]                      # 真的被调用了，且参数原样传下去
    assert len(plan.purchases) == 1                        # 输出**随之改变**
    assert plan.purchases[0].resource == "/api/premium"    # 不再是默认的两笔
    assert [p.resource for p in plan.purchases] != [p.resource for p in default_planner(TASK, BUDGET)]


def test_default_planner_is_swappable_without_touching_callers():
    """默认 planner 也能被单独调用——说明它是普通函数，不是流程里的私有分支。"""
    purchases = default_planner(TASK, BUDGET)

    assert isinstance(purchases, tuple)
    assert all(isinstance(p, Purchase) for p in purchases)
    assert len(purchases) == 2


# ---- R-3：两套单位口径之间的换算 ----

@pytest.mark.parametrize(
    ("usd", "want"),
    [
        ("0.005", "5000"),    # 便宜那笔
        ("0.05", "50000"),    # 贵那笔
        ("0.01", "10000"),    # Pact 的单笔上限
        ("0.1", "100000"),    # 总预算
        ("1", "1000000"),
        ("0", "0"),
    ],
)
def test_usd_to_atomic_matches_the_x402_wire_format(usd: str, want: str):
    """x402 的 `accepts[].amount` 是**原子单位字符串**（6 位小数），不是整币。
    收款方实测回的 `amount` 就是 "5000"，所以这个换算必须与它一致。"""
    assert usd_to_atomic(Decimal(usd)) == want


def test_usd_to_atomic_rejects_sub_atomic_precision():
    """比原子单位还小的精度不该被静默截断——静默截断会让"0.0000001 元"变成免费。"""
    with pytest.raises(ValueError):
        usd_to_atomic(Decimal("0.0000001"))
