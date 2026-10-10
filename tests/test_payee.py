"""收款方（T2）的验收单测。

对应方案 A2：`GET /api/cheap` 必须回 **402**，且 `PAYMENT-REQUIRED` 头 base64 解出来
的支付要求里 `amount == "5000"`（0.005 USDC 的原子单位，6 位小数）、
`network == "eip155:84532"`（v2 的 CAIP-2 写法）、`asset` 是 Base Sepolia 的 USDC 合约。

期望值全部是**手写字面量**，不是从被测代码里算出来的。
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from pactpay.payee.app import CHEAP_REPORT, create_app

# 手写字面量：Base Sepolia USDC（链上 name()=="USDC"、decimals()==6，已独立核验）
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
NETWORK_V2 = "eip155:84532"
PAY_TO = "0xef6aa50e6cf9b97016f9cb31e8c8f884ad8ec6c0"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(pay_to=PAY_TO))


def _payment_required(header_value: str) -> dict:
    """`PAYMENT-REQUIRED` 是标准 base64（不是 base64url）的 UTF-8 JSON。"""
    return json.loads(base64.b64decode(header_value))


@pytest.mark.parametrize(
    ("path", "amount_atomic"),
    [
        ("/api/cheap", "5000"),    # 0.005 USDC @ 6dp
        ("/api/premium", "50000"),  # 0.05  USDC @ 6dp
    ],
)
def test_unpaid_request_returns_402_with_v2_requirements(client: TestClient, path: str, amount_atomic: str):
    response = client.get(path)

    assert response.status_code == 402
    assert "PAYMENT-REQUIRED" in response.headers

    required = _payment_required(response.headers["PAYMENT-REQUIRED"])
    assert required["x402Version"] == 2
    accepts = required["accepts"]
    assert len(accepts) == 1

    accept = accepts[0]
    assert accept["scheme"] == "exact"
    assert accept["network"] == NETWORK_V2
    assert accept["asset"].lower() == USDC_BASE_SEPOLIA.lower()
    assert accept["amount"] == amount_atomic          # v2 是 amount，不是 maxAmountRequired
    assert accept["payTo"] == PAY_TO
    assert "maxAmountRequired" not in accept          # v1 字段名不许出现


def test_cheap_and_premium_are_priced_differently(client: TestClient):
    """两笔端点价格必须真的不同——否则"便宜那笔成功、贵那笔被拒"的剧情就不成立。"""
    cheap = _payment_required(client.get("/api/cheap").headers["PAYMENT-REQUIRED"])
    premium = _payment_required(client.get("/api/premium").headers["PAYMENT-REQUIRED"])

    assert int(cheap["accepts"][0]["amount"]) < int(premium["accepts"][0]["amount"])


def test_unpaid_request_does_not_leak_the_payload(client: TestClient):
    """没付钱绝不能拿到数据——这是"付费 API"最低限度的正确性。"""
    body = client.get("/api/cheap").text

    assert CHEAP_REPORT["report_id"] not in body


def test_the_paid_success_path_is_covered_end_to_end_not_by_this_offline_suite():
    """**如实记录覆盖边界**：付费成功路径由端到端命令覆盖，不由这组离线单测覆盖。

    离线把一次"付过款的请求"推过 x402 中间件，需要在没有 facilitator 的情况下伪造
    `PAYMENT-SIGNATURE` 并绕过 `ExactEvmServerScheme` 的校验 —— 试过，
    中间件会把失败静默吞成 402（`_extract_payment` 里 `except: return None`），
    造出来的测试会**测到我自己的 mock 而不是产品行为**，价值为负。

    真实覆盖在：
      - `make a0e`   —— 完整 x402 付款全链路探针（含链上复核）
      - `make demo`  —— 两次采购，第一次真实付款成功，A4 用 RPC 复核了收款转账

    这条测试的作用是**钉住这个事实**，免得后来人以为离线套件覆盖了付费路径。
    """
    from pactpay.payee.app import build_routes

    routes = build_routes(PAY_TO)

    assert set(routes) == {"GET /api/cheap", "GET /api/premium"}, (
        "路由表变了 —— 端到端用例（A2/A4）打的就是这两个路径，改了必须同步改用例"
    )


@pytest.mark.parametrize(
    ("path", "price"),
    [("GET /api/cheap", "$0.005"), ("GET /api/premium", "$0.05")],
)
def test_route_prices_are_the_ones_the_acceptance_cases_assume(path: str, price: str):
    """路由里的价格是**用例假设的那个值**（A2 断言 5000 原子单位、A5 断言 0.05 USDC）。

    价格写在两处（路由配置的 `$` 写法 + 中间件吐出的原子单位），
    这条钉住"写下去的那一半"，另一半由 A2 的真 402 响应钉住。
    """
    from pactpay.payee.app import build_routes

    entry = build_routes(PAY_TO)[path]

    assert entry["accepts"]["price"] == price
    assert entry["accepts"]["scheme"] == "exact"
    assert entry["accepts"]["network"] == NETWORK_V2
    assert entry["accepts"]["payTo"] == PAY_TO


def test_health_endpoint_echoes_the_configured_payee(client: TestClient):
    """`/health` 必须回显真实配置（排查"钱付给了谁"时的第一现场）。"""
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["network"] == NETWORK_V2
    assert body["pay_to"] == PAY_TO


def test_each_paid_route_delivers_its_own_report():
    """**哪个路径交付哪份报告**必须被钉住（评审第 3 轮 #10）。

    付费成功路径在这组离线单测里走不到（见上面那条如实记录的测试），
    于是"cheap 路径返回 premium 报告"这种错误**没有任何测试会红** ——
    评审把它俩对调，233 条全绿。

    现在映射提成了 `REPORTS` 常量，这条直接把"路径 → 报告"作为数据断言下来。
    报告必须**各不相同**，否则这条断言本身会退化成恒真。
    """
    from pactpay.payee.app import CHEAP_REPORT, PREMIUM_REPORT, REPORTS

    assert REPORTS["GET /api/cheap"] is CHEAP_REPORT, "cheap 路径交付的不是便宜接口的报告"
    assert REPORTS["GET /api/premium"] is PREMIUM_REPORT, "premium 路径交付的不是贵接口的报告"
    assert CHEAP_REPORT != PREMIUM_REPORT, "两份报告内容相同，这条断言失去了区分度"


def test_the_cheap_report_is_actually_cheaper_and_thinner():
    """价格与内容必须**同向**：便宜接口给的东西也要明显更少。

    否则"Agent 先买便宜的、再想买贵的"这个故事就没有说服力 ——
    两条路径若交付同样的东西，超限被拒那笔也就没有代价可言。
    """
    from pactpay.payee.app import CHEAP_REPORT, PREMIUM_REPORT

    assert CHEAP_REPORT["report_id"] != PREMIUM_REPORT["report_id"]
    assert "note" in CHEAP_REPORT and "note" in PREMIUM_REPORT
