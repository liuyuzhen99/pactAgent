"""T6 默认拒绝用例（可重跑版）的验收单测。

需求 R6：任何操作必须被某条策略明确覆盖才能执行，否则自动拒绝。

**为什么这条要单独成"可重跑的一步"**：之前只用临时脚本证明过一次事实，
那是"验过"，不是"交付"——交付意味着 `make demo` 每次都会产出它、
证据文件里有它、页面上看得到它。

期望值全部来自 2026-10-12 的实测（逐字抄录），不是照文档假设：
实测拒绝码是 `ADDRESS_NOT_WHITELISTED`，**不是** `POLICY_DENIED`、
**也不是** `TRANSFER_LIMIT_EXCEEDED`——因为 `when` 只白名单了商户地址，
fail-closed 的默认拒绝先生效。
"""

from __future__ import annotations

from pactpay.agent.pay import normalize_transfer_denial
from pactpay.platform.cobo import PolicyDenied

# 实测逐字抄录：向白名单外地址转账 0.001 的拒绝
REAL_DEFAULT_DENY = PolicyDenied(
    status=403,
    code="ADDRESS_NOT_WHITELISTED",
    reason="no_pact_transfer_allow_policy_matched",
    details={
        "reason": "no_pact_transfer_allow_policy_matched",
        "chain_id": "TBASE_SETH",
        "token_id": "TBASE_USDC",
        "dst_addr": "0x1111111111111111111111111111111111111111",
        "tier": "pact",
        "policy_type": "transfer",
        "denial_code": "ADDRESS_NOT_WHITELISTED",
        "requested_address": "0x1111111111111111111111111111111111111111",
    },
    suggestion="Destination address is not whitelisted.",
    raw={},
)

OUTSIDE_WHITELIST = "0x1111111111111111111111111111111111111111"


def test_default_deny_evidence_pins_the_measured_code():
    """拒绝码必须是实测出来的 `ADDRESS_NOT_WHITELISTED`。

    **不许写成析取集**（`in {POLICY_DENIED, ...}`）：析取集会让"拒绝码变了"
    这种回归静默通过，而拒绝码正是这个演示要展示的东西。
    """
    ev = normalize_transfer_denial(REAL_DEFAULT_DENY, requested_address=OUTSIDE_WHITELIST)

    assert ev["code"] == "ADDRESS_NOT_WHITELISTED"
    assert ev["reason"] == "no_pact_transfer_allow_policy_matched"
    assert ev["outcome"] == "denied"
    assert ev["requested_address"] == OUTSIDE_WHITELIST


def test_default_deny_evidence_proves_it_was_fail_closed():
    """要能一眼看出这是"默认拒绝"而非"命中某条 deny 规则"。

    `no_pact_transfer_allow_policy_matched` 的字面意思就是"没有任何允许策略匹配"，
    这是 fail-closed 生效的直接证据；而**没有** `policy_id` 也正因如此
    （本来就没有哪条策略被命中）。
    """
    ev = normalize_transfer_denial(REAL_DEFAULT_DENY, requested_address=OUTSIDE_WHITELIST)

    assert ev["reason"] == "no_pact_transfer_allow_policy_matched"
    assert ev["policy_id"] is None


def test_default_deny_evidence_leaves_no_chain_trace():
    """默认拒绝同样必须**没有链上痕迹**——这是"签名前拦住"的证据。"""
    ev = normalize_transfer_denial(REAL_DEFAULT_DENY, requested_address=OUTSIDE_WHITELIST)

    assert ev["settlement_tx"] is None


# ---- 探针本身（可重跑的那一步）----

class FakeClient:
    """记录调用并可选抛错，用来验探针的归一化行为（不起真实网络）。"""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[tuple] = []

    def transfer(self, wallet_uuid, body, api_key=None):  # noqa: ANN001
        self.calls.append((wallet_uuid, body, api_key))
        if self.exc:
            raise self.exc
        return {"unexpected": "allowed"}


def _probe(client):
    from pactpay.agent.pay import probe_default_deny

    return probe_default_deny(
        client,
        "wallet-1",
        pact_key="pact-key-1",
        chain_id="TBASE_SETH",
        token_id="TBASE_USDC",
        src_addr="0xagent",
        dst_addr=OUTSIDE_WHITELIST,
        amount="0.001",
    )


def test_probe_returns_a_normalized_denial_and_used_the_pact_key():
    client = FakeClient(exc=REAL_DEFAULT_DENY)

    ev = _probe(client)

    assert ev["outcome"] == "denied"
    assert ev["code"] == "ADDRESS_NOT_WHITELISTED"
    assert ev["settlement_tx"] is None
    # 必须用 **pact-scoped key**：用根 key 会路由到别的 pact，这条用例就失去意义了
    _, body, api_key = client.calls[0]
    assert api_key == "pact-key-1"
    assert body["dst_addr"] == OUTSIDE_WHITELIST


def test_probe_flags_loudly_if_the_transfer_was_actually_allowed():
    """若这笔竟然通过了，必须返回 `outcome="allowed"` 让调用方判失败。

    这是"不许把没报错当成通过"的具体落实：fail-closed 失效时，
    探针不能静默返回一个看起来正常的结果。
    """
    ev = _probe(FakeClient())

    assert ev["outcome"] == "allowed"
    assert ev["code"] is None
