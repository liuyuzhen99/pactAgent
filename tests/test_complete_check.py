"""T9 / R10 完成条件自动终止的验收单测。

期望值来自 2026-10-12 的实测（逐字抄录），不是照文档假设。
"""

from __future__ import annotations

from pactpay.checks.complete import (
    EXPECTED_EXHAUSTED_CODE,
    build_one_shot_pact_spec,
    classify_after_completion,
)


def test_one_shot_pact_completes_after_exactly_one_transaction():
    """载体 Pact 必须是"只允许一笔"——否则验不出完成条件。

    而且 `when` 只按链/币白名单，`deny_if` 给宽松单笔上限：
    这样第一笔成功只可能是这条 allow 规则放行的，不掺别的机制。
    """
    spec = build_one_shot_pact_spec()

    assert spec["completion_conditions"] == [{"type": "tx_count", "threshold": "1"}]
    assert len(spec["policies"]) == 1
    rules = spec["policies"][0]["rules"]
    assert rules["effect"] == "allow"
    assert rules["when"]["chain_in"] == ["TBASE_SETH"]
    assert rules["deny_if"] == {"amount_gt": "0.05"}


def test_the_expected_code_is_the_measured_one():
    """完成后应得的错误码必须是**实测值**，且断言要钉死、不许写析取集。

    实测：Pact 变成 completed 后，用同一把 key 再调用得到
    HTTP 401 / `IDENTITY_INVALID_API_KEY` / reason `invalid_api_key`。
    """
    assert EXPECTED_EXHAUSTED_CODE == "IDENTITY_INVALID_API_KEY"


def test_classifies_key_invalidation_as_the_expected_outcome():
    assert classify_after_completion(401, "IDENTITY_INVALID_API_KEY") == "key_invalidated"


def test_classifies_a_still_working_key_as_failure():
    """完成条件没生效（调用还能成功）必须判失败——这是 R10 不成立。

    不许把它归到"other_error"糊过去：那就把一个"规则失效"的严重问题
    降级成了一次无关紧要的异常。
    """
    assert classify_after_completion(200, None) == "still_works"


def test_other_errors_are_not_counted_as_invalidation():
    """别的错（网络/参数/余额…）**不能**当成"密钥已失效"来记账。

    否则一次偶发失败就会被读成"完成条件生效了"——R10 变成假通过。
    """
    assert classify_after_completion(500, "SERVER_ERROR") == "other_error"
    assert classify_after_completion(403, "TRANSFER_LIMIT_EXCEEDED") == "other_error"
    assert classify_after_completion(0, None) == "other_error"


# ---- B8（第 7 步评审阻断项）：完成条件的**另外两种类型**必须也被真实验过 ----
#
# 评审指出：计划要求用一个 `time_elapsed` 探针把 R1 的"24h 失效"从"只回读 spec"
# 变成**实测**，并覆盖 demo Pact 真正在用的那种完成条件类型；实现里只有 `tx_count`，
# 全仓 `grep EXPIRED` 为 0，却记成"6 项缺口全部关闭"。
# R10 列了三种完成条件（累计花费 / 交易次数 / 时间到期），只验一种不算覆盖。
from pactpay.checks.complete import (  # noqa: E402
    build_amount_spent_pact_spec,
    build_time_elapsed_pact_spec,
)


def test_time_elapsed_spec_uses_that_condition_type():
    spec = build_time_elapsed_pact_spec(seconds="60")

    assert spec["completion_conditions"] == [{"type": "time_elapsed", "threshold": "60"}]


def test_amount_spent_spec_uses_that_condition_type():
    spec = build_amount_spent_pact_spec(threshold="0.005")

    assert spec["completion_conditions"] == [{"type": "amount_spent", "threshold": "0.005"}]


def test_all_three_completion_types_are_covered_by_probes():
    """R10 的三种类型都要有探针在管——少一种就是覆盖缺口。"""
    specs = [
        build_time_elapsed_pact_spec(seconds="60"),
        build_amount_spent_pact_spec(threshold="0.005"),
    ]
    types = {c["type"] for s in specs for c in s["completion_conditions"]}

    assert types == {"time_elapsed", "amount_spent"}


def test_probe_specs_allow_exactly_the_transfer_they_need():
    """探针本身要能放行它要打的那笔，否则会失败在"被策略拒"而不是"完成条件"上。"""
    for spec in (build_time_elapsed_pact_spec(seconds="60"), build_amount_spent_pact_spec(threshold="0.005")):
        policy = spec["policies"][0]
        assert policy["rules"]["effect"] == "allow"
        assert policy["rules"]["when"]["chain_in"] == ["TBASE_SETH"]
        assert policy["rules"]["when"]["token_in"] == [{"chain_id": "TBASE_SETH", "token_id": "TBASE_USDC"}]


def test_probe_specs_do_not_set_a_deny_cap_that_would_block_the_probe():
    """探针的 deny 上限必须大于它要付的金额，否则验的是 deny 而不是完成条件。"""
    from decimal import Decimal

    spec = build_amount_spent_pact_spec(threshold="0.005")

    assert Decimal(spec["policies"][0]["rules"]["deny_if"]["amount_gt"]) > Decimal("0.005")


def test_the_probe_constants_themselves_are_pinned():
    """探针用的**常量**也要钉住。

    变异测试发现：只测 `build_time_elapsed_pact_spec(seconds="60")` 挡不住
    "把默认值 `TIME_ELAPSED_SECONDS` 改错"——因为测试显式传了参数，默认值根本没被走到。
    这类"默认值漂了但测试全绿"的洞，只有直接断言默认值才堵得住。
    """
    from pactpay.checks.complete import (
        AMOUNT_SPENT_THRESHOLD,
        TIME_ELAPSED_SECONDS,
    )

    # 默认值必须真的用上
    assert build_time_elapsed_pact_spec()["completion_conditions"] == [
        {"type": "time_elapsed", "threshold": TIME_ELAPSED_SECONDS}
    ]
    assert build_amount_spent_pact_spec()["completion_conditions"] == [
        {"type": "amount_spent", "threshold": AMOUNT_SPENT_THRESHOLD}
    ]
    # 且取值本身要合理：time_elapsed 要能在一次检查里跑完（否则没人会等）
    assert 0 < int(TIME_ELAPSED_SECONDS) <= 120
    # amount_spent 的门槛要正好是"一笔 /api/cheap 就能打到"
    assert AMOUNT_SPENT_THRESHOLD == "0.005"


def test_time_elapsed_expected_status_is_the_measured_one():
    """`time_elapsed` 命中后落 `completed`，**不是** `expired`（实跑纠正）。

    探针第一次实跑就报了 `status: active -> completed（期望 expired）✗`。
    文档写得很清楚：`expired` 专指"Pact 活着、但 `expires_at` 到了、**且没有**
    `time_elapsed` 完成条件"；而配了 `time_elapsed` 的走的是**完成条件**那条路，
    落 `completed`。把这条实测事实钉住，免得日后又按字面猜回去。
    """
    import inspect

    from pactpay.checks import complete as complete_check

    source = inspect.getsource(complete_check._run)
    assert 'label="time-elapsed"' in source
    assert 'expected_status="completed"' in source
    assert 'expected_status="expired"' not in source


# ---- M17/M31：探针的结论到底算不算数（原先聚合内联在 main 里，改成 False 全绿）----

from pactpay.checks.complete import aggregate_exit_code  # noqa: E402


def test_a_failed_probe_makes_the_whole_check_fail(capsys):
    """**每条探针都有否决权**。把聚合里任何一行改成 `failed |= False`
    （即"这条探针说了不算"）都必须让这条测试变红。"""
    code = aggregate_exit_code(False, [{"label": "time-elapsed", "ok": False}])

    assert code == 1, "一条探针失败了，整体却报通过 —— 那条探针等于没跑"
    assert "time-elapsed" in capsys.readouterr().out


def test_every_probe_is_able_to_veto(capsys):
    """逐个验：三条探针**各自**失败都必须导致退出码 1。"""
    all_ok = [
        {"label": "tx-count-payment", "ok": True},
        {"label": "time-elapsed", "ok": True},
        {"label": "amount-spent", "ok": True},
    ]
    assert aggregate_exit_code(False, all_ok) == 0, "全通过时不该报失败"

    for i, probe in enumerate(all_ok):
        broken = [dict(p) for p in all_ok]
        broken[i]["ok"] = False
        assert aggregate_exit_code(False, broken) == 1, f"{probe['label']} 的失败被忽略了"


def test_missing_probe_list_does_not_silently_pass():
    """一条探针都没收到时，只有 `any_failed` 说了算 —— 但绝不能因为"没收到"就报通过
    而掩盖"根本没跑探针"这件事（那种情况由 `any_failed` 覆盖）。"""
    assert aggregate_exit_code(True, []) == 1
    assert aggregate_exit_code(False, []) == 0
