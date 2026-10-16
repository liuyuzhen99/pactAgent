"""收款方：两个"付费 API"（T2）。

这是演示里的**第三方卖家**——它不关心你谁、也不给你注册账号，只认 x402 付款。
没付钱就回 `402 Payment Required` 并把支付要求放在 `PAYMENT-REQUIRED` 头里（v2 协议）。

两个端点价格故意拉开：
  /api/cheap    $0.005  —— 便宜的数据接口（demo 里这笔应该成功）
  /api/premium  $0.05   —— 贵的分析接口（demo 里这笔应该被 Pact 单笔上限拒掉）

网络只支持 Base Sepolia（`eip155:84532`）：免费的公开 facilitator 只支持它，
所以不能靠改链绕开 Base Sepolia 领币那一步。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient
from x402.http.middleware import fastapi_payment_middleware
from x402.mechanisms.evm.exact import ExactEvmServerScheme

# Base Sepolia（v2 用 CAIP-2 写法；v1 的 "base-sepolia" 是旧的）
NETWORK = "eip155:84532"
# 免费的公开 facilitator：不需要注册、不需要 API key，但**只支持测试网**
FACILITATOR_URL = "https://x402.org/facilitator"

# 两个"数据产品"。`report_id` 是给验收断言用的具体字段——
# A4 要求"真的取回了数据正文"，所以必须能断言到一个**具体的值**，
# 而不是"body 非空"（错误页也是非空的）。
CHEAP_REPORT: dict[str, Any] = {
    "report_id": "cheap-raw-001",
    "kind": "raw_data",
    "records": 42,
    "note": "原始运营数据（便宜接口）",
}
PREMIUM_REPORT: dict[str, Any] = {
    "report_id": "premium-analysis-001",
    "kind": "analysis",
    "insights": 7,
    "note": "深度分析结论（贵接口）",
}


#: 路径 → 该路径交付的报告。**把它提成模块级常量是有意的**（评审第 3 轮 #10）：
#: 原来两个 handler 各自 `return CHEAP_REPORT` / `return PREMIUM_REPORT`，
#: 而付费成功路径只能靠真金白银的端到端命令走到 —— 于是"哪个路径交付哪份报告"
#: 在这组离线单测里**完全没有覆盖**：把它俩对调，233 条测试照样全绿。
#: 现在映射是一份**可离线断言的数据**，handler 只是查表。
REPORTS: dict[str, dict[str, Any]] = {
    "GET /api/cheap": CHEAP_REPORT,
    "GET /api/premium": PREMIUM_REPORT,
}


def build_routes(pay_to: str) -> dict[str, Any]:
    """x402 的路由配置。价格写在每个路由自己的 accepts 里（v2 的写法）。"""

    def entry(price: str, description: str) -> dict[str, Any]:
        return {
            "accepts": {
                "scheme": "exact",
                "payTo": pay_to,
                "price": price,
                "network": NETWORK,
            },
            "description": description,
            "mimeType": "application/json",
        }

    return {
        "GET /api/cheap": entry("$0.005", "原始运营数据"),
        "GET /api/premium": entry("$0.05", "深度分析结论"),
    }


def create_app(pay_to: str | None = None) -> FastAPI:
    pay_to = pay_to or os.environ.get("PAY_TO_ADDRESS", "")
    if not pay_to:
        raise RuntimeError("缺少收款地址：设置 PAY_TO_ADDRESS 或调用 create_app(pay_to=...)")

    app = FastAPI(title="PactPay payee", version="1.0.0")

    facilitator = HTTPFacilitatorClient({"url": FACILITATOR_URL})
    server = x402ResourceServer(facilitator).register(NETWORK, ExactEvmServerScheme())

    # 只构建一次。官方 docstring 的示例是在每个请求里调一次 `fastapi_payment_middleware(...)`，
    # 但那会每次新建一个 x402HTTPResourceServer，把"是否已向 facilitator 初始化"的状态丢掉。
    # 构建一次、复用它，语义相同且不丢状态。
    middleware = fastapi_payment_middleware(build_routes(pay_to), server)

    @app.middleware("http")
    async def x402_middleware(request, call_next):  # type: ignore[no-untyped-def]
        return await middleware(request, call_next)

    @app.get("/api/cheap")
    async def cheap() -> dict[str, Any]:
        return REPORTS["GET /api/cheap"]

    @app.get("/api/premium")
    async def premium() -> dict[str, Any]:
        return REPORTS["GET /api/premium"]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "network": NETWORK, "pay_to": pay_to}

    return app


def main() -> None:  # pragma: no cover - 手工起服务用
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()  # PAY_TO_ADDRESS / PAYEE_PORT 从 .env 读

    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=int(os.environ.get("PAYEE_PORT", "8001")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
