"""守卫 `plan` 立下的硬规则：**deny 阈值必须严格小于 complete 阈值**。

**为什么要有这个测试**（B2，第 7 步评审判 FAIL 的阻断项之一）：

`amount_spent` 是**完成条件**（"累计花到这个数就自动终止"），不是拒绝规则。
如果让它和 `rolling_24h` 的拒绝阈值**取同一个数**，就会出现这种时序：

1. 累计恰好爬到阈值；
2. **完成条件先命中** → Pact 进入终态 `COMPLETED`；
3. 本该被判"累计超限"的那一笔，拿到的是**"Pact 已结束"**而不是 `TRANSFER_LIMIT_EXCEEDED`。

于是演示要证明的"累计硬边界"**变成了另一回事**，而且**不可稳定复现**（取决于先命中哪条）。
这个形态在账本里被命名为 R-x10 并明令禁止过，但 `build_demo_pact_spec` **一个测试都没有**，
所以它一路带过了 4 轮修复和 47 个变异自检。

这里把那条规则固化成**可执行的不变量**，而不是只写在文档里。
"""

from __future__ import annotations

from pactpay.demo import ROLLING_24H_CAP, build_demo_pact_spec

PAY_TO = "0x757868baab9ab42a20d0ed2d8ffcff9c6df82ea7"

# 与"钱"有关的完成条件类型：它们的阈值和 deny 的额度阈值是**同一个量纲**，可比较。
SPEND_COMPLETIONS = {"amount_spent", "amount_spent_usd"}



# ---- B2 的守卫重写：从"空转的 for 循环"变成"有阳性对照的检查"（评审第 2 轮）----

from pactpay.pact_spec import check_deny_strictly_below_spend_completions  # noqa: E402


def _spec_with(completion_type: str, threshold: str, deny_cap: str = ROLLING_24H_CAP) -> dict:
    """造一个只改了完成条件的 demo spec，用来做阳性/阴性对照。"""
    spec = build_demo_pact_spec(PAY_TO)
    spec["deny_if"] = {"usage_limits": {"rolling_24h": {"amount_gt": deny_cap}}}
    spec["completion_conditions"] = [{"type": completion_type, "threshold": threshold}]
    return spec


def test_a_spend_completion_at_the_deny_cap_is_rejected():
    """**阳性对照**：取等号必须被抓住。

    没有这一条，前面"demo spec 没问题"的结论毫无意义 —— 原守卫是个空转的 for 循环，
    它绿着但抓不住任何东西。**一个检查必须先证明它抓得住违规。**
    """
    result = check_deny_strictly_below_spend_completions(_spec_with("amount_spent", ROLLING_24H_CAP))

    assert result["ok"] is False, "取等号竟然通过了 —— 这正是 R-x10"
    assert result["spend_completions_checked"] == 1, "阳性对照必须真的被检查到（不是空转）"
    assert any("R-x10" in p for p in result["problems"])


def test_a_spend_completion_below_the_deny_cap_is_rejected_too():
    """比拒绝阈值**更低**同样不行：完成条件会先命中。"""
    result = check_deny_strictly_below_spend_completions(_spec_with("amount_spent", "0.05"))

    assert result["ok"] is False


def test_a_spend_completion_above_the_deny_cap_passes():
    """严格大于才通过 —— 这是允许的形态。"""
    result = check_deny_strictly_below_spend_completions(_spec_with("amount_spent", "0.2"))

    assert result["ok"] is True
    assert result["spend_completions_checked"] == 1


def test_usd_denominated_completion_is_checked_too():
    """`amount_spent_usd` 与 `amount_spent` 是同一条线上的两种写法，都要查。"""
    result = check_deny_strictly_below_spend_completions(_spec_with("amount_spent_usd", ROLLING_24H_CAP))

    assert result["ok"] is False


def test_time_elapsed_alone_is_fine_but_is_reported_as_zero_checked():
    """demo 现在的形态（只有 `time_elapsed`）应通过，**但必须自报"没查到额度条件"**。

    这样"通过"与"根本没东西可查"就不会混为一谈 —— 后者是上一版守卫的形态。
    """
    result = check_deny_strictly_below_spend_completions(build_demo_pact_spec(PAY_TO))

    assert result["ok"] is True
    assert result["spend_completions_checked"] == 0


def test_the_real_demo_spec_has_no_colliding_spend_completion():
    """对**真实 spec** 的断言。关键在于它同时钉住了 `spend_completions_checked`：
    如果将来有人给 demo spec 加回额度完成条件，这条会立刻要求它别撞上拒绝阈值。"""
    spec = build_demo_pact_spec(PAY_TO)
    result = check_deny_strictly_below_spend_completions(spec)

    assert result["ok"] is True, f"demo spec 的额度完成条件撞上了拒绝阈值：{result['problems']}"
    # 当前形态：只有时间条件。这是**有意的**（见 R-x10 与账本里对 R10 的说明）。
    assert result["spend_completions_checked"] == 0
    assert {c["type"] for c in spec["completion_conditions"]} == {"time_elapsed"}
