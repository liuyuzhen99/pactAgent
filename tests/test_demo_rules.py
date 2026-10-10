"""把**人类设定的规则**与**证据来源的措辞**钉住。

第 4 轮评审的 F5/F6 是同一类问题：**产品里最该被守住的东西，恰恰是裸常量**。
评审把 `PER_TX_CAP` 从 `0.01` 改成 `1`（宽松 100 倍 —— README 里说必须被拒的
0.05 那笔会被**付掉**），把 `EXPECTED_SPEND` 改成 `0`，删掉 `require_balance`，
禁掉默认拒绝守卫，把审计佐证打桩 —— **254 条测试全绿**。

这个项目的全部主张都建立在这些常量上。它们不是"实现细节"，是**被测对象**：
"单笔 ≤ 0.01" 就是用户写下的硬边界本身。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "pactpay" / "demo.py"


@pytest.fixture(scope="module")
def demo_src() -> str:
    return DEMO.read_text()


@pytest.fixture(scope="module")
def demo_tree(demo_src: str) -> ast.Module:
    return ast.parse(demo_src, filename=str(DEMO))


def _has_constant_false(test: ast.AST) -> bool:
    """`if False and X:` 这类常量折叠出来的死分支。"""
    for sub in ast.walk(test):
        if isinstance(sub, ast.BoolOp) and any(
            isinstance(v, ast.Constant) for v in sub.values
        ):
            return True
        if isinstance(sub, ast.IfExp) and isinstance(sub.test, ast.Constant):
            return True
    return False


def _module_constant(tree: ast.Module, name: str):
    """取模块级 `NAME = <字面量>` 的值。"""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"模块级常量 {name} 找不到")


# ---------------------------------------------------------------------------
# F6：人类写下的硬边界
# ---------------------------------------------------------------------------

def test_per_tx_cap_is_the_story_number(demo_tree: ast.Module):
    """**单笔上限必须就是故事里那个数**（0.01 USDC）。

    这是整个演示的**被测对象**：用户说"单笔最多 0.01"，Agent 想买 0.05 的那笔
    必须被拒。把 `PER_TX_CAP` 调大，演示会**买下那笔本该被拒的采购**，
    而屏幕上照样打 ✅ —— 一次"边界不存在"的运行看起来像成功。

    评审实测：`"0.01"` → `"1"`，254 条测试全绿。
    """
    assert _module_constant(demo_tree, "PER_TX_CAP") == "0.01", (
        "单笔上限不再是 0.01 USDC —— 这改了演示要证明的那个边界本身"
    )


def test_the_budget_is_the_story_number():
    """总预算必须就是故事里那个数（0.1 USDC）。"""
    from decimal import Decimal

    from pactpay.demo import BUDGET_USD

    assert BUDGET_USD == Decimal("0.1")


def test_the_expensive_purchase_is_actually_over_the_cap(demo_tree: ast.Module):
    """**被拒的那笔必须真的超限** —— 这是"边界是硬的"的前提。

    如果采购计划里的金额被改小到上限以内，它就**不该**被拒；
    反过来，如果上限被改大，它就不会被拒。两边必须对得上，
    所以这里检查的是**关系**：至少有一笔计划金额 > 单笔上限。
    """
    from pactpay.demo import BUDGET_USD, PER_TX_CAP, TASK
    from pactpay.planner import make_plan

    plan = make_plan(TASK, BUDGET_USD)
    cap = float(PER_TX_CAP)

    over = [p for p in plan.purchases if float(p.amount_usd) > cap]
    under = [p for p in plan.purchases if float(p.amount_usd) <= cap]

    assert over, "没有任何一笔采购超过单笔上限 —— '超限被拒'就没有被测对象了"
    assert under, "没有任何一笔采购在上限之内 —— '允许支付'就没有被测对象了"


def test_expected_spend_is_the_cheap_purchase():
    """前置检查的预期花费必须等于"实际会花掉的那笔"。

    评审实测：`EXPECTED_SPEND` 改成 `0`，254 条测试全绿 ——
    于是"余额够不够"这个前置检查变成了一句空话。
    """
    from decimal import Decimal

    from pactpay.demo import BUDGET_USD, PER_TX_CAP, TASK, planned_spend
    from pactpay.planner import make_plan

    affordable = [
        p for p in make_plan(TASK, BUDGET_USD).purchases if float(p.amount_usd) <= float(PER_TX_CAP)
    ]
    assert affordable, "计划里没有负担得起的那笔"
    expected_sum = sum((p.amount_usd for p in affordable), Decimal("0"))
    # G6：预计花费**从计划求和得出**，不再是一个可被清零的裸常量。
    assert planned_spend() == expected_sum, (
        f"planned_spend()={planned_spend()} 与'计划里真正会花掉的钱' {expected_sum} 对不上"
    )
    assert planned_spend() > 0, "预计花费是 0 —— 余额前置检查变成了空话"


def test_balance_precondition_is_actually_called(demo_src: str):
    """**前置检查必须真的被调用**（评审 N15：删掉它 254 条全绿）。

    没有它，余额不足时会跑到一半才失败，留下一份**残缺证据**——
    而残缺证据看起来和"什么都没发生"一模一样。
    """
    assert "require_balance(" in demo_src, "余额前置检查没有被调用"


def test_the_default_deny_guard_is_actually_enforced(demo_src: str):
    """**R6 的默认拒绝守卫必须真的会 raise**（评审 N04：改成 `if False:` 全绿）。

    这条守的是"fail-closed 真的生效"：如果默认拒绝那笔**没有**被拒，
    演示必须中止，而不是把它当成一次普通结果继续。
    """
    assert 'if default_deny["outcome"] != "denied":' in demo_src, (
        "默认拒绝的守卫不见了 —— fail-closed 没有被验证"
    )
    for node in ast.walk(ast.parse(demo_src)):
        if not isinstance(node, ast.If):
            continue
        if "default_deny" in ast.unparse(node.test) and "denied" in ast.unparse(node.test):
            assert not _has_constant_false(node.test), (
                "默认拒绝守卫被常量折叠成了死分支（`if False and ...`）"
            )
            assert any(isinstance(s, ast.Raise) for s in node.body), (
                "默认拒绝守卫里没有 raise —— 它不会真的中止"
            )
            return
    raise AssertionError("找不到默认拒绝守卫")


def test_audit_corroboration_is_not_stubbed(demo_src: str):
    """**审计佐证必须来自真实查询**（评审 N06：打桩成 `{'ok': True}` 全绿）。

    A5c② 是三条证据里唯一能说"进了 Cobo 审计日志"的那条。
    打桩就等于**伪造审计**——这正是本项目反复强调不做的事。
    """
    assert "wait_for_audit_corroboration(" in demo_src, (
        "审计佐证不是通过 wait_for_audit_corroboration 查出来的"
    )
    # 不许在 demo 里直接把 corroboration 写成一个常量字典
    for node in ast.walk(ast.parse(demo_src)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "corroboration" for t in node.targets
        ):
            assert not isinstance(node.value, ast.Dict), (
                "corroboration 被硬编码成字典了 —— 审计佐证被打桩"
            )


# ---------------------------------------------------------------------------
# F5：证据来源的措辞
# ---------------------------------------------------------------------------

def test_payment_response_is_never_labelled_as_an_audit_log():
    """**付款响应绝不许被说成审计日志**（评审 F5）。

    README 白纸黑字写着"我们不会把付款响应说成审计日志"，而实测确认
    x402 付款的拒绝**不进** Cobo 审计日志（等 2/8/20 秒都没有）。
    评审把 x402 那条的来源标签改成「Cobo 审计日志（不可篡改）」，254 条全绿 ——
    于是一句谎言会印在页面上。
    """
    from pactpay.dashboard.app import SOURCE_LABELS

    payment_label = SOURCE_LABELS["cobo_payment_response"]

    # 判据是"它**自称**是什么"，不是"它有没有提到审计日志"——
    # 正确措辞里本来就该出现"该路径审计日志**不记录**此拒绝"（如实说明）。
    assert payment_label.startswith("Cobo 付款响应"), (
        f"付款响应的标签没有自称'付款响应'：{payment_label!r}"
    )
    assert "不可篡改" not in payment_label, (
        f"付款响应的标签自称'不可篡改'——那是审计日志的说法：{payment_label!r}"
    )
    assert "不记录" in payment_label, (
        f"付款响应的标签没有如实说明'该路径审计日志不记录此拒绝'：{payment_label!r}"
    )


def test_the_x402_denial_is_sourced_from_the_payment_response():
    """x402 那条拒绝的来源必须标成**付款响应**，不是审计日志。"""
    from pactpay.dashboard.app import SOURCE_LABELS, denial_evidence_source

    denial = {"code": "X402_POLICY_DENIED", "policy_reason": "matched_pact_transfer_deny_if"}

    # 审计日志里**没有**这条拒绝（实测 x402 路径就是不进审计日志）
    source = denial_evidence_source(denial, [])

    assert source == "cobo_payment_response", (
        f"x402 被拒那笔的来源被标成了 {source!r} —— 应为 cobo_payment_response"
    )
    assert not SOURCE_LABELS[source].startswith("Cobo 审计日志")


def test_audit_backed_denial_is_labelled_as_audit_log():
    """**阳性对照**：真有审计行的转账拒绝，才该标成审计日志。"""
    from pactpay.dashboard.app import SOURCE_LABELS, denial_evidence_source

    denial = {
        "code": "ADDRESS_NOT_WHITELISTED",
        "policy_reason": "no_pact_transfer_allow_policy_matched",
    }
    audit_logs = [
        {
            "id": 927580,
            "action": "transfer.denied",
            "result": "denied",
            "created_at": "2026-10-12T08:10:44Z",
            "authz_details": {"reason": denial["policy_reason"]},
        }
    ]

    source = denial_evidence_source(denial, audit_logs)

    assert source == "cobo_audit_log", f"有审计行的拒绝被标成了 {source!r}"
    assert "审计日志" in SOURCE_LABELS[source]


def test_the_persisted_counter_always_has_all_three_keys():
    """写进产物的计数器必须**三个键都在**（第 4 轮评审 F10）。

    `counter` 只在某个结果真的出现过时才建键，于是"1 成功 1 被拒"的运行写出来是
    `{"paid": 1, "denied": 1}` —— `failed` **缺席**。看证据的人无法分辨
    "未结算是 0 笔"和"未结算根本没记"，而这两件事的严重程度完全不同
    （前者是正常，后者是证据不完整）。

    提交信息里写着"计数器写全三个键（缺席记 0）"，产物却只有两个 —— 说明和实现对不上。
    """
    from pactpay.evidence import OUTCOMES, count_outcomes

    # 运行时计数器只见过两种结果
    counter = {"paid": 1, "denied": 1}

    # 这是 demo 落盘时用的形状
    persisted = {name: counter.get(name, 0) for name in OUTCOMES}

    assert set(persisted) == set(OUTCOMES), (
        f"落盘的计数器缺键：{set(OUTCOMES) - set(persisted)} —— 看证据的人分不清"
        "「0 笔」和「没记」"
    )
    assert persisted == count_outcomes([{"outcome": "paid"}, {"outcome": "denied"}]), (
        "补零后的计数器与链里数出来的对不上"
    )


def test_demo_zero_fills_the_counter_on_write():
    """`demo.py` 落盘时必须走"补全三个键"的那条路径。

    上一条测的是形状；这一条测**接线**——评审 F1 的教训就是
    "函数改对了，调用处没改"照样能全绿。
    """
    src = DEMO.read_text()

    assert "write_counts(run_dir, {name: counter.get(name, 0) for name in OUTCOMES})" in src, (
        "demo 落盘时没有给计数器补全三个键（F10 复发）"
    )
