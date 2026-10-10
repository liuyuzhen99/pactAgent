"""Agent 的"大脑"：把任务变成采购计划（T3）。

MVP 阶段这里就是一段**确定性逻辑**——看任务需要什么，列出要买哪几笔。
但它是**一个可替换的函数**，不是硬编码在流程里：等要接 LLM 时，写一个同样签名的
planner 传进去即可（`make_plan(task, budget, planner=llm_planner)`），下游一行不用改。

Agent 的职责边界：**决定买什么、按什么顺序买、每笔花多少**。
它不负责判断"能不能买"——那是 Pact 策略层的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

# USDC 是 6 位小数；x402 的 accepts[].amount 用的就是原子单位字符串。
USDC_DECIMALS = 6


@dataclass(frozen=True)
class Purchase:
    """一笔计划中的采购。`resource` 同时是收款方的路径，也是 x402 的资源标识。"""

    resource: str
    amount_usd: Decimal
    description: str


@dataclass(frozen=True)
class Plan:
    task: str
    budget_usd: Decimal
    purchases: tuple[Purchase, ...]


# 一个 planner 就是"给任务和预算，返回要买哪几笔"。签名固定，实现可换。
Planner = Callable[[str, Decimal], tuple[Purchase, ...]]


def usd_to_atomic(amount: Decimal, decimals: int = USDC_DECIMALS) -> str:
    """整币金额 -> 原子单位字符串（`0.005` -> `"5000"`）。

    比原子单位还小的精度**报错而不是静默截断**：静默截断会把"0.0000001 元"
    变成免费，那正是"看起来对、实则错"的一类。
    """
    scaled = amount.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} 比原子单位（{decimals} 位小数）还精细，不能表示")
    return str(int(scaled))


def default_planner(task: str, budget_usd: Decimal) -> tuple[Purchase, ...]:
    """确定性规划：做一份数据报告 = 买一份原始数据 + 买一份分析结论。

    两笔价格故意一大一小（0.005 / 0.05），这样在 Pact 单笔上限 0.01 之下
    必然出现"一笔通过、一笔被拒"，演示才有戏可演。
    """
    return (
        Purchase(resource="/api/cheap", amount_usd=Decimal("0.005"), description="原始运营数据"),
        Purchase(resource="/api/premium", amount_usd=Decimal("0.05"), description="深度分析结论"),
    )


def make_plan(task: str, budget_usd: Decimal, planner: Planner = default_planner) -> Plan:
    """把任务和预算变成一份采购计划。

    `planner` 可注入是为了让"大脑"可替换（R13）——默认是确定性逻辑，
    将来换成 LLM 时下游无需改动。
    """
    return Plan(task=task, budget_usd=budget_usd, purchases=planner(task, budget_usd))
