"""`parse_denial` 的单测。

为什么这个函数值得单测：R5"结构化拒绝原因"整条需求都压在它身上——
它解析不出来的话，被拒的那笔就退化成一句普通报错，Dashboard 也就拿不到
"触发了哪条策略"（policy_id）。期望值全部是**手写字面量**，不是用被测代码算出来的。
"""

from __future__ import annotations

import pytest

from pactpay.cobo import CoboClient, PolicyDenied, parse_denial

# 真实响应体的逐字副本（2026-10-10 对 Cobo 生产实测拿到的那一笔）。
# 注意它是**嵌在 error 下**的，官方文档里那个扁平示例是错的。
REAL_403 = {
    "success": False,
    "error": {
        "code": "TRANSFER_LIMIT_EXCEEDED",
        "reason": "matched_pact_transfer_deny_if",
        "details": {
            "reason": "matched_pact_transfer_deny_if",
            "chain_id": "TBASE_SETH",
            "token_id": "TBASE_USDC",
            "dst_addr": "0x1111111111111111111111111111111111111111",
            "tier": "pact",
            "policy_type": "transfer",
            "policy_id": "5fba2db7-a47b-42d8-bea4-59aff0715aeb",
        },
    },
    "suggestion": "Operation denied by the pact's policy (policy_id: 5fba2db7-...).",
    "api_request_uuid": "3afe25f7-c2e0-45a1-a45e-f182ab3bf77e",
}


def test_parses_the_real_nested_shape():
    denial = parse_denial(403, REAL_403)

    assert denial is not None
    assert denial.status == 403
    assert denial.code == "TRANSFER_LIMIT_EXCEEDED"
    assert denial.reason == "matched_pact_transfer_deny_if"
    assert denial.details["policy_id"] == "5fba2db7-a47b-42d8-bea4-59aff0715aeb"
    assert denial.details["tier"] == "pact"
    assert denial.suggestion.startswith("Operation denied by the pact's policy")


def test_parses_the_flat_shape_from_the_docs():
    """官方文档里那个扁平示例——两种形状都要吃，因为文档和实测不一致。"""
    denial = parse_denial(403, {"code": "POLICY_DENIED", "reason": "nope", "details": {"a": 1}})

    assert denial is not None
    assert denial.code == "POLICY_DENIED"
    assert denial.details == {"a": 1}


def test_details_is_empty_dict_not_none_when_absent():
    """details 缺失时必须是 {}——否则下游 `details["policy_id"]` 会 TypeError。"""
    denial = parse_denial(403, {"error": {"code": "POLICY_DENIED"}})

    assert denial is not None
    assert denial.details == {}


@pytest.mark.parametrize(
    "body",
    [
        {"error": "Not Found"},          # 非结构化 403（实测见过这种）
        {"success": False},               # 没有 code
        {"error": {"code": ""}},          # code 是空串
        {"error": {"code": None}},        # code 是 None
        {},                               # 空体
    ],
)
def test_returns_none_for_unstructured_403(body):
    """非结构化的 403 必须返回 None，这样调用方才会退回去抛普通错误——
    而不是伪造一个 code 为空的 PolicyDenied 让上层以为拿到了结构化拒绝。"""
    assert parse_denial(403, body) is None


def test_client_raises_typed_denial_and_keeps_raw_body(monkeypatch):
    """端到端：客户端在 403 时要抛 PolicyDenied，且把**逐字**响应体带出来（A0c 要它当证据）。"""
    import httpx

    def fake_request(self, method, url, **kwargs):
        return httpx.Response(403, json=REAL_403, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "request", fake_request)

    client = CoboClient(base_url="https://example.invalid", api_key="caw_test")
    with pytest.raises(PolicyDenied) as excinfo:
        client.transfer("wallet-uuid", {"amount": "0.005"})

    denial = excinfo.value
    assert denial.code == "TRANSFER_LIMIT_EXCEEDED"
    assert denial.raw == REAL_403  # 逐字留档，不是摘要
    assert client.calls[0].status == 403
    assert client.calls[0].request_body == {"amount": "0.005"}


# 2026-10-12 实测逐字抄录：**累计窗口**拒绝的响应形状与 per-tx 完全不同。
# 它**没有 `code` 字段**，只有 `reason: counter_limit_exceeded`，
# 而且 `limit_value` / `current_total` / `increment` 在这里**是存在的**
# （per-tx 那条路径里反而不存在）。
REAL_COUNTER_DENIAL = {
    "success": False,
    "error": {
        "reason": "counter_limit_exceeded",
        "window": "daily",
        "metric": "value",
        "limit_value": "0.015",
        "current_total": "0.015",
        "increment": "0.005",
    },
    "suggestion": "",
    "api_request_uuid": "76fcacf3-0535-44a7-9608-4a2f17b0fa8e",
}


def test_parses_the_counter_window_denial_shape():
    """累计窗口拒绝必须被识别成结构化拒绝，**不能当成普通 403 丢掉**。

    之前 `parse_denial` 硬性要求有 `code` 字段，于是这条真实存在的策略拒绝
    被降级成一个通用 `CoboError`——调用方再也拿不到 `limit_value`/`current_total`，
    A5b 只能靠"N>1"这种弱启发式去猜。实测证明它有明确的结构，应当被解析出来。
    """
    denied = parse_denial(403, REAL_COUNTER_DENIAL)

    assert denied is not None
    assert denied.code == "COUNTER_LIMIT_EXCEEDED"
    assert denied.reason == "counter_limit_exceeded"
    assert denied.details["limit_value"] == "0.015"
    assert denied.details["current_total"] == "0.015"
    assert denied.details["window"] == "daily"


def test_counter_denial_keeps_the_fields_that_prove_it_is_cumulative():
    """这几个字段正是"这是累计拒绝、不是 per-tx 拒绝"的证据，必须留住。"""
    denied = parse_denial(403, REAL_COUNTER_DENIAL)

    assert denied is not None
    assert denied.details["increment"] == "0.005"
    assert denied.details["metric"] == "value"
