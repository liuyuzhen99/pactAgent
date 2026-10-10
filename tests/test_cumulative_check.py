"""A5b：累计总预算（R2）真的会被执行吗？——用独立第二份 Pact 验证。

**为什么不能用 demo 那份 Pact 验**：单笔上限 0.01 + 总额 0.1 ⇒ 要 **≥11 笔**成功付款
才会触发累计拒绝，与"恰好两笔"的演示冲突。所以用第二份 Pact，把窗口阈值设成**可达的值**，
验的是**同一条代码路径**；并如实写明 0.1 那一档没有被直接打爆（算术够不着）。

**为什么断言要带"区分性证据"**：per-tx 拒绝与累计拒绝在载荷上**不可区分**——
`reason` 都是通用的 `matched_pact_transfer_deny_if`，`details` 里只有
`policy_id`/`tier`/`policy_type`，**没有规则标识、也没有 limit/requested 数值**。
所以如果第二份 Pact 同时有 per-tx 与 cumulative 两条规则、探针金额又超了 per-tx 上限，
**per-tx 拒绝就会冒充累计拒绝并通过**。
"""

from __future__ import annotations

from decimal import Decimal

_COUNTER_DENIAL = {
    "code": "COUNTER_LIMIT_EXCEEDED",
    "reason": "counter_limit_exceeded",
    "details": {"window": "daily", "metric": "value", "limit_value": "0.015",
                "current_total": "0.015", "increment": "0.005"},
}

from pactpay.cumulative_check import (
    build_cumulative_pact_spec,
    check_cumulative_denial,
)

THRESHOLD = Decimal("0.015")


def test_cumulative_pact_has_only_the_rolling_rule():
    """③ 第二份 Pact **只放 rolling 规则、不放 per-tx 规则**——拒绝才只可能来自累计。

    另外 `completion_conditions` **只能有 `time_elapsed`**：若照抄 `amount_spent`，
    累计到阈值时 Pact 会**先 COMPLETED**，下一笔报的是"已结束"而不是累计拒绝 ⇒ 假失败。
    """
    spec = build_cumulative_pact_spec("0.015")

    assert len(spec["policies"]) == 1
    rules = spec["policies"][0]["rules"]
    assert "amount_gt" not in rules["deny_if"], "不得有单笔上限，否则 per-tx 拒绝会冒充累计拒绝"
    assert rules["deny_if"]["usage_limits"]["rolling_24h"]["amount_gt"] == "0.015"
    assert [c["type"] for c in spec["completion_conditions"]] == ["time_elapsed"]


def test_denial_happens_after_the_first_payment():
    """① 拒绝必须发生在第 N 笔且 N > 1。

    per-tx 规则**绝不可能**拒绝第 1 笔之后的小额——所以这是最本质的判别，
    且不依赖阈值语义是 `>` 还是 `>=`。
    """
    attempts = [
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "denied", "denial": _COUNTER_DENIAL},
    ]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["ok"] is True
    assert result["denied_at"] == 3  # 0-based，第 4 笔


def test_a_first_payment_denial_is_rejected_as_per_tx_masquerade():
    """第 1 笔就被拒 ⇒ 一定是别的机制干的（per-tx 或白名单），累计拒绝不可能这样。"""
    attempts = [
        {"amount": Decimal("0.005"), "outcome": "denied", "denial": _COUNTER_DENIAL},
    ]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["ok"] is False
    assert "第 1 笔" in result["why"] or "N>1" in result["why"]


def test_sum_before_denial_counts_only_the_allowed_ones():
    """② 的 `sum_before_denial` = **被拒那笔之前**已放行的金额之和。

    **这条原先是对测试自己的 fixture 做算术**（`assert 0.015 >= 0.015 - 0.01`，恒真），
    与被测函数无关，删掉函数体也照样绿（评审列为"同义反复"）。
    现在改成真断言：把前几笔的金额**换掉**，函数输出的和必须跟着变；
    再把被拒那笔之后的一笔加上去，和**不许**变 —— 这才测到了"之前"这个语义。
    """
    attempts = [
        {"amount": Decimal("0.007"), "outcome": "paid"},
        {"amount": Decimal("0.004"), "outcome": "paid"},
        {"amount": Decimal("0.002"), "outcome": "denied", "denial": _COUNTER_DENIAL},
    ]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["sum_before_denial"] == Decimal("0.011"), "和必须等于前两笔之和"


def test_sum_before_denial_stops_at_the_denial():
    """被拒那笔**之后**的放行金额不许计入 —— 否则"之前"这个词就没有意义了。"""
    attempts = [
        {"amount": Decimal("0.007"), "outcome": "paid"},
        {"amount": Decimal("0.003"), "outcome": "denied", "denial": _COUNTER_DENIAL},
        {"amount": Decimal("0.005"), "outcome": "paid"},
    ]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["sum_before_denial"] == Decimal("0.007")


def test_rejects_when_nothing_was_ever_denied():
    """全放行 ⇒ 累计上限根本没生效，必须判失败。"""
    attempts = [{"amount": Decimal("0.005"), "outcome": "paid"} for _ in range(6)]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["ok"] is False


# ---- 以下用**实测到的强证据**（counter 读数）做负例 ----

def _denied(**over):
    details = {"window": "daily", "metric": "value", "limit_value": "0.015",
               "current_total": "0.015", "increment": "0.005"}
    details.update(over.pop("details", {}))
    return {"code": over.pop("code", "COUNTER_LIMIT_EXCEEDED"),
            "reason": "counter_limit_exceeded", "details": details}


def test_rejects_a_per_tx_denial_masquerading_as_cumulative():
    """per-tx 拒绝冒充累计拒绝 ⇒ 必须被识破。

    方案原以为两者载荷不可区分、只能靠"N>1"猜；**实测推翻了这点**：
    累计拒绝的 code 是 `COUNTER_LIMIT_EXCEEDED`，per-tx 的是 `TRANSFER_LIMIT_EXCEEDED`。
    """
    attempts = [
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "denied",
         "denial": _denied(code="TRANSFER_LIMIT_EXCEEDED")},
    ]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["ok"] is False
    assert "COUNTER_LIMIT_EXCEEDED" in result["why"]


def test_rejects_when_the_counter_arithmetic_does_not_add_up():
    """读数自相矛盾（current_total + increment 并没超过 limit_value）⇒ 不能算通过。

    **这个用例要精确隔离"验算"这一条**：只花了 0.005（1 笔）就被拒，
    此时 current_total == 本轮累计、limit_value == 配置阈值，**只有验算这一条不成立**。
    否则会被前面某一条检查先拦下，测试就"因为错的原因通过"了
    ——变异测试正是靠这一点发现原来的用例是假的。
    """
    attempts = [
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "denied",
         "denial": _denied(details={"current_total": "0.005"})},
    ]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["ok"] is False
    assert "验算" in result["why"]


def test_rejects_when_limit_value_is_not_the_configured_threshold():
    """读数里的 limit_value 与本次配置的阈值不符 ⇒ 说明验的不是这份 Pact。

    **精确隔离**：`limit_value=0.018` 时验算仍成立（0.015+0.005 > 0.018）、
    current_total 也与本轮累计一致，**只有"等于配置阈值"这一条不成立**。
    """
    attempts = [
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "denied",
         "denial": _denied(details={"limit_value": "0.018"})},
    ]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["ok"] is False
    assert "limit_value" in result["why"]


def test_rejects_when_current_total_disagrees_with_the_converted_sum():
    """读数里的 current_total 与本轮逐笔累计不一致 ⇒ 不能算通过。"""
    attempts = [
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "paid"},
        {"amount": Decimal("0.005"), "outcome": "denied",
         "denial": _denied(details={"current_total": "0.014", "increment": "0.005"})},
    ]

    result = check_cumulative_denial(attempts, THRESHOLD)

    assert result["ok"] is False


# ---------------------------------------------------------------------------
# G7（第 5 轮评审，重大）：`make cumulative-check` 在累计上限**从未触发**时
# 仍能打出"✓ A5b 成立"并 exit 0 —— 把任意一处 `failed |= ...` 改成
# `failed |= False` 即可，292 条测试全绿。
#
# 屏幕上的结论必须从**一组被观察到的输入**算出来，而不是靠沿途 `|=` 的副作用。
# 所以判据抽成纯函数 `judge_a5b`，在这里被行为地钉住。
# ---------------------------------------------------------------------------

def test_verdict_fails_when_the_cumulative_cap_never_fired():
    """**G7 的核心回归测试**：累计上限没触发 ⇒ 结论必须不成立。

    这正是"边界是硬性的"这个主张的反面 —— 如果累计上限根本没触发过，
    这个检查就什么都没证明，绝不能打 ✓。
    """
    from pactpay.cumulative_check import judge_a5b

    ok, problems = judge_a5b(
        policies_count=1,
        verdict_ok=False,                      # ← 累计从未被拒
        verdict_why="打满 8 笔都没被拒 ⇒ 看不到累计上限生效",
        fresh_first_outcome="paid",
    )

    assert ok is False
    assert any("累计判别未成立" in p for p in problems)


def test_verdict_fails_when_the_counter_is_wallet_scoped():
    """新建 Pact 的第一笔就被拒 ⇒ 计数器挂在钱包上，A5b 不成立。"""
    from pactpay.cumulative_check import judge_a5b

    ok, problems = judge_a5b(
        policies_count=1, verdict_ok=True, verdict_why="ok",
        fresh_first_outcome="denied",
    )

    assert ok is False
    assert any("钱包作用域" in p for p in problems)


def test_verdict_fails_when_there_are_extra_policies():
    """策略数组不止 1 条 ⇒ 拒绝可能来自别的规则，"不是 per-tx 冒充"就不成立。"""
    from pactpay.cumulative_check import judge_a5b

    ok, problems = judge_a5b(
        policies_count=3, verdict_ok=True, verdict_why="ok", fresh_first_outcome="paid",
    )

    assert ok is False
    assert any("只有 1 条" in p for p in problems)


def test_verdict_passes_only_when_every_condition_holds():
    """**阳性对照**：三条都成立才 ✓。没有它，上面几条可能只是"永远 False"。"""
    from pactpay.cumulative_check import judge_a5b

    ok, problems = judge_a5b(
        policies_count=1, verdict_ok=True, verdict_why="ok", fresh_first_outcome="paid",
    )

    assert ok is True, problems
    assert problems == []
