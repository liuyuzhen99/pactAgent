"""Agent 的采购执行（T4/T5）。

Agent 的职责边界（见 `planner.py` 的说明）：**决定买什么、按什么顺序买、每笔花多少**。
它**不判断"能不能买"**——那是 Pact 策略层的事，agent 只是把 402 挑战原样交给 Cobo，
然后如实记录 Cobo 的答复（放行 / 结构化拒绝）。

一笔采购的真实时序：

    GET 资源 → 402 + PAYMENT-REQUIRED 挑战
             → Cobo /payment(protocol=x402, 挑战原样透传)   ← 策略引擎在这里拦
             → 放行：拿 retry_headers（含 PAYMENT-SIGNATURE）重放原请求 → 200 + 数据 + 结算哈希
             → 拒绝：抛 PolicyDenied → 记下结构化拒绝原因，**不留任何链上痕迹**
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from pactpay.cobo import CoboClient, PolicyDenied
from pactpay.planner import Purchase

HTTP_TIMEOUT_S = 30


@dataclass(frozen=True)
class PurchaseOutcome:
    """一笔采购的最终结果——Dashboard 与证据文件都读它。"""

    resource: str
    amount_usd: Decimal
    description: str
    outcome: str  # "paid" | "denied" | "failed"（未结算，见 agent.py 注释）
    http_status: int
    data: dict[str, Any] | None
    settlement_tx: str | None
    denial: dict[str, Any] | None
    # A7b：幂等键与"这是不是一次重放"。证据要能回答"这笔到底是不是新交易"，
    # 否则一次幂等重放会被当成新交易记账，账目就对不上了。
    request_id: str | None = None
    idempotent: bool = False


def _denial_as_evidence(denied: PolicyDenied) -> dict[str, Any]:
    """把两条路径的拒绝**归一化成同一组字段**。

    实测（2026-10-12）两条路径形状不一样：
    - **普通转账**：策略信息在顶层 `details` 里（`policy_type`/`policy_id`），
      code 直接就是 `TRANSFER_LIMIT_EXCEEDED`。
    - **x402 付款**：code 是笼统的 `X402_POLICY_DENIED`、reason 是
      `x402_no_matching_payment`，真正的策略信息被埋进 `details.reject_details[0]`
      （`reason` / `policy_reason` / `policy_id`）。

    归一化的理由不是"好看"：R9 要求 Dashboard 回答"触发了哪条策略"，
    让它自己认两种形状的话，多半只处理一种，另一条路径上就静默显示空白。
    """
    details = denied.details or {}
    rejects = details.get("reject_details") or []
    reject = rejects[0] if rejects and isinstance(rejects[0], dict) else {}
    return {
        "code": denied.code,
        "reason": denied.reason,
        "details": details,
        "suggestion": denied.suggestion,
        # 归一化：两条路径形状不同，统一提到顶层，Dashboard 才不用懂两种形状
        "reject_reason": reject.get("reason") or "",
        "policy_reason": reject.get("policy_reason") or denied.reason,
        "policy_id": reject.get("policy_id") or details.get("policy_id"),
        # **这条拒绝来自哪个 API** —— 决定它有没有审计行可以佐证。
        # 实测（2026-10-12）：`payment`（x402）路径的拒绝**根本不写审计日志**，
        # 只有 `transfer` 路径才写。所以这个字段是"能不能去审计里找证据"的开关，
        # 不是装饰。靠 `policy_reason` 字符串去猜会把两条路径混起来（见下方审计匹配）。
        "api": "payment",
    }


def normalize_transfer_denial(
    denied: PolicyDenied, *, requested_address: str | None = None
) -> dict[str, Any]:
    """把一次**普通转账**的拒绝归一化。

    与 `_denial_as_evidence()`（走 x402 付款路径）的区别：转账路径的策略信息
    **本来就在顶层 `details` 里**，不需要去挖 `reject_details`。两条路径归一化后
    字段一致，证据文件与页面就不必懂两种形状。

    "默认拒绝"这一类的特征：`reason` 是 `no_pact_transfer_allow_policy_matched`
    （字面意思"没有任何允许策略匹配"），且**没有 policy_id**——本来就没策略被命中。
    """
    details = denied.details or {}
    return {
        "outcome": "denied",
        "code": denied.code,
        "reason": denied.reason,
        "details": details,
        "suggestion": denied.suggestion,
        "reject_reason": details.get("reason") or "",
        "policy_reason": details.get("policy_reason") or denied.reason,
        "policy_id": details.get("policy_id"),
        "requested_address": (
            requested_address or details.get("requested_address") or details.get("dst_addr")
        ),
        # 被拒绝的转账没有链上痕迹——这是"签名前拦住"的直接证据
        "settlement_tx": None,
        # 转账路径的拒绝**会**进审计日志（`transfer.denied`），所以它可以去找审计行佐证。
        "api": "transfer",
    }


def probe_default_deny(
    client: Any,
    wallet_uuid: str,
    *,
    pact_key: str,
    chain_id: str,
    token_id: str,
    src_addr: str,
    dst_addr: str,
    amount: str,
) -> dict[str, Any]:
    """用一个**未被任何策略覆盖**的转账，实测默认拒绝是否真生效（R6）。

    为什么单独成一步而不是"验过一次就算"：R6 是项目要证明的命题之一，
    必须每次 `make demo` 都能重跑出来、进证据文件、进页面。

    返回值里的 `outcome` 是判据：`"denied"` = 默认拒绝生效（正常）；
    **`"allowed"` = fail-closed 没生效**，调用方**必须判失败**——
    不许当成"没报错所以通过"。
    """
    try:
        result = client.transfer(
            wallet_uuid,
            {
                "chain_id": chain_id,
                "token_id": token_id,
                "src_addr": src_addr,
                "dst_addr": dst_addr,
                "amount": amount,
            },
            api_key=pact_key,
        )
    except PolicyDenied as denied:
        return normalize_transfer_denial(denied, requested_address=dst_addr)
    return {
        "outcome": "allowed",
        "code": None,
        "reason": None,
        "details": {},
        "suggestion": None,
        "reject_reason": "",
        "policy_reason": None,
        "policy_id": None,
        "requested_address": dst_addr,
        "settlement_tx": None,
        "unexpected_result": result,
    }


def buy(
    client: CoboClient,
    payee_base: str,
    purchase: Purchase,
    *,
    wallet_uuid: str,
    pact_api_key: str,
) -> PurchaseOutcome:
    """买一笔。放行或拒绝都如实返回，不抛给调用方（`expect_error` 的语义在这里收口）。"""
    url = payee_base + purchase.resource

    challenge_response = httpx.get(url, timeout=HTTP_TIMEOUT_S)
    challenge = challenge_response.headers.get("PAYMENT-REQUIRED")
    if challenge_response.status_code != 402 or not challenge:
        # 收款方没收钱就给了东西 → 绝不能记成 paid（否则 Dashboard 会显示一笔
        # 根本没有链上交易的"成功支付"）。详见 tests/test_agent.py 里对应的用例。
        raise RuntimeError(
            f"{purchase.resource} 期望 402 + PAYMENT-REQUIRED，实际 "
            f"HTTP {challenge_response.status_code}（收款方的 x402 中间件可能没挂上）"
        )

    # 幂等键每笔都换：同一个值重放会返回旧结果而不重复扣款。
    request_id = str(uuid.uuid4())
    try:
        result = client.payment(
            wallet_uuid,
            protocol="x402",
            request_id=request_id,
            x402_payment_required=challenge,
            api_key=pact_api_key,
        )
    except PolicyDenied as denied:
        return PurchaseOutcome(
            resource=purchase.resource,
            amount_usd=purchase.amount_usd,
            description=purchase.description,
            outcome="denied",
            http_status=denied.status,
            data=None,
            settlement_tx=None,
            denial=_denial_as_evidence(denied),
            request_id=request_id,
        )

    inner = result.get("result") or {}
    retry_headers = inner.get("retry_headers") or {}
    replay = httpx.get(url, headers=retry_headers, timeout=HTTP_TIMEOUT_S)

    settlement_tx = None
    payment_response = replay.headers.get("PAYMENT-RESPONSE")
    if payment_response:
        try:
            # v2 的字段叫 transaction（v1 才叫 txHash）
            settlement_tx = json.loads(base64.b64decode(payment_response)).get("transaction") or None
        except (ValueError, TypeError) as exc:
            # 结算头坏了不能当"没这回事"——它意味着这笔付款**无法验证**
            return _not_settled(purchase, replay, request_id, f"PAYMENT-RESPONSE 解不开：{exc}")

    # **付款成功的判据是"真的结算了"，不是"Cobo 没抛异常"**（B3，第 7 步评审阻断项）。
    # 只要这里放宽成"没抛异常就算 paid"，就会静默降级：重放 500 也记 paid、
    # A4 的链上复核被整段跳过、屏幕上仍打印"✅ 支付成功"、退出码 0 —— 钱到底动没动没人验过。
    # 形状不对的"哈希"不是结算凭证（评审 MINOR）：不能因为收款方回了个非空字符串
    # 就把它当成链上交易记进证据。形状不对 ⇒ 这笔付款没有被验证过 ⇒ 不是 paid。
    if settlement_tx is not None and not looks_like_a_tx_hash(settlement_tx):
        return _not_settled(
            purchase, replay, request_id,
            f"PAYMENT-RESPONSE.transaction 不是合法的交易哈希：{settlement_tx!r}",
            settlement_tx=None,
        )

    if replay.status_code != 200 or not settlement_tx:
        reasons = []
        if replay.status_code != 200:
            reasons.append(f"重放返回 HTTP {replay.status_code}（不是 200）")
        if not settlement_tx:
            reasons.append("重放没有带回 PAYMENT-RESPONSE.transaction 结算哈希")
        return _not_settled(purchase, replay, request_id, "；".join(reasons), settlement_tx=settlement_tx)

    return PurchaseOutcome(
        resource=purchase.resource,
        amount_usd=purchase.amount_usd,
        description=purchase.description,
        outcome="paid",
        http_status=replay.status_code,
        data=replay.json(),
        settlement_tx=settlement_tx,
        denial=None,
        request_id=request_id,
        # Cobo 回 idempotent=true 表示这是**重放**、不是新交易——必须如实标记
        idempotent=bool(inner.get("idempotent")),
    )


def looks_like_a_tx_hash(value: Any) -> bool:
    """结算哈希必须是 `0x` + 64 位十六进制。

    **为什么要核**（第 5 轮评审 MINOR）：`settlement_tx` 是从收款方的
    `PAYMENT-RESPONSE` 头里**原样取出**的，之前不校验形状就记成"已结算"。
    收款方（或中间人）回一个 `"ok"` / `"0x1"` / 一段随便的字符串，
    都会被写进证据当成链上交易哈希。

    demo 路径上 A4 会拿它去 RPC 复核、从而兜住；但**探针路径没有 A4**，
    于是一个假哈希会一路记进证据。这里在**入口**就把它挡掉：
    形状不对 = 拿不到可验证的结算凭证 = 这笔付款**没被验证过**，不是 paid。
    """
    if not isinstance(value, str):
        return False
    if not value.startswith("0x") or len(value) != 66:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _not_settled(
    purchase: Purchase,
    replay: httpx.Response,
    request_id: str,
    reason: str,
    *,
    settlement_tx: str | None = None,
) -> PurchaseOutcome:
    """付款没能结算：**不是** `paid`，也**不是**策略拒绝，而是第三种结果。

    单独立一个 `outcome="failed"` 而不是塞进 `denied`，是因为两者含义不同：
    `denied` = 策略在签名前挡住了（钱没动，是**预期**结果）；
    `failed` = 策略放行了但没结算成功（钱可能动了一半、或根本没动，是**异常**结果）。
    混在一起会让看板把"基础设施故障"读成"边界生效"，正好把这个项目要证明的事说反。
    """
    return PurchaseOutcome(
        resource=purchase.resource,
        amount_usd=purchase.amount_usd,
        description=purchase.description,
        outcome="failed",
        http_status=replay.status_code,
        data=None,
        settlement_tx=settlement_tx,
        denial={
            "code": "PAYMENT_NOT_SETTLED",
            "reason": reason,
            "policy_reason": None,
            "policy_id": None,
            "http_status": replay.status_code,
        },
        request_id=request_id,
    )
