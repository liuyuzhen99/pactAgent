"""证据落盘（T7）的验收单测。

对应方案 A7：一次运行的证据文件里要能数出**真实发生过的**决策笔数，
且拒绝那笔必须带着结构化原因与命中策略；成功那笔必须带着链上交易哈希。

期望值全部手写字面量；不依赖真实网络。
"""

from __future__ import annotations

from decimal import Decimal

from pactpay.agent import PurchaseOutcome
from pactpay.evidence import (
    count_outcomes,
    match_audit_denial,
    purchase_as_evidence,
    within_run,
)

PAID = PurchaseOutcome(
    resource="/api/cheap",
    amount_usd=Decimal("0.005"),
    description="原始运营数据",
    outcome="paid",
    http_status=200,
    data={"report_id": "cheap-raw-001"},
    settlement_tx="0x9f9f372583e5130baef16dda008bb1a761c23e2c401173fcbeeab62d453ec1bc",
    denial=None,
)

DENIED = PurchaseOutcome(
    resource="/api/premium",
    amount_usd=Decimal("0.05"),
    description="深度分析结论",
    outcome="denied",
    http_status=422,
    data=None,
    settlement_tx=None,
    denial={
        "code": "X402_POLICY_DENIED",
        "reason": "x402_no_matching_payment",
        "policy_reason": "matched_pact_transfer_deny_if",
        "policy_id": "34781b4c-ea9a-4351-b963-f461a0680705",
    },
)



def test_counts_reflect_the_real_outcomes():
    """计数必须来自**真实发生过的**结果，而且三种结果都要在。"""
    purchases = [purchase_as_evidence(PAID), purchase_as_evidence(DENIED)]

    assert count_outcomes(purchases) == {"paid": 1, "denied": 1, "failed": 0}


def test_paid_purchase_keeps_its_chain_tx():
    row = purchase_as_evidence(PAID)

    assert row["settlement_tx"] == "0x9f9f372583e5130baef16dda008bb1a761c23e2c401173fcbeeab62d453ec1bc"
    assert row["amount_usd"] == "0.005"
    assert row["denial"] is None


def test_denied_purchase_keeps_policy_and_no_chain_tx():
    """R9 要求页面能说清"为什么被拒、依据哪条策略"——所以这两样必须在证据里。"""
    row = purchase_as_evidence(DENIED)

    assert row["settlement_tx"] is None
    assert row["denial"]["policy_reason"] == "matched_pact_transfer_deny_if"
    assert row["denial"]["policy_id"] == "34781b4c-ea9a-4351-b963-f461a0680705"
    assert row["denial"]["code"] == "X402_POLICY_DENIED"


def test_amount_is_serialized_as_a_string_not_a_float():
    """金额必须是字符串。

    Decimal -> float 会丢精度（0.1 + 0.2 那类问题），而在这个项目里
    "金额被悄悄改掉一点点"正好会打在预算边界的判断上。
    """
    row = purchase_as_evidence(PAID)

    assert isinstance(row["amount_usd"], str)
    assert row["amount_usd"] == "0.005"


# 下面这两条钉的是**实测出来的一个事实**：Cobo 的审计日志记录转账拒绝、
# **不记录 x402 付款的拒绝**（2026-10-12 实测，等 2/8/20 秒都没有那一行）。
# Dashboard 必须如实区分这两种情况，不能一律声称"在不可篡改审计里"。

AUDIT_WITH_TRANSFER_DENIAL = [
    {
        "id": 926973,
        "action": "transfer.denied",
        "result": "denied",
        "authz_details": {"reason": "no_pact_transfer_allow_policy_matched", "policy_id": None},
    },
    {
        "id": 926972,
        "action": "transfer.initiate",
        "result": "allowed",
        "authz_details": {"reason": None, "pact_id": "1dc64f61-d76d-4b5a-8588-c7348a149a22"},
    },
]

AUDIT_WITHOUT_THE_X402_DENIAL = [
    {
        "id": 926962,
        "action": "transfer.initiate",
        "result": "allowed",
        "authz_details": {"reason": None, "pact_id": "1dc64f61-d76d-4b5a-8588-c7348a149a22"},
    }
]


def test_finds_the_audit_row_for_a_transfer_denial():
    """转账路径的拒绝：审计日志里**有**对应行 → 要能找到，Dashboard 才能引用它。"""
    denial = {
        "code": "ADDRESS_NOT_WHITELISTED",
        "policy_reason": "no_pact_transfer_allow_policy_matched",
    }

    found = match_audit_denial(denial, AUDIT_WITH_TRANSFER_DENIAL)

    assert found is not None
    assert found["id"] == 926973
    assert found["action"] == "transfer.denied"


def test_returns_none_for_the_x402_denial_because_cobo_never_recorded_it():
    """x402 路径的拒绝：Cobo **不写**审计行 → 必须返回 None。

    这条测试是"不许假装"的守门人：如果哪天有人图省事，让匹配逻辑在找不到时
    回退到"最近一条 denied 行"或随便凑一条，Dashboard 就会声称这条拒绝
    "在不可篡改审计里"——**而那是假的**。宁可为空，也不许凑。
    """
    denial = {
        "code": "X402_POLICY_DENIED",
        "policy_reason": "matched_pact_transfer_deny_if",
    }

    assert match_audit_denial(denial, AUDIT_WITHOUT_THE_X402_DENIAL) is None


# ---- 时间窗：这个坑是**真踩到的**，不是假想 ----
# 实测现场：审计日志里躺着一条 02:22:11 的 `transfer.denied`
# （reason 同为 `matched_pact_transfer_deny_if`，来自**另一次转账操作**），
# 而本次运行开始于 02:36:28。只按 reason 匹配的话，Dashboard 会把 x402 那笔的拒绝
# 错误地标成"在不可篡改审计日志里"——**正是这套标注要防的那句假话**。

RUN_STARTED_AT = "2026-10-12T02:36:28.699773+00:00"

STALE_DENIAL_ROW = [
    {
        "id": 926920,
        "action": "transfer.denied",
        "result": "denied",
        "authz_details": {
            "reason": "matched_pact_transfer_deny_if",
            "policy_id": "456b6620-94c4-44f5-97dc-f5f58f283bdf",
        },
        "created_at": "2026-10-12T02:22:11.441726Z",
    }
]


def test_ignores_a_denial_row_that_predates_the_run():
    """同样 reason、但时间**早于本次运行**的审计行，不许拿来当本次拒绝的证据。"""
    denial = {"code": "X402_POLICY_DENIED", "policy_reason": "matched_pact_transfer_deny_if"}

    assert match_audit_denial(denial, STALE_DENIAL_ROW, since=RUN_STARTED_AT) is None
    # 同一行若**落在**运行窗口内就该匹配上——否则"时间窗"只是把功能整个关掉了
    fresh = [{**STALE_DENIAL_ROW[0], "created_at": "2026-10-12T02:42:43.942053Z"}]
    assert match_audit_denial(denial, fresh, since=RUN_STARTED_AT) is not None


def test_ignores_a_denial_row_whose_time_cannot_be_read():
    """时间读不出来的审计行**不许**当证据。

    宁可说"审计里没有这条"，也不要拿一条时间不明的行去冒充本次拒绝的证据
    ——这正是"不许凑"在边界上的表现。
    """
    denial = {"code": "X402_POLICY_DENIED", "policy_reason": "matched_pact_transfer_deny_if"}

    for bad_time in (None, "", "not-a-time"):
        rows = [{**STALE_DENIAL_ROW[0], "created_at": bad_time}]
        assert match_audit_denial(denial, rows, since=RUN_STARTED_AT) is None, bad_time


def test_an_allowed_row_can_never_evidence_a_denial():
    """只有 `result == "denied"` 的行才能当拒绝的证据。

    这条把"允许"和"拒绝"的语义钉死：即便某天 Cobo 在一行**允许**记录上也填了
    `authz_details.reason`，它也**不能**被拿来佐证一次拒绝——否则页面会指着一行
    "操作成功"的记录说"你看，这就是它被拒绝的审计证据"。
    """
    denial = {"code": "X402_POLICY_DENIED", "policy_reason": "matched_pact_transfer_deny_if"}
    allowed_row_with_same_reason = [
        {
            "id": 926999,
            "action": "transfer.initiate",
            "result": "allowed",
            "authz_details": {"reason": "matched_pact_transfer_deny_if", "policy_id": "abc"},
            "created_at": "2026-10-12T02:42:43.942053Z",
        }
    ]

    assert match_audit_denial(denial, allowed_row_with_same_reason, since=RUN_STARTED_AT) is None


def test_within_run_excludes_rows_with_unreadable_times():
    """时间读不出来的审计行**不算**在本次运行内——宁可少列一行。

    审计表要回答的是"这一次运行里发生了什么"。一行连时间都读不出来的记录，
    放进表里就是在让页面替它背书。少列比乱列好。
    """
    assert within_run({"created_at": "not-a-time"}, RUN_STARTED_AT) is False
    assert within_run({"created_at": None}, RUN_STARTED_AT) is False
    assert within_run({}, RUN_STARTED_AT) is False
    # 没有要求时间窗时（since 为空）不设限——否则会把功能整个关掉
    assert within_run({"created_at": None}, None) is True
