"""LLM planner（Agent 的"大脑"）的测试。

**一个字节都不出网**：所有用例都通过 `client=` 注入口塞一个假模型。
真调 DeepSeek 的那条路只在 `make demo` 里走，以及一条显式 opt-in 的活体测试。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from pactpay.llm_planner import (
    API_CATALOG,
    LLMError,
    build_prompt,
    llm_planner,
    parse_plan_json,
)

BUDGET = Decimal("0.1")


def _fake_llm(payload: object) -> object:
    """造一个"假模型"：不管 prompt 是什么，都回同一段文本。"""
    text = payload if isinstance(payload, str) else json.dumps(payload)

    def client(prompt: str) -> str:
        return text

    return client


# ── 正常路径 ────────────────────────────────────────────────────────────


def test_picks_both_apis_when_budget_allows():
    """模型在预算内挑了两个接口 —— 这正是演示要的那一幕。

    两个加起来 0.055 < 预算 0.1，**从预算角度看完全合理**。
    而人类的单笔上限是 0.01，所以第二笔会被 Pact 拒绝。
    这就是"Agent 自己算出该买 → 撞上人类划的边界"。
    """
    plan = llm_planner(
        "产出一份数据分析报告",
        BUDGET,
        client=_fake_llm({"purchases": [
            {"resource": "/api/cheap", "reason": "拿原始数据"},
            {"resource": "/api/premium", "reason": "拿深度结论"},
        ]}),
    )

    assert [p.resource for p in plan] == ["/api/cheap", "/api/premium"]
    assert sum(p.amount_usd for p in plan) == Decimal("0.055")


def test_price_comes_from_our_catalog_not_from_the_model():
    """**价格由我们查表决定，不采信模型报的数。**

    否则模型可以自称"/api/premium 只要 0.001"，把预算校验绕过去。
    """
    plan = llm_planner(
        "任务",
        BUDGET,
        client=_fake_llm({"purchases": [
            {"resource": "/api/cheap"},
            {"resource": "/api/premium", "price_usd": "0.0001", "reason": "它很便宜"},
        ]}),
    )

    premium = next(p for p in plan if p.resource == "/api/premium")
    assert premium.amount_usd == Decimal("0.05"), "必须用清单里的价格，不是模型报的"


def test_reason_is_kept_so_a_human_can_see_why():
    """模型给的理由要带进描述里 —— 人看得见"它为什么买这个"。"""
    plan = llm_planner(
        "任务",
        BUDGET,
        client=_fake_llm({"purchases": [
            {"resource": "/api/cheap", "reason": "需要原始明细"},
            {"resource": "/api/premium"},
        ]}),
    )

    cheap = next(p for p in plan if p.resource == "/api/cheap")
    assert "需要原始明细" in cheap.description


def test_markdown_fenced_json_is_tolerated():
    """模型有时会把 JSON 包在 ```json 围栏里。扒掉围栏，其余不猜。"""
    fenced = ('```json\n{"purchases": [{"resource": "/api/cheap"},'
              ' {"resource": "/api/premium"}]}\n```')

    plan = llm_planner("任务", BUDGET, client=_fake_llm(fenced))

    assert [p.resource for p in plan] == ["/api/cheap", "/api/premium"]


# ── 非法输出：一律报错，绝不静默退回硬编码 ──────────────────────────────


def test_not_json_raises_instead_of_silently_falling_back():
    """模型输出不是 JSON ⇒ 报错停下。

    **绝不能"模型坏了就退回两条硬编码"**：那样演示会在没人察觉的情况下
    退化成确定性版本，而报告里还写着"由 LLM 决策"。
    """
    with pytest.raises(LLMError, match="不是合法 JSON"):
        llm_planner("任务", BUDGET, client=_fake_llm("我觉得买便宜那个就行"))


def test_unknown_resource_is_rejected():
    """模型编了一个清单里没有的接口 ⇒ 拒绝。"""
    with pytest.raises(LLMError, match="清单外的接口"):
        llm_planner("任务", BUDGET, client=_fake_llm(
            {"purchases": [{"resource": "/api/free-lunch"}]}
        ))


def test_empty_plan_is_rejected():
    with pytest.raises(LLMError, match="非空的 purchases"):
        llm_planner("任务", BUDGET, client=_fake_llm({"purchases": []}))


def test_over_budget_plan_is_rejected():
    """模型挑了超预算的组合 ⇒ 拒绝。

    planner 有责任**在自己这一层**守住预算，不能指望后面有人拦。
    """
    with pytest.raises(LLMError, match="超预算"):
        llm_planner("任务", Decimal("0.01"), client=_fake_llm(
            {"purchases": [{"resource": "/api/premium"}]}   # 0.05 > 0.01
        ))


def test_top_level_not_object_is_rejected():
    with pytest.raises(LLMError, match="顶层不是对象"):
        parse_plan_json('["/api/cheap"]', BUDGET)


# ── 最关键的一条：prompt 里不许出现单笔上限 ────────────────────────────


def test_prompt_does_not_leak_the_per_transaction_cap():
    """**prompt 里绝不能出现单笔上限。**

    如果把"单笔 ≤ 0.01"告诉模型，它就会自己绕开那笔不买，
    "边界生效"这一幕永远不会发生 —— 演示也就失去了意义。
    模型只该知道**总预算**，然后自己算出"要深度结论就得花 0.05"。
    """
    prompt = build_prompt("任务", BUDGET)

    assert "0.01" not in prompt, "单笔上限泄漏进 prompt 了，Agent 会自己绕开，演示就假了"
    assert "单笔" not in prompt and "上限" not in prompt
    assert "0.1" in prompt, "总预算必须告诉模型，否则它没法规划"


def test_catalog_prices_are_the_real_ones():
    """清单里的价格必须和收款方实际收的一致 —— 否则 planner 算的预算全是假的。"""
    prices = {item["resource"]: item["price_usd"] for item in API_CATALOG}
    assert prices == {"/api/cheap": "0.005", "/api/premium": "0.05"}


def test_catalog_matches_the_payee_routes():
    """清单和收款方的真实路由必须一一对应（少一个多一个都是漂移）。"""
    from pactpay.payee.app import REPORTS

    # 收款方那边的键带方法前缀（"GET /api/cheap"），planner 侧只用路径
    payee_paths = sorted(key.split(" ", 1)[1] for key in REPORTS)
    catalog_paths = sorted(item["resource"] for item in API_CATALOG)

    assert catalog_paths == payee_paths, (
        f"planner 能买的接口和收款方实际提供的对不上：{catalog_paths} vs {payee_paths}"
    )


# ── 重规划：Agent 自己要核对计划是否满足任务 ────────────────────────────


def test_incomplete_plan_triggers_one_replan():
    """模型第一次只挑了 premium（实测真的发生过）⇒ 带缺口重规划一次。

    这不是"演示需要两份"，是**任务需要**：一份只有结论没有原始数据打底、
    或只有数据没有结论的报告，任务就没完成。
    """
    calls: list[str] = []
    replies = [
        '{"purchases": [{"resource": "/api/premium", "reason": "它更符合任务目标"}]}',
        '{"purchases": [{"resource": "/api/cheap"}, {"resource": "/api/premium"}]}',
    ]

    def client(prompt: str) -> str:
        calls.append(prompt)
        return replies[len(calls) - 1]

    plan = llm_planner("做一份数据分析报告", BUDGET, client=client)

    assert len(calls) == 2, "应该重规划一次"
    assert [p.resource for p in plan] == ["/api/cheap", "/api/premium"]
    assert "缺少这些必需内容" in calls[1] and "原始运营数据" in calls[1]


def test_replan_feedback_does_not_mention_the_pact_cap():
    """重规划的反馈里**也不能**出现单笔上限。

    否则等于在教模型避开那笔超限采购 —— 边界被触发的那一幕就成了我们安排的。
    """
    from pactpay.llm_planner import build_replan_prompt

    prompt = build_replan_prompt("任务", BUDGET, ("原始运营数据",))

    assert "0.01" not in prompt
    assert "单笔" not in prompt and "上限" not in prompt
    assert "原始运营数据" in prompt


def test_still_incomplete_after_replan_raises():
    """重规划后还是不全 ⇒ 报错停下，不交残缺报告。"""
    def client(prompt: str) -> str:
        return '{"purchases": [{"resource": "/api/premium"}]}'

    with pytest.raises(LLMError, match="仍然缺少必需内容"):
        llm_planner("做一份数据分析报告", BUDGET, client=client)


def test_a_complete_first_plan_does_not_replan():
    """第一次就够了 ⇒ 不许多调一次模型（省一次调用，也让行为可预期）。"""
    calls: list[str] = []

    def client(prompt: str) -> str:
        calls.append(prompt)
        return '{"purchases": [{"resource": "/api/cheap"}, {"resource": "/api/premium"}]}'

    llm_planner("做一份数据分析报告", BUDGET, client=client)

    assert len(calls) == 1


def test_deliverables_point_at_real_catalog_entries():
    """交付要点指向的接口必须在清单里 —— 否则重规划永远不可能成功。"""
    from pactpay.llm_planner import DELIVERABLES

    catalog = {item["resource"] for item in API_CATALOG}
    for label, resource in DELIVERABLES:
        assert resource in catalog, f"{label} 指向的 {resource} 不在可采购清单里"
