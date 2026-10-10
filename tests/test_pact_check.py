"""A1：提交的 Pact 与**服务端回读**的必须逐字段一致（R1）。

**为什么要比对而不是"提交成功就算"**：提交接口返回 200 只说明它收下了，
不说明它**按我们写的那样存下来了**。政策被服务端"归一化"掉一条字段，
agent 的授权范围就与人类设定的不一样了——而这一点**在演示里看不出来**，
因为两笔采购照样一过一拒，页面一片正常。

**为什么是"子集比对"而不是严格相等**：服务端会**补充**字段（实测策略对象里多了
`priority` / `is_active`）。要求严格相等会假失败；只要求"我们提交的每个字段都原样回读"
才是真正要验的东西。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

from pactpay.pact_check import compare_spec

SUBMITTED = {
    "policies": [
        {
            "name": "demo-transfer",
            "type": "transfer",
            "rules": {
                "effect": "allow",
                "when": {"chain_in": ["TBASE_SETH"],
                         "token_in": [{"chain_id": "TBASE_SETH", "token_id": "TBASE_USDC"}],
                         "destination_address_in": ["0xabc0000000000000000000000000000000000001"]},
                "deny_if": {"amount_gt": "0.01"},
            },
        }
    ],
    "completion_conditions": [{"type": "time_elapsed", "threshold": "86400"}],
}

# 服务端回读：多加了 priority / is_active（实测如此），其余一致
READBACK_OK = {
    "policies": [
        {
            "name": "demo-transfer",
            "type": "transfer",
            "rules": {
                "effect": "allow",
                "when": {"chain_in": ["TBASE_SETH"],
                         "token_in": [{"chain_id": "TBASE_SETH", "token_id": "TBASE_USDC"}],
                         "destination_address_in": ["0xabc0000000000000000000000000000000000001"]},
                "deny_if": {"amount_gt": "0.01"},
            },
            "priority": 0,
            "is_active": True,
        }
    ],
    "completion_conditions": [{"type": "time_elapsed", "threshold": "86400"}],
}


def test_identical_spec_passes_even_though_the_server_added_fields():
    result = compare_spec(SUBMITTED, READBACK_OK)

    assert result["ok"] is True
    assert result["differences"] == []


def test_a_changed_spend_limit_is_caught():
    """**最关键的一条**：单笔上限被服务端改掉 ⇒ 必须报出来。

    这是最危险的静默失败——限额被放大，agent 的授权范围就超出了人类设定的边界，
    而演示照样"一过一拒"，页面上看不出任何异常。
    """
    readback = {
        **READBACK_OK,
        "policies": [{**READBACK_OK["policies"][0],
                      "rules": {**READBACK_OK["policies"][0]["rules"],
                                "deny_if": {"amount_gt": "1.00"}}}],
    }

    result = compare_spec(SUBMITTED, readback)

    assert result["ok"] is False
    assert any("deny_if" in d for d in result["differences"])


def test_a_dropped_allowlist_entry_is_caught():
    """白名单被丢掉一项 ⇒ 也必须报出来（授权范围变了）。"""
    readback = {
        **READBACK_OK,
        "policies": [{**READBACK_OK["policies"][0],
                      "rules": {**READBACK_OK["policies"][0]["rules"],
                                "when": {"chain_in": ["TBASE_SETH"],
                                         "token_in": [{"chain_id": "TBASE_SETH", "token_id": "TBASE_USDC"}],
                                         "destination_address_in": []}}}],
    }

    result = compare_spec(SUBMITTED, readback)

    assert result["ok"] is False


def test_a_dropped_completion_condition_is_caught():
    """完成条件被丢掉 ⇒ 授权不会自动收回，同样必须报出来。"""
    result = compare_spec(SUBMITTED, {**READBACK_OK, "completion_conditions": []})

    assert result["ok"] is False
    assert any("completion_conditions" in d for d in result["differences"])


def test_a_missing_policy_is_caught():
    result = compare_spec(SUBMITTED, {**READBACK_OK, "policies": []})

    assert result["ok"] is False


def test_extra_server_side_policies_are_reported_too():
    """服务端**多塞**一条策略也要报——多出来的授权同样是范围被改了。"""
    readback = {
        **READBACK_OK,
        "policies": READBACK_OK["policies"] + [{"name": "unexpected", "type": "transfer",
                                                 "rules": {"effect": "allow"}}],
    }

    result = compare_spec(SUBMITTED, readback)

    assert result["ok"] is False
    assert any("多" in d or "unexpected" in d for d in result["differences"])


def test_a_wholly_missing_key_is_caught():
    """**精确隔离"键丢失"这一条**：整个 `deny_if` 键从回读里消失（不是变成空值）。

    切成空值会被"条数不同/值不同"先拦住，测不到"键没了"这个分支。
    """
    policy = dict(READBACK_OK["policies"][0])
    policy["rules"] = {k: v for k, v in policy["rules"].items() if k != "deny_if"}

    result = compare_spec(SUBMITTED, {**READBACK_OK, "policies": [policy]})

    assert result["ok"] is False
    assert any("丢失" in d for d in result["differences"])


# ---- A1 第三个断言：必须是 pact-scoped key ----

from pactpay.pact_check import pact_key_is_scoped  # noqa: E402


def test_scoped_key_must_differ_from_the_root_key():
    assert pact_key_is_scoped("pact-key-abc", "root-key-xyz") is True


def test_using_the_root_key_is_not_accepted():
    """用根 key 执行 ⇒ 策略挂在"默认 Pact"上，限额与白名单根本不是我们设的那套。"""
    assert pact_key_is_scoped("root-key-xyz", "root-key-xyz") is False


def test_a_missing_key_is_not_accepted():
    assert pact_key_is_scoped(None, "root-key-xyz") is False
    assert pact_key_is_scoped("", "root-key-xyz") is False


# ---- B4（第 7 步评审阻断项）：A1 必须断言 Pact **真的生效**（status==active）----
#
# 评审的反证：让假服务端回读 `status="pending_approval"`、spec 原样不变，
# `make pact` 照样打印「✓ A1 成立」并且**退出码 0**。
# 也就是说"规则挂在了一个还没生效的 Pact 上"，而演示一切正常 —— 典型静默失败。
# 触发条件真实存在：agent 一旦被配对（pairing），Pact 就不再自动 ACTIVE，而是等人审批。
from pactpay.pact_check import judge_readback  # noqa: E402

B4_SUBMITTED = {
    "policies": [{"name": "p", "type": "transfer", "rules": {"effect": "allow", "when": {"chain_in": ["TBASE_SETH"]}}}],
    "completion_conditions": [{"type": "time_elapsed", "threshold": "86400"}],
}
B4_ROOT_KEY = "caw_root_key_aaaa"


def _detail(**over):
    base = {"status": "active", "spec": B4_SUBMITTED, "api_key": "caw_pact_scoped_bbbb"}
    base.update(over)
    return base


def test_active_pact_passes():
    verdict = judge_readback(B4_SUBMITTED, _detail(), B4_ROOT_KEY)

    assert verdict["ok"], verdict["failures"]


def test_pending_approval_pact_fails_even_though_the_spec_matches():
    """**核心**：spec 一模一样，但 Pact 还没生效 ⇒ 必须判失败。

    这正是评审证明过的逃逸路径：只看"规则存没存下来"会漏掉"规则到底生没生效"。
    """
    verdict = judge_readback(B4_SUBMITTED, _detail(status="pending_approval"), B4_ROOT_KEY)

    assert not verdict["ok"]
    assert any("status" in f for f in verdict["failures"])


def test_non_active_statuses_all_fail():
    """终态与未生效态一律不许算通过（除 active 外全是"规则不在生效中"）。"""
    for status in ("pending_approval", "rejected", "completed", "expired", "revoked", "withdrawn"):
        verdict = judge_readback(B4_SUBMITTED, _detail(status=status), B4_ROOT_KEY)
        assert not verdict["ok"], f"status={status} 竟然判过了"


def test_spec_difference_still_fails():
    """原有能力不能被这次改动弄丢：spec 有差异仍要判失败。"""
    verdict = judge_readback(B4_SUBMITTED, _detail(spec={**B4_SUBMITTED, "policies": []}), B4_ROOT_KEY)

    assert not verdict["ok"]


def test_root_key_reused_as_pact_key_fails():
    """A1 还要求执行用的是 pact-scoped key；用根 key 要判失败。"""
    verdict = judge_readback(B4_SUBMITTED, _detail(api_key=B4_ROOT_KEY), B4_ROOT_KEY)

    assert not verdict["ok"]
    assert any("key" in f.lower() or "key" in f for f in verdict["failures"])


def test_failures_are_specific():
    """失败原因要指得出是哪一项，不能只说"没过"。"""
    verdict = judge_readback(B4_SUBMITTED, _detail(status="pending_approval", api_key=B4_ROOT_KEY), B4_ROOT_KEY)

    assert len(verdict["failures"]) >= 2


# ---------------------------------------------------------------------------
# 第 4 轮**自查变异**发现的洞：A1 的三条判据里，`active` **只印不判**。
#
#   把 `active = detail.get("status") == ACTIVE_STATUS` 改成 `active = True`
#   → 281 条测试全绿；退出码依然是 0。
#
# 我第一版"修复"写的是结构断言（读源码找 `failed |= not active` 这一行）——
# **那还是"检查形状"**：把 `active` 的定义改成 `True`，那一行照样在，测试照样绿。
# 所以这里改成**调函数看返回值**：把结论聚合抽成 `judge_a1` 纯函数。
# ---------------------------------------------------------------------------

def test_a_non_active_pact_makes_the_verdict_fail():
    """Pact 不是 `active` ⇒ A1 结论必须是**失败**，不只是打印一个 ✗。

    Pact 不在 `active` 时，限额/白名单**根本没生效** —— 后面每一句"被拒"
    都可能只是预算到期，而不是边界起作用。整个 A1 结论都不成立。
    """
    from pactpay.pact_check import judge_a1

    verdict = judge_a1(active=False, readback_ok=True, scoped=True)

    assert verdict["ok"] is False, (
        "Pact 不在 active，A1 却给出了通过 —— `active` 没有参与判定（B4 的未修完形态）"
    )
    assert any("active" in r.lower() or "生效" in r for r in verdict["reasons"]), (
        f"失败原因没有指向'规则没生效'：{verdict['reasons']}"
    )


def test_an_unscoped_key_makes_the_verdict_fail():
    """执行用的钥匙必须**被 Pact 约束住**，否则限额是写在纸上的。"""
    from pactpay.pact_check import judge_a1

    verdict = judge_a1(active=True, readback_ok=True, scoped=False)

    assert verdict["ok"] is False
    assert any("key" in r or "钥匙" in r for r in verdict["reasons"])


def test_a_readback_mismatch_makes_the_verdict_fail():
    """回读不一致（策略被服务端改过）⇒ 必须失败。"""
    from pactpay.pact_check import judge_a1

    verdict = judge_a1(active=True, readback_ok=False, scoped=True)

    assert verdict["ok"] is False
    assert any("回读" in r for r in verdict["reasons"])


def test_a1_passes_only_when_all_three_hold():
    """**阳性对照**：三条都成立时才通过。

    没有这一条，上面三条可能只是"永远返回失败"。
    """
    from pactpay.pact_check import judge_a1

    verdict = judge_a1(active=True, readback_ok=True, scoped=True)

    assert verdict["ok"] is True, f"三条判据都成立却给出了失败：{verdict['reasons']}"
    assert verdict["reasons"] == []


def test_main_actually_routes_the_verdict_into_its_exit_code():
    """`main()` 必须把 `judge_a1` 的结论接进 `failed`（接线，不只是函数对）。

    评审 F1 的教训：**函数改对了、调用处没改**照样全绿。
    """
    src = (REPO / "pactpay" / "pact_check.py").read_text()
    tree = ast.parse(src)
    main_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    joined = ast.unparse(main_fn)

    assert "judge_a1(" in joined, "main() 没有调用 judge_a1 —— 判定逻辑没接线"
    assert "failed |= not verdict_a1" in joined or "failed = not verdict_a1" in joined, (
        "judge_a1 的结论没有接进 failed —— 算了不用，等于没判"
    )
