"""A5c：证明"拒绝发生在**签名之前**"（R7）——三重独立证据。

**为什么不能拿 `tx_hash` 为空当证据**：实测确认 x402 付款的 `PaymentResult.tx_hash`
**成功时也是 null**（它只对 `request_close`/`withdraw` 返回）。所以"为空"对成功与失败
**都成立**——拿它当"被拦住了"的判据，等于**功能全坏也过**。这是方案里被抓出来的
最严重缺陷，这里用三重独立证据替换它：

① 拿到 HTTP 403 的结构化拒绝（不是超时、不是网络错误）；
② 该拒绝能在 Cobo 审计日志里按 action 过滤找到，且带 `policy_id`；
③ **紧邻这次尝试前后**采样钱包余额与交易数，**必须不变**。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pactpay.evidence.block import check_no_onchain_transfer


# ---- A5c② 审计佐证：**只对转账路径可用** ----

from pactpay.evidence.block import audit_corroboration  # noqa: E402

TRANSFER_DENIED_ROW = {
    "id": 926193,
    "action": "transfer.denied",
    "result": "denied",
    "created_at": "2026-10-12T03:41:52Z",
    "authz_details": {"reason": "no_pact_transfer_allow_policy_matched",
                      "policy_id": "8124b2ad-0000-0000-0000-000000000000"},
}


def test_audit_corroboration_finds_the_denied_row_by_action():
    """② 必须**按 action 过滤**——不过滤会把同一时段别的拒绝记录混进来。"""
    result = audit_corroboration(
        reason="no_pact_transfer_allow_policy_matched",
        audit_logs=[TRANSFER_DENIED_ROW, {**TRANSFER_DENIED_ROW, "id": 1, "action": "transfer.initiate", "result": "allowed"}],
        since="2026-10-12T00:00:00Z",
        action="transfer.denied",
    )

    assert result["ok"] is True
    assert result["audit_id"] == 926193
    assert result["policy_id"] == "8124b2ad-0000-0000-0000-000000000000"


def test_audit_corroboration_reports_missing_for_the_x402_path():
    """**实测事实**：x402 付款的拒绝**不进**审计日志。

    所以这条佐证对 x402 路径**取不到**——必须如实报"取不到"，
    而不是放宽成"随便找一条 denied 就算数"（那会让 A5c② 变成空断言）。
    """
    result = audit_corroboration(
        reason="x402_no_matching_payment",
        audit_logs=[TRANSFER_DENIED_ROW],
        since="2026-10-12T00:00:00Z",
        action="transfer.denied",
    )

    assert result["ok"] is False
    assert result["why"]


def test_audit_corroboration_ignores_rows_outside_the_run_window():
    """窗口外那条是**上一次运行**的，不能拿来当本次的证据。"""
    result = audit_corroboration(
        reason="no_pact_transfer_allow_policy_matched",
        audit_logs=[{**TRANSFER_DENIED_ROW, "created_at": "2020-01-01T00:00:00Z"}],
        since="2026-10-12T00:00:00Z",
        action="transfer.denied",
    )

    assert result["ok"] is False


# ---- A5c② 需要一个"命中规则"的拒绝：fail-closed 那条**本来就没有 policy_id** ----

from pactpay.evidence.block import probe_matched_rule_denial  # noqa: E402
from pactpay.platform.cobo import PolicyDenied  # noqa: E402


class _FakeClient:
    def __init__(self, deny_with=None):
        self.deny_with = deny_with
        self.calls = []

    def transfer(self, wallet_uuid, body, *, api_key=None):
        self.calls.append({"wallet_uuid": wallet_uuid, "body": body, "api_key": api_key})
        if self.deny_with:
            raise self.deny_with
        return {"result": {"status": "completed"}}


MATCHED_DENIAL = PolicyDenied(
    status=403,
    code="TRANSFER_LIMIT_EXCEEDED",
    reason="matched_pact_transfer_deny_if",
    details={"reason": "matched_pact_transfer_deny_if", "tier": "pact",
             "policy_type": "transfer", "policy_id": "1fb23678-0000-0000-0000-000000000000",
             "chain_id": "TBASE_SETH", "token_id": "TBASE_USDC",
             "dst_addr": "0xabc0000000000000000000000000000000000001"},
    suggestion="", raw={},
)


def test_probe_reports_the_matched_rule_denial_with_its_policy_id():
    """命中 deny_if 的拒绝**必须带 policy_id**——这正是 ② 要的佐证物。"""
    client = _FakeClient(deny_with=MATCHED_DENIAL)

    result = probe_matched_rule_denial(
        client, "w1", pact_key="k1", chain_id="TBASE_SETH", token_id="TBASE_USDC",
        src_addr="0xabc0000000000000000000000000000000000001",
        dst_addr="0xabc0000000000000000000000000000000000001",
        amount="0.05",
    )

    assert result["outcome"] == "denied"
    assert result["code"] == "TRANSFER_LIMIT_EXCEEDED"
    assert result["policy_id"] == "1fb23678-0000-0000-0000-000000000000"
    assert client.calls[0]["api_key"] == "k1"


def test_probe_raises_if_the_over_cap_transfer_unexpectedly_succeeds():
    """超限转账竟然成功了 ⇒ 必须报错。

    这正是这个演示要证明的反面：如果超限能过，"硬边界"就是假的。
    **不许静默返回一个 None 让上层以为没问题。**
    """
    import pytest

    client = _FakeClient()

    with pytest.raises(RuntimeError, match="超限"):
        probe_matched_rule_denial(
            client, "w1", pact_key="k1", chain_id="TBASE_SETH", token_id="TBASE_USDC",
            src_addr="0xabc0000000000000000000000000000000000001",
            dst_addr="0xabc0000000000000000000000000000000000001",
            amount="0.05",
        )


# ---- 实测发现：审计日志有**写入延迟**，必须轮询 ----

from pactpay.evidence.block import wait_for_audit_corroboration  # noqa: E402


class _FakeAuditClient:
    """前 N 次查询返回空，之后才出现那一行——模拟实测到的写入延迟。"""

    def __init__(self, appear_after: int, row: dict):
        self.appear_after = appear_after
        self.row = row
        self.calls = 0

    def list_audit_logs(self, *, wallet_id, limit):
        self.calls += 1
        items = [self.row] if self.calls > self.appear_after else []
        return {"result": {"items": items}}


def test_waits_for_the_audit_row_to_appear():
    """实测踩到过：转账拒绝的审计行**比操作晚一点**才出现，
    查询抢在写入之前就会误判成"审计里没有"——那是个会误导人的假失败。"""
    client = _FakeAuditClient(appear_after=2, row=TRANSFER_DENIED_ROW)

    result = wait_for_audit_corroboration(
        client, "w1", reason="no_pact_transfer_allow_policy_matched",
        action="transfer.denied", since="2026-10-12T00:00:00Z",
        timeout_s=2.0, interval_s=0.01,
    )

    assert result["ok"] is True
    assert result["audit_id"] == 926193
    assert client.calls >= 3


def test_gives_up_after_the_timeout_and_says_so():
    """一直没出现 ⇒ 如实报"没等到"，**不许无限等、也不许编一条出来**。"""
    client = _FakeAuditClient(appear_after=999, row=TRANSFER_DENIED_ROW)

    result = wait_for_audit_corroboration(
        client, "w1", reason="no_pact_transfer_allow_policy_matched",
        action="transfer.denied", since="2026-10-12T00:00:00Z",
        timeout_s=0.05, interval_s=0.01,
    )

    assert result["ok"] is False
    assert "没等到" in result["why"] or "未出现" in result["why"]


def test_action_filter_is_what_keeps_a_lookalike_row_out():
    """**精确隔离"按 action 过滤"这一条。**

    构造一条"别的 action、但同样 denied、reason 也一样"的行，且它**排在前面**、
    带一个**不同的** policy_id。不过滤 action 就会捞到它，从而指认错策略。
    """
    lookalike = {
        "id": 111,
        "action": "wallet.read",                      # 不是我们要的 action
        "result": "denied",
        "created_at": "2026-10-12T03:00:00Z",
        "authz_details": {"reason": "no_pact_transfer_allow_policy_matched",
                          "policy_id": "WRONG-0000-0000-0000-000000000000"},
    }

    result = audit_corroboration(
        reason="no_pact_transfer_allow_policy_matched",
        audit_logs=[lookalike, TRANSFER_DENIED_ROW],
        since="2026-10-12T00:00:00Z",
        action="transfer.denied",
    )

    assert result["ok"] is True
    assert result["policy_id"] == "8124b2ad-0000-0000-0000-000000000000"  # 不是 WRONG-…


def test_a_matching_row_without_policy_id_is_reported_not_accepted():
    """**精确隔离"必须带 policy_id"这一条。**

    找到一行 action/result/reason 都对、但**没有 policy_id** 的（实测 fail-closed
    那条正是这样）——此时不能算佐证成立：没有 policy_id 就指认不出是哪条策略拦的。
    """
    no_policy = {
        "id": 927181,
        "action": "transfer.denied",
        "result": "denied",
        "created_at": "2026-10-12T03:53:32Z",
        "authz_details": {"reason": "no_pact_transfer_allow_policy_matched", "policy_id": None},
    }

    result = audit_corroboration(
        reason="no_pact_transfer_allow_policy_matched",
        audit_logs=[no_policy],
        since="2026-10-12T00:00:00Z",
        action="transfer.denied",
    )

    assert result["ok"] is False
    assert "policy_id" in result["why"]


# ---- B7（第 7 步评审阻断项）：A5c 证据必须是**测出来的**，不是打印出来的 ----
#
# 评审指出三处：
#   ① `demo.py` 打印的 "① HTTP 403 ✅ ③ 无链上痕迹 ✅" 是**硬编码字面量**（该探针从未采样/比较）；
#   ② 写进 run.json 的 `no_chain_trace` 是"存在结构化拒绝"的**派生布尔**，不是测量值；
#   ③ `policy_id` 在字典字面量里**出现两次**，后者覆盖前者，
#      把 A5a 要的"拒绝响应 policy_id == 审计行 policy_id"三方交叉**在产物里销毁了**。
from pactpay.evidence.block import summarize_a5c  # noqa: E402

X402_DENIED = {
    "http_status": 422,
    "code": "X402_POLICY_DENIED",
    "policy_id": "7ef70031-ab6d-4d9b-9039-f0f06d8d9925",
}
MATCHED = {"http_status": 403, "code": "TRANSFER_LIMIT_EXCEEDED", "policy_id": "456b6620-94c4-44f5-97dc-f5f58f283bdf"}
CORROBORATION = {"ok": True, "audit_id": 926973, "policy_id": "456b6620-94c4-44f5-97dc-f5f58f283bdf", "why": "审计行已找到"}
# A5c③ 的测量结果形状（第 2 轮 N2 起改用链上日志 + 阳性对照）。
# **必须带齐 ok/verdict/count/control_count** —— `summarize_a5c` 会用
# `require_measured_trace` 挡住"调用点拿常量冒充测量结果"（第 5 轮 G3）。
TRACE = {
    "ok": True,
    "verdict": "no_trace",
    "why": "链上无痕迹：目标窗口 (100, 101) 内 0 笔 USDC 转账（阳性对照数到 1 笔）",
    "count": 0,
    "control_count": 1,
}


def test_summary_keeps_both_policy_ids_apart():
    """③ **核心**：探针拿到的 policy_id 与审计行的 policy_id 必须是**两个不同的键**。

    **这条以前抓不到它要抓的 bug**（评审第 2 轮 M11 + 实测确认）：原来的 fixture 里
    `MATCHED` 与 `CORROBORATION` 的 `policy_id` 是**同一个字符串**，于是哪怕代码写成
    `"audit_policy_id": probe_policy_id`（两个键共用同一个变量），断言也照样通过。
    而那个 bug **当时真的在代码里**：产物里 `audit_policy_id` 存的是探针的值，
    演示页上打印的"探针 X vs 审计 X → ✅ 一致"两边是同一个 X —— 交叉核对成了自问自答。

    **修法**：让两个来源的 id **不同**，这样"两个键分别等于各自的来源"才是一条有区分度的断言。
    """
    audit_side = {**CORROBORATION, "policy_id": "AUDIT-SIDE-ONLY-0000"}
    summary = summarize_a5c(X402_DENIED, MATCHED, audit_side, TRACE)

    assert summary["matched_rule_denial"]["probe_policy_id"] == MATCHED["policy_id"]
    assert summary["matched_rule_denial"]["audit_policy_id"] == "AUDIT-SIDE-ONLY-0000"


def test_audit_key_does_not_fall_back_to_the_probe_value_when_audit_is_missing():
    """审计那条**没有** policy_id 时，`audit_policy_id` 必须是 `None`，
    绝不许悄悄回落到探针的值 —— 那会把"没找到佐证"伪装成"两边一致"。"""
    summary = summarize_a5c(X402_DENIED, MATCHED, {"ok": False, "policy_id": None, "why": "没找到"}, TRACE)

    assert summary["matched_rule_denial"]["probe_policy_id"] == MATCHED["policy_id"]
    assert summary["matched_rule_denial"]["audit_policy_id"] is None
    assert summary["matched_rule_denial"]["policy_id_matches_audit"] is False


def test_summary_exposes_the_three_way_cross_check_as_a_measured_result():
    """三方交叉要作为一个**算出来的结论**留在产物里，而不是靠人肉比对。"""
    summary = summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE)

    assert summary["matched_rule_denial"]["policy_id_matches_audit"] is True


def test_cross_check_is_false_when_the_ids_differ():
    """反例：两个 id 不同必须报 False（否则这个字段恒真，等于没测）。"""
    summary = summarize_a5c(X402_DENIED, MATCHED, {**CORROBORATION, "policy_id": "other"}, TRACE)

    assert summary["matched_rule_denial"]["policy_id_matches_audit"] is False


def test_http_status_comes_from_the_measurement_not_a_literal():
    """① HTTP 状态必须来自实测返回，不能是硬编码的 403。"""
    summary = summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE)

    assert summary["matched_rule_denial"]["http_status"] == 403
    assert summary["x402_denial"]["http_status"] == 422

    other = summarize_a5c(X402_DENIED, {**MATCHED, "http_status": 418}, CORROBORATION, TRACE)
    assert other["matched_rule_denial"]["http_status"] == 418


def test_no_chain_trace_is_the_measured_snapshot_comparison():
    """② `no_chain_trace` 必须是快照比对的**测量值**，不是"存在结构化拒绝"的派生布尔。"""
    summary = summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE)

    assert summary["matched_rule_denial"]["no_chain_trace"] is True
    assert summary["matched_rule_denial"]["no_chain_trace_why"] == TRACE["why"]


def test_missing_measurement_is_not_silently_treated_as_success():
    """采样缺失（`trace=None`）时不许当成"没痕迹"——那是把未知当通过。"""
    summary = summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, None)

    assert summary["matched_rule_denial"]["no_chain_trace"] is False


def test_outcome_to_a5c_shape_pulls_the_code_out_of_the_nested_denial():
    """`PurchaseOutcome` 里 `code` 藏在 `denial` 子字典里，不在顶层。

    实跑时暴露：直接把 `outcome.__dict__` 丢给 `summarize_a5c`，`code` 取到的是 **None**
    —— 三重证据的第 ① 项看起来"✅"，但机器可读的拒绝码是空的，等于没证明什么。
    这类"结构对不上但两边都不报错"的错配，只有真的跑一遍或专门测形状才抓得到。
    """
    from decimal import Decimal

    from pactpay.agent.pay import PurchaseOutcome
    from pactpay.evidence.block import outcome_to_a5c_shape

    outcome = PurchaseOutcome(
        resource="/api/premium",
        amount_usd=Decimal("0.05"),
        description="深度分析结论",
        outcome="denied",
        http_status=422,
        data=None,
        settlement_tx=None,
        denial={"code": "X402_POLICY_DENIED", "policy_reason": "matched_pact_transfer_deny_if",
                "policy_id": "1b74e383-449d-4b96-9bbb-20d183827400"},
    )

    shaped = outcome_to_a5c_shape(outcome)

    assert shaped["http_status"] == 422
    assert shaped["code"] == "X402_POLICY_DENIED"
    assert shaped["policy_id"] == "1b74e383-449d-4b96-9bbb-20d183827400"


def test_outcome_to_a5c_shape_keeps_a_paid_outcome_free_of_denial_data():
    """成功那笔没有 denial，转换后 `code` 必须是 None 而**不是崩掉或编一个**。"""
    from decimal import Decimal

    from pactpay.agent.pay import PurchaseOutcome
    from pactpay.evidence.block import outcome_to_a5c_shape

    paid = PurchaseOutcome(
        resource="/api/cheap", amount_usd=Decimal("0.005"), description="原始运营数据",
        outcome="paid", http_status=200, data={"report_id": "x"},
        settlement_tx="0xabc", denial=None,
    )

    shaped = outcome_to_a5c_shape(paid)

    assert shaped["code"] is None
    assert shaped["policy_id"] is None


# =====================================================================================
# A5c③ 的**正确形态**：敏感度量 + 阳性对照（评审第 2 轮 N2，阻断项）
# =====================================================================================


def _counter(counts: dict[tuple[int, int], int], *, fail: set | None = None):
    """造一个可注入的计数器：按窗口返回笔数，`fail` 里的窗口模拟查询失败。"""

    def _fn(_subject: str, from_block: int, to_block: int) -> dict:
        if fail and (from_block, to_block) in fail:
            return {"ok": False, "count": None, "error": "模拟 RPC 失败"}
        value = counts.get((from_block, to_block))
        if value is None:
            return {"ok": False, "count": None, "error": f"未预设窗口 {(from_block, to_block)}"}
        return {"ok": True, "count": value, "error": None}

    return _fn


# 采样高度（G5 起：调用点只交出**三个采样事实**，窗口由判据自己构造）
CONTROL_BLOCK = 100    # 已知发生过付款的那个区块（阳性对照）
BEFORE = 200           # 发起被拒尝试**之前**采的高度
AFTER = 210            # 发起之后采的高度
# 判据内部会把这些采样高度构造成下面这两个窗口；测试里仍按窗口形状喂给替身计数器
CONTROL = (CONTROL_BLOCK, CONTROL_BLOCK)
TARGET = (BEFORE, AFTER)

# 一个**恒返 0 的瞎度量**：阳性对照窗口里明明有转账，它也说 0 笔。
# N2 的核心反例，多处复用。
def _blind(subject: str, from_block: int, to_block: int) -> dict:
    return {"ok": True, "count": 0, "error": None}


def test_blind_metric_can_never_report_success():
    """**这条是 N2 的核心**：一个看不见转账的度量，绝不允许给出 ✅。

    评审实测：旧判据在 17 次真实运行里有 16 次，**确已结算**的付款同样报"余额未变"，
    `tx_count` 更是 17/17 全为 `0→0`。也就是说旧判据等价于一个**恒返回"无变化"的仪表**，
    而它给出的是 ✅。这里用一个恒返 0 的计数器精确复现那个"瞎掉的度量"。
    """
    result = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK, count_fn=_blind
    )

    assert result["ok"] is False, "瞎掉的度量竟然给出了通过 —— 这正是 N2 的形态"
    assert result["verdict"] == "inconclusive"


def test_control_window_must_show_a_transfer_before_the_target_is_trusted():
    """阳性对照必须在**已知发生过转账**的窗口上数到 ≥ 1 笔。"""
    result = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK,
        count_fn=_counter({CONTROL: 3, TARGET: 0}),
    )

    assert result["ok"] is True
    assert result["verdict"] == "no_trace"
    assert result["control_count"] == 3          # 对照确实看见了东西
    assert "阳性对照" in result["why"]


def test_a_trace_in_the_target_window_fails_the_check():
    """目标窗口里若真有转账，必须判失败 —— 这是判据的反向能力。"""
    result = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK,
        count_fn=_counter({CONTROL: 3, TARGET: 1}),
    )

    assert result["ok"] is False
    assert result["verdict"] == "trace_found"
    assert result["count"] == 1


def test_control_query_failure_is_inconclusive_not_success():
    """对照查询本身失败（RPC 挂了）也不许给 ✅。"""
    result = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK,
        count_fn=_counter({CONTROL: 3, TARGET: 0}, fail={CONTROL}),
    )

    assert result["ok"] is False
    assert result["verdict"] == "inconclusive"


def test_target_query_failure_is_inconclusive_not_success():
    """目标窗口查询失败同样不许给 ✅ —— "查不到"不等于"没有"。"""
    result = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK,
        count_fn=_counter({CONTROL: 3, TARGET: 0}, fail={TARGET}),
    )

    assert result["ok"] is False
    assert result["verdict"] == "inconclusive"


def test_the_check_is_sensitive_to_the_subject_address():
    """判定必须真的把地址传下去 —— 传错地址会让查询查别人，从而恒返 0。"""
    seen: list[str] = []

    def _spy(subject: str, from_block: int, to_block: int) -> dict:
        seen.append(subject)
        return {"ok": True, "count": 3 if (from_block, to_block) == CONTROL else 0, "error": None}

    check_no_onchain_transfer(
        subject="0xDEADBEEF", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK, count_fn=_spy
    )

    assert seen == ["0xDEADBEEF", "0xDEADBEEF"], f"地址没被原样传下去：{seen}"


# ---- N3/M4：A4 守卫的**行为**测试（原先零覆盖，改回逃逸写法全绿）----

from pactpay.agent.pay import PurchaseOutcome  # noqa: E402
from pactpay.evidence.block import judge_paid_requires_onchain  # noqa: E402


def _paid(tx: str | None) -> PurchaseOutcome:
    return PurchaseOutcome(
        resource="/api/cheap", amount_usd=Decimal("0.005"), description="原始运营数据",
        outcome="paid", http_status=200, data={"report_id": "x"},
        settlement_tx=tx, denial=None,
    )


def test_paid_without_a_settlement_hash_is_a_hard_failure():
    """**这条就是 M4 要抓的**：记成 `paid` 却没有结算哈希时，**不许静默跳过**。

    逃逸写法 `if outcome == "paid" and outcome.settlement_tx:` 在这种情况下
    整段不执行、什么也不返回，于是"没哈希的 paid"一路绿到底。
    正确行为是**明确失败**：判据是"凡是 paid 都必须验链上"。
    """
    verdict = judge_paid_requires_onchain(
        _paid(None), lambda *a, **k: pytest.fail("没有哈希时不该去查链上"),
        expect_from="0xa", expect_to="0xb", expect_value=1,
    )

    assert verdict["ok"] is False
    assert verdict["checked"] is True, "没有哈希时必须仍然进入检查（逃逸写法会返回 checked=False）"
    assert "没有结算哈希" in verdict["why"]


def test_paid_with_a_hash_is_actually_verified_on_chain():
    """有哈希时必须真的去查，并把链上结果带回来。"""
    calls: list[str] = []

    def _verify(tx, **_kw):
        calls.append(tx)
        return {"ok": True, "block_number": 48011015, "transfers": [{"from": "0xa", "to": "0xb", "value": 5000}], "errors": []}

    verdict = judge_paid_requires_onchain(_paid("0xdead"), _verify, expect_from="0xa", expect_to="0xb", expect_value=5000)

    assert calls == ["0xdead"], "没有真的去查链上"
    assert verdict["ok"] is True
    assert verdict["block_number"] == 48011015


def test_onchain_verification_failure_is_propagated():
    """链上复核不过必须失败，且把原因带出来。"""
    verdict = judge_paid_requires_onchain(
        _paid("0xdead"),
        lambda *a, **k: {"ok": False, "block_number": None, "transfers": [], "errors": ["to 不对"]},
        expect_from="0xa", expect_to="0xb", expect_value=5000,
    )

    assert verdict["ok"] is False
    assert "to 不对" in verdict["why"]


def test_non_paid_outcomes_are_not_chain_checked():
    """被拒那笔不该去做链上复核 —— 它本来就没有哈希（这是**正确**的跳过）。"""
    denied = PurchaseOutcome(
        resource="/api/premium", amount_usd=Decimal("0.05"), description="深度分析结论",
        outcome="denied", http_status=422, data=None, settlement_tx=None, denial={"code": "X"},
    )

    verdict = judge_paid_requires_onchain(
        denied, lambda *a, **k: pytest.fail("被拒那笔不该查链上"), expect_from="0xa", expect_to="0xb", expect_value=1
    )

    assert verdict["ok"] is True
    assert verdict["checked"] is False


def test_failed_outcome_is_exactly_the_string_failed():
    """三态里的 `failed` 字面量必须被钉住（N3/M12：改成 `denied` 原先无人变红）。

    这不是吹毛求疵：`failed`（策略放行但没结算）与 `denied`（策略在签名前挡住）
    在**用户可见的结论上是相反的**——前者"钱可能动了"，后者"钱没动"。
    把字面量写错，看板就会把基础设施故障读成"边界生效"。
    """
    from pactpay.agent.pay import _not_settled

    class _Resp:
        status_code = 500

    outcome = _not_settled(
        type("P", (), {"resource": "/api/cheap", "amount_usd": Decimal("0.005"), "description": "d"})(),
        _Resp(), "req-1", "重放 500",
    )

    assert outcome.outcome == "failed", f"结算失败必须是 failed，实际 {outcome.outcome!r}"
    assert outcome.outcome != "denied", "failed 绝不能被写成 denied（两者结论相反）"


# ---- N6：`snapshot_wallet` 的度量来源原先零测试（M30 恒返回 0/0 不可见）----

from pactpay.evidence.block import snapshot_wallet  # noqa: E402


class _FakeWalletClient:
    def __init__(self, balances, pact):
        self._balances = balances
        self._pact = pact
        self.calls: list[tuple] = []

    def list_balances(self, wallet_uuid, *, chain_id, token_id):
        self.calls.append(("list_balances", wallet_uuid, chain_id, token_id))
        return {"result": self._balances}

    def get_pact(self, pact_id):
        self.calls.append(("get_pact", pact_id))
        return {"result": self._pact}


def test_snapshot_wallet_reads_the_balance_and_the_pact_tx_count():
    """快照必须真的读出**这两个数**（而不是恒返回 0/0 也看不出来）。

    它曾经是 A5c③ 的判据来源；现在虽然不再是判据（改用链上日志），
    但仍然**写进证据文件给人看** —— 一个恒返回 0 的仪表盘比没有仪表盘更糟。
    """
    client = _FakeWalletClient(
        [{"chain_id": "TBASE_SETH", "token_id": "TBASE_USDC", "amount": "19.855"}],
        {"progress_tx_count": 7},
    )

    snap = snapshot_wallet(client, "w-1", "p-1", chain_id="TBASE_SETH", token_id="TBASE_USDC")

    assert snap["balance"] == Decimal("19.855")
    assert snap["tx_count"] == 7
    assert ("list_balances", "w-1", "TBASE_SETH", "TBASE_USDC") in client.calls
    assert ("get_pact", "p-1") in client.calls


def test_snapshot_wallet_picks_the_right_row_among_several():
    """余额表里有多条（不同链/币）时必须挑中**指定的那条**，不能拿第一条充数。"""
    client = _FakeWalletClient(
        [
            {"chain_id": "SOLDEV", "token_id": "SOLDEV_SOL", "amount": "1.5"},
            {"chain_id": "TBASE_SETH", "token_id": "TBASE_USDC", "amount": "3.25"},
        ],
        {"progress_tx_count": 0},
    )

    snap = snapshot_wallet(client, "w-1", "p-1", chain_id="TBASE_SETH", token_id="TBASE_USDC")

    assert snap["balance"] == Decimal("3.25"), "挑错了行（或拿了第一条）"


def test_snapshot_wallet_is_zero_when_the_row_is_absent():
    """查不到该链/币的行时是 0（不是崩、也不是拿别的币冒充）。"""
    client = _FakeWalletClient(
        [{"chain_id": "SOLDEV", "token_id": "SOLDEV_SOL", "amount": "1.5"}],
        {"progress_tx_count": 0},
    )

    snap = snapshot_wallet(client, "w-1", "p-1", chain_id="TBASE_SETH", token_id="TBASE_USDC")

    assert snap["balance"] == Decimal("0")


def test_snapshot_wallet_tolerates_a_missing_progress_field():
    """`progress_tx_count` 缺失时按 0 处理，不抛 `KeyError`。"""
    client = _FakeWalletClient([], {})

    snap = snapshot_wallet(client, "w-1", "p-1", chain_id="TBASE_SETH", token_id="TBASE_USDC")

    assert snap["tx_count"] == 0


# ---------------------------------------------------------------------------
# 对 N2 修复本身的对抗测试（我自己先攻了一遍，抓出三个洞）
#
# 评审只说了"缺阳性对照"。修完之后我对新实现做对抗测试，又发现三处
# **会把"测不了"伪装成"通过"或"崩溃"** 的形态，全部补上并在此钉死。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("label", "fn"),
    [
        ("查询抛异常（RPC 抖动/DNS 挂）",
         lambda subject, start, end: (_ for _ in ()).throw(RuntimeError("rpc down"))),
        ("count_fn 返回 None",
         lambda subject, start, end: None),
        ("count_fn 返回非字典",
         lambda subject, start, end: 42),
        ("ok=True 但 count 缺失",
         lambda subject, start, end: {"ok": True}),
        ("**ok=True 但 count 是 None**（原实现会 `or 0` 变成 0 → 假 ✅）",
         lambda subject, start, end: {"ok": True, "count": 1 if (start, end) == CONTROL else None}),
        ("count 是字符串 '0'（不是数字）",
         lambda subject, start, end: {"ok": True, "count": 1 if (start, end) == CONTROL else "0"}),
        ("count 是负数",
         lambda subject, start, end: {"ok": True, "count": 1 if (start, end) == CONTROL else -5}),
        ("count 是布尔 True（bool 是 int 的子类，必须排除）",
         lambda subject, start, end: {"ok": True, "count": True}),
        ("查询自报失败",
         lambda subject, start, end: {"ok": False, "error": "timeout"}),
    ],
)
def test_unusable_counters_are_inconclusive_never_success_never_crash(label, fn):
    """**拿不到非负整数计数 ⇒ 一律 inconclusive**，既不许给 ✅，也不许把整个流程炸掉。

    `count is None` 那条尤其要紧：旧写法 `actual.get("count") or 0` 会把它悄悄变成 `0`，
    然后走进"0 笔 ⇒ 无痕迹"分支打出一个**假的 ✅** —— 与第 1 轮被抓的
    `tx_hash` 空断言、第 2 轮被抓的 N2 是**同一种病**：把"未知"当成"通过"。
    """
    result = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK, count_fn=fn
    )

    assert result["ok"] is False, f"{label} 竟然给出了通过"
    assert result["verdict"] == "inconclusive", f"{label} 的判定不是 inconclusive"


def test_a_raising_counter_does_not_escape_as_an_exception():
    """异常必须被就地转成"判不了"，而不是冒泡炸掉整条证据链。

    证据链的可信度要求：**查询失败 ≠ 没有转账**。让异常冒泡意味着 RPC 一抖，
    整个 demo 就崩在半个证据上；吞掉它当成"没痕迹"则更糟。唯一正确的处置是"判不了"。
    """
    def explode(subject, start, end):
        raise ConnectionError("RPC unreachable")

    result = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK, count_fn=explode
    )

    assert result["ok"] is False
    assert "ConnectionError" in result["why"] or "RPC unreachable" in result["why"]


# ---------------------------------------------------------------------------
# #7：A5c③ 的**两条路径**都要是测量值，且都要被测试钉住
#
# 评审第 3 轮用 X6/X7 证明：x402 那半边的 `no_chain_trace` 被硬编码成 True、
# 或者 demo 里把实测结果丢掉，**233 条测试全绿**。
# 即"两条路径都换成新判据"这句话只被钉住了一半。
# ---------------------------------------------------------------------------

def test_the_x402_side_trace_is_derived_from_its_own_measurement():
    """**x402 那半边也必须是测量值**（评审 #7 / X6）。

    先前只有 `matched_rule_denial.no_chain_trace` 被钉住，x402 那条可以硬编码成
    `True` 而无人变红 —— 正是 B7 的原始形态（拿结论冒充证据）。
    """
    measured = {"ok": True, "verdict": "no_trace",
                "why": "链上无痕迹：窗口 (100, 110) 内 0 笔（阳性对照数到 1 笔）",
                "count": 0, "control_count": 1}
    unmeasured = {"ok": False, "verdict": "inconclusive",
                  "why": "阳性对照没数到任何转账 ⇒ 判不了", "count": None, "control_count": 0}

    ok_summary = summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE, measured)
    bad_summary = summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE, unmeasured)
    none_summary = summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE, None)

    assert ok_summary["x402_denial"]["no_chain_trace"] is True
    assert ok_summary["x402_denial"]["no_chain_trace_why"] == measured["why"]

    assert bad_summary["x402_denial"]["no_chain_trace"] is False, (
        "x402 那条的判据说'判不了'，产物却记成了 True —— 拿结论冒充证据（B7 形态）"
    )
    assert none_summary["x402_denial"]["no_chain_trace"] is False, (
        "x402 那条**根本没测**，产物却记成了 True"
    )


def test_the_matched_rule_trace_is_derived_from_its_own_measurement():
    """命中规则那半边同理：判据说判不了，产物不许记 True。"""
    assert summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE)["matched_rule_denial"][
        "no_chain_trace"
    ] is True
    assert summarize_a5c(
        X402_DENIED, MATCHED, CORROBORATION,
        # 阳性对照失败的**真实形状**（不是手写的半截字典 —— 那种现在会被
        # `require_measured_trace` 当成伪造而报错，见 G3）
        {"ok": False, "verdict": "inconclusive", "why": "阳性对照没数到任何转账 ⇒ 判不了",
         "count": None, "control_count": 0},
    )[
        "matched_rule_denial"
    ]["no_chain_trace"] is False
    assert summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, None)["matched_rule_denial"][
        "no_chain_trace"
    ] is False


# ---------------------------------------------------------------------------
# #8：交叉核对在"两边都缺"时不许恒真
# ---------------------------------------------------------------------------

def test_cross_check_is_false_when_both_policy_ids_are_missing():
    """**两边都缺 ⇒ 核对不成立**（评审 #8 / X4）。

    这条钉住一个很容易"顺手简化掉"的写法：把
    `bool(probe and audit and probe == audit)` 简化成 `probe == audit`，
    于是 `None == None` → `True` —— **两个来源都没说话，却被记成"一致"**。
    这与刚修的 M11（`probe == probe` 恒真）是**同一个病**：
    交叉核对的全部意义是拿两个来源对撞，两个都空不叫对上了。
    """
    both_missing = summarize_a5c(
        X402_DENIED, {**MATCHED, "policy_id": None}, {**CORROBORATION, "policy_id": None}, TRACE
    )

    assert both_missing["matched_rule_denial"]["policy_id_matches_audit"] is False, (
        "两边都没有 policy_id，却被记成'探针与审计一致'"
    )
    assert both_missing["matched_rule_denial"]["probe_policy_id"] is None
    assert both_missing["matched_rule_denial"]["audit_policy_id"] is None


def test_cross_check_is_false_when_only_one_side_is_missing():
    """只有一边有值也不算核对成立。"""
    probe_only = summarize_a5c(
        X402_DENIED, MATCHED, {**CORROBORATION, "policy_id": None}, TRACE
    )
    audit_only = summarize_a5c(
        X402_DENIED, {**MATCHED, "policy_id": None}, CORROBORATION, TRACE
    )

    assert probe_only["matched_rule_denial"]["policy_id_matches_audit"] is False
    assert audit_only["matched_rule_denial"]["policy_id_matches_audit"] is False


# ---------------------------------------------------------------------------
# G3（第 5 轮评审，重大）：**判据的函数体被测住了，判据的输入没有。**
#
# 评审实测：把 `demo.py` 里喂给 `summarize_a5c` 的值换成硬编码常量
# （`x402_trace = {"ok": True, "why": "硬编码"}`），292 条测试全绿，
# 而屏幕上照样打 ✅ —— 因为它只断言了"函数算得对"，没管"喂进去的是什么"。
#
# 修法不是再写一条"函数算得对"的测试，而是**把闸门挪到产出 ✅ 的那一步**：
# `require_measured_trace` 只放行带齐测量字段的字典。
# 手写的常量给不出 `verdict`/`count`/`control_count`，于是**当场炸**，
# 而不是安静地打一个来路不明的绿灯。
# ---------------------------------------------------------------------------

def test_a_hardcoded_constant_cannot_stand_in_for_a_measurement():
    """**G3 的核心回归测试**：伪造的"测量结果"必须当场炸，绝不许渲染成 ✅。"""
    import pytest

    from pactpay.evidence.block import summarize_a5c

    with pytest.raises(ValueError, match="缺少测量字段"):
        summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE, {"ok": True, "why": "硬编码"})


def test_a_fake_ok_without_a_real_positive_control_is_rejected():
    """带齐键但**没跑过阳性对照**（`control_count` 不是整数）⇒ 也不许打 ✅。

    这是 G3 更隐蔽的一种形态：伪造者知道要凑字段名，于是补上 `verdict="ok"`，
    但阳性对照是**必须真的跑过**才会有整数计数 —— 编不出来。
    """
    import pytest

    from pactpay.evidence.block import summarize_a5c

    fake = {"ok": True, "verdict": "no_trace", "why": "我说的", "count": 0, "control_count": None}
    with pytest.raises(ValueError, match="阳性对照"):
        summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE, fake)


def test_an_illegal_verdict_is_rejected():
    """`verdict` 只能是 {ok, inconclusive} —— 编一个别的值也不算测量。"""
    import pytest

    from pactpay.evidence.block import summarize_a5c

    with pytest.raises(ValueError, match="verdict"):
        summarize_a5c(
            X402_DENIED, MATCHED, CORROBORATION, TRACE,
            {"ok": True, "verdict": "trust-me", "why": "x", "count": 0, "control_count": 1},
        )


def test_a_missing_measurement_is_false_not_a_crash():
    """**缺测量 ≠ 伪造**：`None` 表示"这一侧没测"，语义是"没证明"（→ False）。

    两者混为一谈会把"没测"变成崩溃，也会把"伪造"变成安静的 False ——
    两种都是错的。所以 `None` 走 False，一个**不是测量结果的东西**才炸。
    """
    from pactpay.evidence.block import summarize_a5c

    summary = summarize_a5c(X402_DENIED, MATCHED, CORROBORATION, TRACE, None)

    assert summary["x402_denial"]["no_chain_trace"] is False


# ---------------------------------------------------------------------------
# G5（第 5 轮评审，重大）：**"窗口有多宽"由调用点说了算。**
#
# 原先窗口在调用点拼好再传进来，评审把这些变异注进去、292 条测试全绿：
#   - `window=(block_after, block_after)` —— 窗口整体挪到**尝试之后**，等于没测
#   - `block_after = block_before`         —— 窗口宽度 0
#   - 兜底断言改成 `> block_after+100` / `and False` —— 废掉
#
# 修法：**窗口由判据自己造**，调用点只交出三个采样高度。
# 于是那些变异要么造不出窗口，要么造出一个形状非法的窗口 —— 一律判 `inconclusive`，
# **绝不给 ✅**。
# ---------------------------------------------------------------------------

def test_a_window_that_starts_after_the_attempt_is_rejected():
    """**G5 核心回归 1**：窗口整体挪到尝试之后（`block_before == block_after`）⇒ 判不了。"""
    result = check_no_onchain_transfer(
        subject="0xabc",
        block_before=AFTER, block_after=AFTER, control_block=CONTROL_BLOCK,
        count_fn=_counter({CONTROL: 3, TARGET: 0}),
    )

    assert result["ok"] is False
    assert result["verdict"] == "inconclusive"
    assert "严格早于" in result["why"]


def test_a_zero_width_window_is_rejected():
    """**G5 核心回归 2**：采样没跨过那次尝试（宽度 0）⇒ 判不了，不给 ✅。"""
    result = check_no_onchain_transfer(
        subject="0xabc",
        block_before=BEFORE, block_after=BEFORE, control_block=CONTROL_BLOCK,
        count_fn=_counter({CONTROL: 3, TARGET: 0}),
    )

    assert result["ok"] is False
    assert result["verdict"] == "inconclusive"


def test_a_control_block_inside_the_target_window_is_rejected():
    """**G5 核心回归 3**：阳性对照那个区块落在被测窗口里 ⇒ 判不了。

    否则本运行**自己那笔已结算的转账**会落进被测窗口，
    于是拿它去指控这笔被拒的付款"留下了痕迹"。
    """
    result = check_no_onchain_transfer(
        subject="0xabc",
        block_before=BEFORE, block_after=AFTER,
        control_block=BEFORE,          # ← 对照区块 == 窗口下界，落进窗口了
        count_fn=_counter({CONTROL: 3, TARGET: 0}),
    )

    assert result["ok"] is False
    assert result["verdict"] == "inconclusive"
    assert "没有早于被测窗口下界" in result["why"]


def test_missing_block_heights_are_inconclusive():
    """采样失败（拿不到高度）⇒ 判不了，绝不因为"没数据"就给通过。"""
    for kwargs in (
        {"block_before": None, "block_after": AFTER, "control_block": CONTROL_BLOCK},
        {"block_before": BEFORE, "block_after": None, "control_block": CONTROL_BLOCK},
        {"block_before": BEFORE, "block_after": AFTER, "control_block": None},
    ):
        result = check_no_onchain_transfer(subject="0xabc", count_fn=_counter({}), **kwargs)
        assert result["ok"] is False, kwargs
        assert result["verdict"] == "inconclusive", kwargs


def test_the_window_adapter_actually_queries_the_whole_window():
    """**G5 核心回归 4**：查询口子必须把 `from_block`/`to_block` **两个**都传下去。

    原先它是调用点里的匿名 lambda，评审把 `to_block=b` 改成 `to_block=a`
    （查询区间被压成一个点，几乎什么都查不到）**无人变红**。
    具名之后这里直接钉住它。
    """
    from unittest import mock

    from pactpay.evidence.block import count_transfers_in_window

    with mock.patch("pactpay.platform.onchain.count_usdc_transfers_from") as fake:
        fake.return_value = {"ok": True, "count": 0}
        count_transfers_in_window("0xABC", 200, 210)

    fake.assert_called_once_with("0xABC", from_block=200, to_block=210)


def test_the_legal_verdict_list_matches_what_the_checker_actually_returns():
    """`_LEGAL_VERDICTS` 必须**覆盖** `check_no_onchain_transfer` 真的会返回的每个 verdict。

    这条不是形式主义：守卫第一版手写了 `("ok", "inconclusive")`，而真实返回值是
    `no_trace` / `trace_found` / `inconclusive` —— 于是**真跑起来**守卫把自己人拦下了
    （单元测试全绿，因为测试里喂的是手写的假 trace）。清单必须与实现同源。
    """
    from pactpay.evidence.block import VERDICT_INCONCLUSIVE, VERDICT_NO_TRACE, VERDICT_TRACE_FOUND

    produced = set()
    # 走遍每一条返回分支
    cases = [
        dict(block_before=None, block_after=None, control_block=None, count_fn=_counter({})),
        dict(block_before=BEFORE, block_after=BEFORE, control_block=CONTROL_BLOCK, count_fn=_counter({})),
        dict(block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK, count_fn=_blind),
        dict(block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK, count_fn=_counter({CONTROL: 3, TARGET: 0})),
        dict(block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK, count_fn=_counter({CONTROL: 3, TARGET: 2})),
    ]
    for case in cases:
        produced.add(check_no_onchain_transfer(subject="0xabc", **case)["verdict"])

    assert produced == {VERDICT_INCONCLUSIVE, VERDICT_NO_TRACE, VERDICT_TRACE_FOUND}, (
        f"实际产出的 verdict 集合 {produced} 与常量不一致 —— 守卫的合法清单已经漂移"
    )


def test_only_no_trace_counts_as_proven():
    """**只有 `no_trace` 代表证明成立**。`trace_found` 是"证伪"，`inconclusive` 是"没证明"。

    三者混同就会把"没测出来"当成"证明了没发生"—— 这正是本轮要根除的那个病。
    """
    ok = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK,
        count_fn=_counter({CONTROL: 3, TARGET: 0}),
    )
    found = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK,
        count_fn=_counter({CONTROL: 3, TARGET: 2}),
    )
    unknown = check_no_onchain_transfer(
        subject="0xabc", block_before=BEFORE, block_after=AFTER, control_block=CONTROL_BLOCK,
        count_fn=_blind,
    )

    assert ok["ok"] is True and ok["verdict"] == "no_trace"
    assert found["ok"] is False and found["verdict"] == "trace_found"
    assert unknown["ok"] is False and unknown["verdict"] == "inconclusive"


# ---------------------------------------------------------------------------
# G5：窗口**由判据自己构造**，调用点交不出一个"看起来对"的窗口
# ---------------------------------------------------------------------------


def test_measurement_window_is_built_from_the_sampled_blocks():
    """构造出来的窗口必须**逐字等于**采样到的那对高度。

    评审 G5 把 `window = (block_before, block_after)` 改成 `(block_after, block_after)`
    （窗口整体挪到**尝试之后**，等于没测），292 条测试全绿。现在窗口由
    `build_measurement_window` 自己造，这条测试钉住它。
    """
    from pactpay.evidence.block import build_measurement_window

    window, control, why = build_measurement_window(block_before=100, block_after=105, control_block=99)
    assert why == ""
    assert window == (100, 105), f"窗口被改动了：{window}"
    assert control == (99, 99), f"阳性对照窗口被改动了：{control}"


def test_measurement_window_rejects_a_zero_width_or_shifted_window():
    """宽度 0 / 挪到尝试之后 / 对照区块没早于窗口 —— 三种形状都必须被拒（返回空窗口）。"""
    from pactpay.evidence.block import build_measurement_window

    # 宽度 0：采样没跨过那次尝试
    window, _, why = build_measurement_window(block_before=100, block_after=100, control_block=99)
    assert why and window[0] > window[1], "宽度 0 的窗口必须被拒"

    # 窗口挪到尝试之后（block_before > block_after）
    window, _, why = build_measurement_window(block_before=105, block_after=100, control_block=99)
    assert why and window[0] > window[1], "倒置的窗口必须被拒"

    # 阳性对照区块没早于窗口下界 —— 自己那笔成功的付款会落进被测窗口
    window, _, why = build_measurement_window(block_before=99, block_after=105, control_block=99)
    assert why and window[0] > window[1], "对照区块与被测窗口重合时必须被拒"

    # 拿不到高度
    window, _, why = build_measurement_window(block_before=None, block_after=105, control_block=99)
    assert why and window[0] > window[1], "采样失败时必须被拒"


def test_a_rejected_window_makes_the_check_inconclusive_not_a_pass():
    """窗口不合法 ⇒ 结论必须是 `inconclusive`（不给 ✅），**不是**通过。"""
    from pactpay.evidence.block import build_measurement_window

    _, _, why = build_measurement_window(block_before=100, block_after=100, control_block=99)
    result = check_no_onchain_transfer(
        subject="0xabc", block_before=100, block_after=100, control_block=99, count_fn=_blind
    )
    assert result["ok"] is False
    assert result["verdict"] == "inconclusive"
    assert why  # 原因必须能说出来
