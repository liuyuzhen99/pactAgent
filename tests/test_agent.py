"""Agent 采购执行（T4/T5）的验收单测。

对应方案 A4 / A5：
- **A4** 便宜那笔（限内）→ 付款成功、拿回数据、有链上结算哈希。
- **A5** 贵那笔（超单笔上限）→ 被策略拒绝、**不产生链上交易**、且拿到**结构化**拒绝原因。

期望值全部是**手写字面量**；替身是手写的（`FakeCobo` + 假的 httpx 层），
不用 `unittest.mock`（tdd 卡第 6 条禁止 mock 类 API）。
断言打在**被测代码产出的结果**上（`PurchaseOutcome`），不打在替身上。
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal

import httpx
import pytest

from pactpay.agent import PurchaseOutcome, buy
from pactpay.cobo import PolicyDenied
from pactpay.planner import Purchase

# ---- 手写字面量（Base Sepolia 已独立核验：USDC name()=="USDC"、decimals()==6）----
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
NETWORK_V2 = "eip155:84532"
PAY_TO = "0x757868baab9ab42a20d0ed2d8ffcff9c6df82ea7"
CHEAP_TX = "0x48ccf9d6042970eb78f814f159f5f40d5d288213a41e360cc1e1efa7befda292"
CHEAP_ATOMIC = "5000"
PREMIUM_ATOMIC = "50000"
WALLET_UUID = "f342c5cf-b6ed-44a2-8d33-55b554f8a4d0"
PACT_KEY = "caw_fake_pact_scoped_key"

CHEAP = Purchase(resource="/api/cheap", amount_usd=Decimal("0.005"), description="原始运营数据")
PREMIUM = Purchase(resource="/api/premium", amount_usd=Decimal("0.05"), description="深度分析结论")


def _challenge_header(amount_atomic: str) -> str:
    """构造收款方的 402 挑战头（这是**输入**，所以可以由测试来拼）。"""
    required = {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": NETWORK_V2,
                "asset": USDC_BASE_SEPOLIA,
                "amount": amount_atomic,
                "payTo": PAY_TO,
                "maxTimeoutSeconds": 300,
            }
        ],
    }
    return base64.b64encode(json.dumps(required).encode()).decode()


def _settlement_header(transaction: str) -> str:
    return base64.b64encode(
        json.dumps({"success": True, "transaction": transaction, "network": NETWORK_V2, "payer": PAY_TO}).encode()
    ).decode()


class FakeCobo:
    """手写替身：记录收到的参数，按预置行为返回成功或抛结构化策略拒绝。"""

    def __init__(self, deny_with: PolicyDenied | None = None, replay: bool = False) -> None:
        self.replay = replay
        self.deny_with = deny_with
        self.payments: list[dict] = []

    def payment(
        self,
        wallet_uuid: str,
        *,
        protocol: str,
        request_id: str,
        x402_payment_required: str | None = None,
        api_key: str | None = None,
        expect_error: bool = False,
    ) -> dict:
        self.payments.append(
            {
                "wallet_uuid": wallet_uuid,
                "protocol": protocol,
                "request_id": request_id,
                "challenge": x402_payment_required,
                "api_key": api_key,
            }
        )
        if self.deny_with is not None:
            raise self.deny_with
        return {
            "success": True,
            "result": {
                "status": "completed",
                "protocol": "x402",
                "idempotent": self.replay,
                "retry_headers": {"PAYMENT-SIGNATURE": "signed-payload-from-cobo"},
            },
        }


def _fake_httpx(monkeypatch, *, cheap_atomic: str = CHEAP_ATOMIC,
                settlement_tx: str = CHEAP_TX) -> list[dict]:
    """假的收款方：第一次请求回 402 挑战，带签名重放时回 200 + 数据。

    返回一个列表，记录每次收到的请求头（用来断言"重放时确实带上了签名"）。
    """
    seen: list[dict] = []

    def fake_get(url: str, headers=None, timeout=None):  # noqa: ANN001
        seen.append({"url": url, "headers": dict(headers or {})})
        path = url.split("127.0.0.1:8001")[-1]
        amount = cheap_atomic if path == "/api/cheap" else PREMIUM_ATOMIC

        # 没带签名 → 402 挑战
        if not (headers or {}).get("PAYMENT-SIGNATURE"):
            return httpx.Response(
                402,
                headers={"PAYMENT-REQUIRED": _challenge_header(amount)},
                json={"error": "PAYMENT-SIGNATURE header is required"},
                request=httpx.Request("GET", url),
            )
        # 带了签名 → 放行
        body = {"report_id": "cheap-raw-001", "kind": "raw_data", "records": 42}
        return httpx.Response(
            200,
            headers={"PAYMENT-RESPONSE": _settlement_header(settlement_tx)},
            json=body,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


def test_paid_purchase_returns_data_and_settlement_tx(monkeypatch):
    """A4：限内那笔 → paid、拿回数据、有链上结算哈希，且重放时带上了 Cobo 给的签名。"""
    seen = _fake_httpx(monkeypatch)
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.outcome == "paid"
    assert outcome.data is not None
    assert outcome.data["report_id"] == "cheap-raw-001"
    assert outcome.settlement_tx == CHEAP_TX
    assert outcome.denial is None
    # 自己的契约：必须把 402 的挑战**原样**透传给 Cobo，且用 pact-scoped key
    assert cobo.payments[0]["challenge"] == _challenge_header(CHEAP_ATOMIC)
    assert cobo.payments[0]["protocol"] == "x402"
    assert cobo.payments[0]["api_key"] == PACT_KEY
    # 重放那一次必须带上 Cobo 回的签名
    assert seen[1]["headers"].get("PAYMENT-SIGNATURE") == "signed-payload-from-cobo"


def test_over_limit_purchase_is_denied_without_any_chain_tx(monkeypatch):
    """A5：超限那笔 → denied、结构化拒绝原因、**没有链上交易**、也没有拿到数据。"""
    seen = _fake_httpx(monkeypatch)
    denial = PolicyDenied(
        status=403,
        code="TRANSFER_LIMIT_EXCEEDED",
        reason="matched_pact_transfer_deny_if",
        details={"tier": "pact", "policy_type": "transfer", "policy_id": "456b6620-94c4-44f5-97dc-f5f58f283bdf"},
        suggestion="Operation denied by the pact's policy.",
        raw={"success": False},
    )
    cobo = FakeCobo(deny_with=denial)

    outcome = buy(cobo, "http://127.0.0.1:8001", PREMIUM, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.outcome == "denied"
    assert outcome.denial is not None
    assert outcome.denial["code"] == "TRANSFER_LIMIT_EXCEEDED"
    assert outcome.denial["reason"] == "matched_pact_transfer_deny_if"
    assert outcome.denial["details"]["policy_id"] == "456b6620-94c4-44f5-97dc-f5f58f283bdf"
    # R7 的核心：被拒那笔**不能留下任何链上痕迹**，也不能拿到数据
    assert outcome.settlement_tx is None
    assert outcome.data is None
    # 被拒时**不许**再拿签名去重放
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("purchase", "expected_atomic"),
    [(CHEAP, CHEAP_ATOMIC), (PREMIUM, PREMIUM_ATOMIC)],
)
def test_challenge_is_passed_through_verbatim(monkeypatch, purchase: Purchase, expected_atomic: str):
    """透传给 Cobo 的挑战必须与收款方给的**逐字相同**，且金额就是该端点自己的价。

    钉住"别自己重算金额"：一旦有人图省事在 agent 里按 `amount_usd` 重新拼一个挑战，
    收款方的校验就会对不上——而那种错在 happy path 上看起来一切正常。
    """
    _fake_httpx(monkeypatch)
    cobo = FakeCobo()

    buy(cobo, "http://127.0.0.1:8001", purchase, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    passed = cobo.payments[0]["challenge"]
    decoded = json.loads(base64.b64decode(passed))
    assert decoded["accepts"][0]["amount"] == expected_atomic
    assert passed == _challenge_header(expected_atomic)


def test_each_purchase_uses_a_fresh_request_id(monkeypatch):
    """幂等键必须每笔都换。

    `request_id` 是 Cobo 的幂等键：同一个值重复提交会**返回原始结果而不重复扣款**。
    如果两笔共用一个 id，第二笔会拿到第一笔的旧结果——看起来"成功"，实则根本没付款。
    """
    _fake_httpx(monkeypatch)
    cobo = FakeCobo()

    buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)
    buy(cobo, "http://127.0.0.1:8001", PREMIUM, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    first, second = cobo.payments[0]["request_id"], cobo.payments[1]["request_id"]
    assert first != second
    assert first and second


def test_outcome_carries_the_planned_amount_for_evidence(monkeypatch):
    """结果里要带上计划金额与说明——Dashboard 与证据文件都要靠它。"""
    _fake_httpx(monkeypatch)

    outcome = buy(FakeCobo(), "http://127.0.0.1:8001", PREMIUM, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert isinstance(outcome, PurchaseOutcome)
    assert outcome.amount_usd == Decimal("0.05")
    assert outcome.resource == "/api/premium"
    assert outcome.description == "深度分析结论"


def test_refuses_to_report_paid_when_the_payee_never_asked_for_payment(monkeypatch):
    """收款方没要钱就返回 200 时，**必须报错，不许记成 paid**。

    这是一条真实的静默失败路径：万一收款方的 x402 中间件没挂上（配置漏了），
    每个端点都会直接 200。如果 agent 照旧记 `paid`，Dashboard 上会显示"支付成功"、
    却**没有任何链上交易**——整个项目要证明的东西就变成了假的。
    """

    def fake_get_no_paywall(url: str, headers=None, timeout=None):  # noqa: ANN001
        return httpx.Response(200, json={"report_id": "cheap-raw-001"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get_no_paywall)
    cobo = FakeCobo()

    with pytest.raises(RuntimeError, match="402"):
        buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    # 而且**一分钱都不该付出去**
    assert cobo.payments == []


# 下面这份是 **x402 路径超限时的真实响应**（2026-10-12 实测逐字抄下来的）。
# 与普通转账路径不同：真正的策略信息被埋进了 details.reject_details[0]，
# 顶层只是笼统的 X402_POLICY_DENIED / x402_no_matching_payment。
X402_DENIAL = PolicyDenied(
    status=422,
    code="X402_POLICY_DENIED",
    reason="x402_no_matching_payment",
    details={
        "total_accepts": 1,
        "supported_x402_chains": ["BASE_ETH", "MATIC", "SETH", "SOL", "SOLDEV_SOL", "TBASE_SETH"],
        "reject_details": [
            {
                "index": 0,
                "network": "eip155:84532",
                "asset": USDC_BASE_SEPOLIA,
                "chain_id": "TBASE_SETH",
                "reason": "policy_denied",
                "policy_reason": "matched_pact_transfer_deny_if",
                "policy_id": "0d46924a-31d0-4d78-9565-ab717560753b",
            }
        ],
    },
    suggestion="A supported chain/token with sufficient balance was found but denied by policy.",
    raw={"success": False},
)


def test_x402_denial_surfaces_the_nested_policy(monkeypatch):
    """x402 路径的拒绝：**必须把埋在 reject_details 里的策略信息提出来**。

    R9 要求 Dashboard 能显示"触发了哪条 Pact 策略"。x402 路径的顶层只给了笼统原因，
    真正的 `policy_reason` / `policy_id` 在 `details.reject_details[0]` 里。
    不统一提上来的话，Dashboard 就得自己懂两种响应形状——而它多半会漏掉这一种。
    """
    _fake_httpx(monkeypatch)
    cobo = FakeCobo(deny_with=X402_DENIAL)

    outcome = buy(cobo, "http://127.0.0.1:8001", PREMIUM, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.outcome == "denied"
    denial = outcome.denial
    assert denial is not None
    # 顶层原样保留（如实记录 Cobo 说了什么）
    assert denial["code"] == "X402_POLICY_DENIED"
    assert denial["reason"] == "x402_no_matching_payment"
    # 但真正可读的策略信息要提到顶层
    assert denial["policy_reason"] == "matched_pact_transfer_deny_if"
    assert denial["policy_id"] == "0d46924a-31d0-4d78-9565-ab717560753b"
    assert denial["reject_reason"] == "policy_denied"
    # 一样不能有链上痕迹
    assert outcome.settlement_tx is None


def test_plain_transfer_denial_policy_fields_come_from_the_top_level(monkeypatch):
    """普通转账路径的拒绝：策略信息**本来就在顶层**，也要照样填进同一组字段。

    两条路径形状不同，但产出的证据结构必须一致——否则 Dashboard 要写两套解析。
    """
    _fake_httpx(monkeypatch)
    cobo = FakeCobo(
        deny_with=PolicyDenied(
            status=403,
            code="TRANSFER_LIMIT_EXCEEDED",
            reason="matched_pact_transfer_deny_if",
            details={"tier": "pact", "policy_type": "transfer", "policy_id": "456b6620-94c4-44f5-97dc-f5f58f283bdf"},
            suggestion="",
            raw={},
        )
    )

    outcome = buy(cobo, "http://127.0.0.1:8001", PREMIUM, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.denial is not None
    assert outcome.denial["policy_reason"] == "matched_pact_transfer_deny_if"
    assert outcome.denial["policy_id"] == "456b6620-94c4-44f5-97dc-f5f58f283bdf"


def test_outcome_carries_request_id_and_idempotent_for_evidence(monkeypatch):
    """A7b：**证据**里必须能看出每笔的 `request_id` 与它是不是幂等重放。

    之前只断言了"调用时传了不同的 request_id"，但证据文件里**根本没记它**——
    于是 `verify` 无从判断有没有"幂等重放被当成新交易记账"。
    记录调用不等于记录证据：账目要对得上，就得把幂等键和重放标记一起落盘。
    """
    _fake_httpx(monkeypatch)
    cobo = FakeCobo()

    paid = buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)
    denied = buy(cobo, "http://127.0.0.1:8001", PREMIUM, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert paid.request_id == cobo.payments[0]["request_id"]
    assert paid.idempotent is False
    # 被拒那笔也要能看出它对应哪次请求（否则同一时段的两笔分不清）
    assert denied.request_id == cobo.payments[1]["request_id"]
    assert denied.idempotent is False


def test_outcome_flags_an_idempotent_replay(monkeypatch):
    """Cobo 回 `idempotent=true` 时，结果必须如实标记——那**不是**一笔新交易。"""
    _fake_httpx(monkeypatch)
    cobo = FakeCobo(replay=True)

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.idempotent is True


# ---- B3（第 7 步评审阻断项）：付款**没结算成功**时不许记成 paid ----
def _fake_httpx_with_broken_replay(
    monkeypatch, *, replay_status: int = 500, settlement_header: str | None = None
):
    """重放阶段坏掉的收款方：Cobo 那边没抛异常，但重放拿不到数据/结算。

    这正是**静默降级**的入口：只要 `buy()` 不看重放的状态码，它就会把这种情况
    记成 "paid" —— 于是 A4 的链上复核被整段跳过，屏幕上还打印 "✅ 支付成功"。
    """
    def fake_get(url: str, headers=None, timeout=None):  # noqa: ANN001
        if not (headers or {}).get("PAYMENT-SIGNATURE"):
            return httpx.Response(
                402,
                headers={"PAYMENT-REQUIRED": _challenge_header(CHEAP_ATOMIC)},
                json={"error": "PAYMENT-SIGNATURE header is required"},
                request=httpx.Request("GET", url),
            )
        hdrs = {}
        if settlement_header is not None:
            hdrs["PAYMENT-RESPONSE"] = settlement_header
        return httpx.Response(
            replay_status,
            headers=hdrs,
            json={"error": "settlement failed"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)


def test_replay_failure_is_not_recorded_as_paid(monkeypatch):
    """**核心**：Cobo 扣款没抛异常、但重放拿不到 200 ⇒ 绝不能记成 `paid`。

    评审的反证实测：修复前这里会得到 `outcome='paid' / http_status=500 / data=None /
    settlement_tx=None`，而且 demo 的 A4 守卫（`and outcome.settlement_tx`）会**整段跳过**
    链上复核，进程退出码仍是 0 —— 屏幕上一切正常，但钱到底动没动**没人验过**。
    """
    _fake_httpx_with_broken_replay(monkeypatch, replay_status=500)
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.outcome != "paid", "重放 500 却记成了 paid —— 静默降级"
    assert outcome.settlement_tx is None
    assert outcome.data is None


def test_paid_requires_a_settlement_tx(monkeypatch):
    """200 但没有 `PAYMENT-RESPONSE`（= 没有结算凭据）也不算 paid。

    触发场景：收款方中间件版本变了 / 头被改名 / facilitator settle 失败仍回 200。
    没有结算哈希就无法做链上复核 —— 那这笔"成功"是不可验证的。
    """
    _fake_httpx_with_broken_replay(monkeypatch, replay_status=200, settlement_header=None)
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.outcome != "paid", "没有结算哈希却记成了 paid —— 这笔成功无法验证"
    assert outcome.settlement_tx is None


def test_replay_failure_carries_a_machine_readable_reason(monkeypatch):
    """失败也要有结构化原因，不能只是"没成功"——否则看板上没法解释。"""
    _fake_httpx_with_broken_replay(monkeypatch, replay_status=500)
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.denial is not None
    assert outcome.denial["code"] == "PAYMENT_NOT_SETTLED"
    assert "500" in json.dumps(outcome.denial, ensure_ascii=False)


def test_a_still_paid_purchase_keeps_working_after_the_guard(monkeypatch):
    """反向守卫：正常那笔**必须仍然是 paid**，别把守卫加成了"永远不成功"。"""
    _fake_httpx(monkeypatch)
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.outcome == "paid"
    assert outcome.settlement_tx == CHEAP_TX
    assert outcome.http_status == 200


def test_a_non_200_replay_with_a_settlement_header_is_still_not_paid(monkeypatch):
    """**重放不是 200 ⇒ 绝不记 `paid`，哪怕响应里带了结算哈希。**

    第 4 轮自查变异发现：把

        `if replay.status_code != 200 or not settlement_tx:`

    改成

        `if not settlement_tx:`

    时 **276 条测试全绿** —— 状态码那一半判据**没有任何测试**。

    这个洞比看起来严重：一笔 "HTTP 500 但响应里恰好带了 `PAYMENT-RESPONSE`" 的付款
    会被记成 `paid`，屏幕上打印 "✅ 支付成功"，A4 链上复核拿到那个哈希去查、
    如果碰巧查得到，整个演示就会声称一笔**结算失败**的付款成功了。
    已有测试只覆盖 `settlement_header=None` 的那种（那时 `not settlement_tx` 也拦得住），
    **区分不出两半判据**。
    """
    _fake_httpx_with_broken_replay(
        monkeypatch,
        replay_status=500,
        settlement_header=_settlement_header("0x" + "ab" * 32),
    )
    cobo = FakeCobo()

    outcome = buy(
        cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY
    )

    assert outcome.outcome != "paid", (
        f"HTTP 500 且带结算哈希的付款被记成了 {outcome.outcome!r} —— "
        "状态码那一半判据没有生效（B3 未修完）"
    )
    assert outcome.outcome == "failed"
    assert "500" in json.dumps(outcome.denial or {}, ensure_ascii=False)


def test_a_non_402_with_a_payment_header_is_still_refused(monkeypatch):
    """**状态码是 200、但带着 `PAYMENT-REQUIRED`** ⇒ 仍然必须报错。

    第 5 轮自查变异发现：把

        `if challenge_response.status_code != 402 or not challenge:`

    改成

        `if not challenge:`

    时 **290 条测试全绿**。已有那条测试（`..._never_asked_for_payment`）用的是
    "200 且**没有** header"，那种情况 `not challenge` **也拦得住**，
    所以它区分不出两半判据 —— 状态码那一半**没有任何测试**。

    这个组合不是假想：一个把付费墙挂错位置的收款方完全可能返回
    `200 OK` 同时带上一个 `PAYMENT-REQUIRED`（比如中间件顺序反了、
    或者挑战头被别的层透传出来）。那时若照旧往下走签名流程，
    agent 会为一笔**本来免费**的响应去付款。
    """
    def fake_get_200_with_challenge(url: str, headers=None, timeout=None):  # noqa: ANN001
        return httpx.Response(
            200,
            headers={"PAYMENT-REQUIRED": _challenge_header(CHEAP_ATOMIC)},
            json={"report_id": "cheap-raw-001"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get_200_with_challenge)
    cobo = FakeCobo()

    with pytest.raises(RuntimeError, match="402"):
        buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert cobo.payments == [], "HTTP 200 却仍然发起了付款 —— 状态码判据没有生效"


def test_a_402_without_the_challenge_header_is_refused(monkeypatch):
    """另一半：**402 但没有 `PAYMENT-REQUIRED`** ⇒ 也必须报错。

    与上一条合起来，两半判据各自被单独钉住（`or` 的两侧都有人管）。
    """
    def fake_get_402_without_header(url: str, headers=None, timeout=None):  # noqa: ANN001
        return httpx.Response(
            402, json={"error": "payment required"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", fake_get_402_without_header)
    cobo = FakeCobo()

    with pytest.raises(RuntimeError, match="402"):
        buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert cobo.payments == []


# ---- 结算哈希的**形状**校验（第 5 轮评审 MINOR）----
# `settlement_tx` 是从收款方的 `PAYMENT-RESPONSE` 头里原样取出的，之前不校验形状
# 就记成"已结算"。收款方回一个 `"ok"` 或随便一段字符串都会被写进证据当成链上交易。
# demo 路径上 A4 会拿它去 RPC 复核、兜住了；但**探针路径没有 A4**。


def test_a_non_hash_settlement_value_is_not_a_hash():
    from pactpay.agent import looks_like_a_tx_hash

    real = "0x" + "ab" * 32
    assert looks_like_a_tx_hash(real) is True

    for bogus in ["ok", "", "0x1", "not-a-hash", None, 123, "0x" + "zz" * 32, "ab" * 32]:
        assert looks_like_a_tx_hash(bogus) is False, f"{bogus!r} 不该被当成交易哈希"


def test_a_payment_whose_response_carries_a_bogus_hash_is_not_recorded_as_paid(monkeypatch):
    """**核心断言**：形状不对的"哈希"不能让它变成 paid。

    判据是"真的结算了"，不是"收款方回了个非空字符串"。
    形状不对 ⇒ 这笔付款**没有被验证过** ⇒ 必须是 `failed`，不是 `paid`。
    """
    from pactpay.agent import buy

    # 收款方回一个**看着非空、其实不是哈希**的值
    _fake_httpx(monkeypatch, settlement_tx="ok")
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP, wallet_uuid=WALLET_UUID, pact_api_key=PACT_KEY)

    assert outcome.outcome != "paid", "形状不对的哈希竟然被记成了 paid"
    assert outcome.settlement_tx is None


# ---------------------------------------------------------------------------
# 重试与幂等（第 7 项收尾）：重试**必须复用同一个 request_id**
# ---------------------------------------------------------------------------


def _flaky_then_ok(monkeypatch, *, fail_times: int, fail_status: int = 503):
    """收款方先坏 `fail_times` 次，之后正常。用来测"重试后成功"。"""
    state = {"n": 0}

    def fake_get(url: str, headers=None, timeout=None):  # noqa: ANN001
        if not (headers or {}).get("PAYMENT-SIGNATURE"):
            return httpx.Response(
                402, headers={"PAYMENT-REQUIRED": _challenge_header(CHEAP_ATOMIC)},
                json={}, request=httpx.Request("GET", url),
            )
        state["n"] += 1
        if state["n"] <= fail_times:
            return httpx.Response(
                fail_status, json={"error": "boom"}, request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200, headers={"PAYMENT-RESPONSE": _settlement_header(CHEAP_TX)},
            json={"report_id": "cheap-raw-001"}, request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    return state


def test_retries_reuse_the_same_request_id(monkeypatch):
    """**这个模块最重要的一条。**

    重试时若换一个新的 `request_id`，Cobo 就不认得这是同一笔，
    **会真的再扣一次钱**。幂等键的全部意义就在于"重试复用同一个值"。
    """
    _flaky_then_ok(monkeypatch, fail_times=2)
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP,
                  wallet_uuid="w", pact_api_key="k", sleep=lambda _s: None)

    assert outcome.outcome == "paid", "重试后应当成功"
    ids = [p["request_id"] for p in cobo.payments]
    assert len(ids) == 3, f"应当尝试 3 次（失败 2 次 + 成功 1 次），实际 {len(ids)}"
    assert len(set(ids)) == 1, f"重试必须复用同一个 request_id，实际拿到 {ids}"
    assert outcome.request_id == ids[0], "结论里记的也必须是那个复用的 id"


def test_a_policy_denial_is_never_retried(monkeypatch):
    """策略拒绝是**确定性**的——重试只会把"硬边界"演成"偶发故障"。"""
    _fake_httpx(monkeypatch)
    denied = PolicyDenied(
        403, "X402_POLICY_DENIED", "x402_no_matching_payment",
        {"reject_details": [{"policy_reason": "matched_pact_transfer_deny_if", "policy_id": "p1"}]},
    )
    cobo = FakeCobo(deny_with=denied)

    outcome = buy(cobo, "http://127.0.0.1:8001", PREMIUM,
                  wallet_uuid="w", pact_api_key="k", sleep=lambda _s: None)

    assert outcome.outcome == "denied"
    assert len(cobo.payments) == 1, f"策略拒绝只该试 1 次，实际 {len(cobo.payments)} 次"


def test_a_missing_402_challenge_is_never_retried(monkeypatch):
    """收款方没收钱就给东西 ⇒ 配置错误，重试无意义（且重试会掩盖它）。"""
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):  # noqa: ANN001
        calls["n"] += 1
        return httpx.Response(200, json={"free": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(RuntimeError, match="期望 402"):
        buy(FakeCobo(), "http://127.0.0.1:8001", CHEAP,
            wallet_uuid="w", pact_api_key="k", sleep=lambda _s: None)

    assert calls["n"] == 1, f"这类错误不该重试，实际请求了 {calls['n']} 次"


def test_backs_off_between_attempts(monkeypatch):
    """重试之间必须等待——否则就是把瞬时故障打成连环请求。"""
    _flaky_then_ok(monkeypatch, fail_times=2)
    waits: list[float] = []

    buy(FakeCobo(), "http://127.0.0.1:8001", CHEAP,
        wallet_uuid="w", pact_api_key="k", sleep=waits.append)

    assert waits == [1.0, 3.0], f"两次重试前应分别等 1s / 3s，实际 {waits}"


def test_exhausted_retries_keep_the_machine_readable_code(monkeypatch):
    """重试用尽 ⇒ `failed`，说清试了几次，**且机器可读字段一个不丢**。

    `make verify` 和看板都靠 `denial["code"]` 判读；退化成一句自由文本就把它们废了。
    """
    _fake_httpx_with_broken_replay(monkeypatch, replay_status=503)
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP,
                  wallet_uuid="w", pact_api_key="k", sleep=lambda _s: None)

    assert outcome.outcome == "failed"
    assert outcome.denial is not None
    assert outcome.denial["code"] == "PAYMENT_NOT_SETTLED", "机器可读的 code 不能丢"
    assert "重试 3 次" in outcome.denial["reason"], f"要说清试了几次：{outcome.denial['reason']}"
    assert len(cobo.payments) == 3
    assert len({p["request_id"] for p in cobo.payments}) == 1, "失败路径也必须复用同一个 id"


def test_a_4xx_replay_is_not_retried(monkeypatch):
    """4xx 是确定性的（比如参数不对），重试无意义。"""
    _fake_httpx_with_broken_replay(monkeypatch, replay_status=400)
    cobo = FakeCobo()

    outcome = buy(cobo, "http://127.0.0.1:8001", CHEAP,
                  wallet_uuid="w", pact_api_key="k", sleep=lambda _s: None)

    assert outcome.outcome == "failed"
    assert len(cobo.payments) == 1, f"4xx 不该重试，实际 {len(cobo.payments)} 次"
