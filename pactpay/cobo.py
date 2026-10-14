"""Cobo Agentic Wallet 的薄客户端。

为什么用 httpx 而不是 `cobo-agentic-wallet` SDK：
方案原本选 SDK，但 T0/T5/T7 的验收标准**反复要求"逐字请求体 + 逐字响应 JSON"**作为证据
（A0c/A0e 明写"不是写结论"）。SDK 返回的是解析后的对象，拿不到原始字节。
这里用 httpx 直连同一套 REST 端点，把每次调用的**请求体与响应体原样留档**，
既满足证据要求，也只需要一层传输（避免两套并存的重复代码）。

端点的路径与请求体字段全部照官方文档的 OpenAPI 段核对过，不是猜的：
  POST /api/v1/principals/provision            （匿名即可，返回 agent 凭证）
  POST /api/v1/wallets                         （wallet_type=Custodial 免 TSS 节点）
  POST /api/v1/wallets/{uuid}/addresses
  GET  /api/v1/faucet/tokens
  POST /api/v1/pacts/submit
  GET  /api/v1/pacts/{pact_id}
  POST /api/v1/wallets/{uuid}/transfer
  GET  /api/v1/audit-logs
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.agenticwallet.cobo.com"


@dataclass
class Call:
    """一次调用的逐字留档——证据就是它。"""

    method: str
    path: str
    request_body: Any
    status: int
    response_body: Any

    def as_evidence(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "request_body": self.request_body,
            "status": self.status,
            "response_body": self.response_body,
        }


@dataclass
class PolicyDenied(Exception):
    """403 策略拒绝。

    真实响应体把细节嵌在 `error` 下（`{"success":false,"error":{code,reason,details}}`），
    而官方文档里那个扁平示例是错的——所以这里两种形状都吃。
    字段名照实测：code / reason / details / suggestion。
    SDK 里对应的类是 `cobo_agentic_wallet.PolicyDenial`。
    """

    status: int
    code: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 只为可读
        return f"PolicyDenied(status={self.status}, code={self.code}, reason={self.reason})"


class CoboError(Exception):
    """非 403 的失败响应。"""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {json.dumps(body, ensure_ascii=False)[:400]}")


def parse_denial(status: int, body: dict[str, Any]) -> PolicyDenied | None:
    """解析结构化拒绝；非结构化的 403 返回 None。"""
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    reason = error.get("reason", "") or ""
    if not isinstance(code, str) or not code:
        # 实测（2026-10-12）存在**第二种形状**：累计窗口拒绝**没有 `code` 字段**，
        # 只有 `reason: "counter_limit_exceeded"` 以及 window/metric/limit_value/
        # current_total/increment。若在这里 `return None`，它就被降级成一个通用
        # `CoboError`，调用方再也拿不到 limit_value/current_total——而那正是
        # "这是累计拒绝、不是 per-tx 拒绝"的证据。
        if reason == "counter_limit_exceeded":
            code = "COUNTER_LIMIT_EXCEEDED"
        else:
            return None
    details = error.get("details")
    if not isinstance(details, dict):
        # 第二种形状把字段**平铺在 error 上**（没有 details 子对象）。
        # 原样保留（除了已单独取用的 code/reason），别丢字段。
        details = {k: v for k, v in error.items() if k not in ("code", "reason")}
    return PolicyDenied(
        status=status,
        code=code,
        reason=reason,
        details=details,
        suggestion=body.get("suggestion", "") or error.get("suggestion", "") or "",
        raw=body,
    )


class CoboClient:
    """直连 Cobo Agentic Wallet REST API。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.calls: list[Call] = []
        self._http = httpx.Client(base_url=self.base_url, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CoboClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 传输层 ----

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
        expect_error: bool = False,
    ) -> Any:
        """发一次请求并把请求/响应逐字留档。

        expect_error=True 时允许 4xx 并原样返回响应体（探针要的就是那个 403）。
        """
        headers = {}
        key = api_key if api_key is not None else self.api_key
        if key:
            headers["X-API-Key"] = key

        response = self._http.request(method, path, json=body, headers=headers)
        try:
            parsed = response.json()
        except ValueError:
            parsed = {"_raw_text": response.text}

        self.calls.append(Call(method, path, body, response.status_code, parsed))

        if response.status_code >= 400 and not expect_error:
            denial = parse_denial(response.status_code, parsed)
            if denial is not None:
                raise denial
            raise CoboError(response.status_code, parsed)
        return parsed

    # ---- T0 用到的端点 ----

    def provision_agent(self, name: str) -> dict[str, Any]:
        """匿名创建一个 agent，拿回 api_key。不需要账号/邀请码。"""
        return self.request("POST", "/api/v1/principals/provision", {"name": name})

    def create_wallet(self, name: str, wallet_type: str = "Custodial") -> dict[str, Any]:
        """建钱包。Custodial 免 TSS/MPC 节点，纯 HTTP 即可。"""
        return self.request("POST", "/api/v1/wallets", {"wallet_type": wallet_type, "name": name})

    def create_address(self, wallet_uuid: str, chain_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/wallets/{wallet_uuid}/addresses", {"chain_id": chain_id})

    def list_faucet_tokens(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/faucet/tokens")


    def search_tokens(self, symbol: str) -> dict[str, Any]:
        """按 symbol 搜代币——文档说拿它返回的 token_id 去做转账/查余额。"""
        return self._get("/api/v1/metadata/tokens/search", {"symbol": symbol})

    def submit_pact(self, wallet_id: str, intent: str, spec: dict[str, Any], name: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/pacts/submit",
            {"wallet_id": wallet_id, "intent": intent, "name": name, "spec": spec},
        )

    def get_pact(self, pact_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/pacts/{pact_id}")

    def revoke_pact(self, pact_id: str) -> dict[str, Any]:
        """**撤销**一份 Pact（`make revoke-check` 用它）。

        这是"人类随时能收权"的落点，而且它在**平台侧**——不在 Agent 进程里。
        这一点很要紧：如果 kill switch 实现在我们自己的循环里，一个被攻破或
        幻觉驱动的 Agent 直接跳过那个判断就行了；撤销 Pact 则绕不过去，
        因为**后续每一次签名都要经过 Cobo**。

        撤销后这个 Pact 的 api_key 立刻什么都干不了（下一个签名请求会被拒）。
        """
        return self.request("POST", f"/api/v1/pacts/{pact_id}/revoke", {})

    def list_pact_events(self, pact_id: str, **params: Any) -> dict[str, Any]:
        """某一 Pact 的事件列表（普通 GET，不是 SSE 流）。"""
        return self._get(f"/api/v1/pacts/{pact_id}/events", {k: v for k, v in params.items() if v is not None})

    def event_stream(self, *, timeout_s: float = 30.0, accept: str = "text/event-stream"):
        """订阅**服务端事件流**（SSE），逐条 yield `(event_type, data_dict)`。

        用 `httpx.stream` 而不是普通请求——SSE 是**长连接**，普通请求会一直挂着
        等响应结束（永远不会结束）。

        这是真正的实时告警通道，而且它是**平台推给我们的**：
        `policy.violated` 事件带 `details.denial_code`，发生在我们这边发请求的**同时**，
        不依赖我们事后去审计日志里翻。

        `heartbeat` 每 30 秒一条（实测文档如此），调用方要容忍它。
        """
        url = f"{self.base_url}/api/v1/events/stream"
        headers = {"Accept": accept}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        with httpx.stream("GET", url, headers=headers, timeout=timeout_s) as response:
            response.raise_for_status()
            event_type = "message"
            data_lines: list[str] = []
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
                elif not line:
                    # 空行 = 一条事件结束
                    if data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            payload = json.loads(raw)
                        except ValueError:
                            payload = {"_raw": raw}
                        yield event_type, payload
                    event_type = "message"
                    data_lines = []

    def transfer(
        self,
        wallet_uuid: str,
        body: dict[str, Any],
        *,
        api_key: str | None = None,
        expect_error: bool = False,
    ) -> dict[str, Any]:
        return self.request(
            "POST", f"/api/v1/wallets/{wallet_uuid}/transfer", body, api_key=api_key, expect_error=expect_error
        )

    def list_balances(
        self, wallet_uuid: str, chain_id: str | None = None, token_id: str | None = None
    ) -> dict[str, Any]:
        """查余额。`GET /api/v1/wallets/balances`（按 wallet_uuid 过滤）。"""
        query: dict[str, Any] = {"wallet_uuid": wallet_uuid}
        if chain_id:
            query["chain_id"] = chain_id
        if token_id:
            query["token_id"] = token_id
        return self._get("/api/v1/wallets/balances", query)

    def list_audit_logs(self, **params: Any) -> dict[str, Any]:
        return self._get("/api/v1/audit-logs", {k: v for k, v in params.items() if v is not None})

    def payment(
        self,
        wallet_uuid: str,
        *,
        protocol: str,
        request_id: str,
        x402_payment_required: str | None = None,
        api_key: str | None = None,
        expect_error: bool = False,
    ) -> dict[str, Any]:
        """签名并提交一笔 x402 付款。

        `x402_payment_required` 是收款方 402 响应里 `PAYMENT-REQUIRED` 头的**原样 base64 串**。
        `request_id` 是幂等键：同一个值重复提交会**返回原始结果**（`idempotent: true`）
        而不会重复扣款——每次运行必须换新的，否则会把"重放"当成"又付了一笔"。

        注意 `PaymentResult.tx_hash` 只在 request_close / withdraw 时有值，
        **x402 付款它是 null**（官方 schema 原文如此）。所以"到底上没上链"要看
        `retry_headers` 重放后收款方回的 `PAYMENT-RESPONSE.transaction`，**不能看 `tx_hash`**。
        """
        body: dict[str, Any] = {"protocol": protocol, "request_id": request_id}
        if x402_payment_required is not None:
            body["x402_payment_required"] = x402_payment_required
        return self.request(
            "POST",
            f"/api/v1/wallets/{wallet_uuid}/payment",
            body,
            api_key=api_key,
            expect_error=expect_error,
        )

    def _get(self, path: str, query: dict[str, Any]) -> Any:
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        response = self._http.get(path, params=query, headers=headers)
        try:
            parsed = response.json()
        except ValueError:
            parsed = {"_raw_text": response.text}
        self.calls.append(Call("GET", path, query, response.status_code, parsed))
        if response.status_code >= 400:
            raise CoboError(response.status_code, parsed)
        return parsed
