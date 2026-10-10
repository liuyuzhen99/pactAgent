"""A0e 探针：把 x402 付款那条链走通（全项目唯一还没被实测覆盖的关键假设）。

这是整套方案里最后一个"没验过就不知道行不行"的地方：
Cobo 的 `/payment` 端点吃下收款方的 402 挑战、签名提交，然后我们拿它回的
`retry_headers` 去**重放**原始请求，看收款方认不认。

前置：① 收款方在跑（`make payee`）② agent 钱包里有 Base Sepolia USDC（faucet 领）。

跑法：
    .venv/bin/python -m pactpay.spike.a0e_probe

证据写进 progress/a0e-evidence.json（gitignored）。
"""

from __future__ import annotations

import base64
import json
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx

from pactpay.cobo import CoboClient, CoboError, PolicyDenied
from pactpay.onchain import USDC_BASE_SEPOLIA, decode_usdc_transfers
from pactpay.config import TOKEN_ID, load_existing
from pactpay.pact_spec import build_x402_pact_spec

PAYEE = "http://127.0.0.1:8001"
# 单笔上限：0.01 USDC。便宜那笔 0.005 在限内 → 应该成功。
# `/api/cheap` 的定价，**原子单位整数**（USDC 6 位小数 ⇒ 0.005 USDC = 5000）。
# 写成字面量而不是从收款方或 Cobo 那边算出来——期望值必须独立推导，否则两边一起错还永远绿。
CHEAP_PRICE_ATOMIC = 5000
USDC_DECIMALS = 6
BASE_SEPOLIA_RPC = "https://sepolia.base.org"
# Cobo 账本反映一笔支出会滞后若干秒；Dashboard（T8）读的就是它，所以要证明它会收敛。
COBO_SETTLE_TRIES = 12
COBO_SETTLE_INTERVAL_S = 5

EVIDENCE_PATH = Path("progress/a0e-evidence.json")


class Check:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def that(self, name: str, condition: bool, detail: str = "") -> bool:
        self.results.append((name, bool(condition), detail))
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        return bool(condition)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.results)


def get_balance(client: CoboClient, wallet_uuid: str) -> str:
    """查某个代币的余额。

    端点是 `GET /api/v1/wallets/balances`（钱包 uuid 是**查询参数**，不在路径里——
    路径里带 uuid 会 404）。响应形状是 `result: [BalanceRead, ...]`。
    `total` 是含在途的总量，`amount` 是可用量；这里要的是"有没有钱"，取 total。
    """
    resp = client._get("/api/v1/wallets/balances", {"wallet_uuid": wallet_uuid, "token_id": TOKEN_ID})
    rows = (resp or {}).get("result") or []
    if isinstance(rows, dict):  # 万一以后包成 data[]
        rows = rows.get("data") or []
    for row in rows:
        if isinstance(row, dict) and row.get("token_id") == TOKEN_ID:
            return str(row.get("total") or row.get("amount") or "0")
    return "0"


def onchain_usdc_atomic(address: str) -> int:
    """链上 USDC 余额，**原子单位整数**。

    这是权威来源：Cobo 的账本要滞后若干秒才反映一笔支出，链上不会。
    用整数而不是小数——0.005 这种金额做浮点比较会出静默的边界 bug。
    """
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(BASE_SEPOLIA_RPC, request_kwargs={"timeout": 30}))
    abi = [
        {
            "name": "balanceOf",
            "type": "function",
            "stateMutability": "view",
            "inputs": [{"name": "account", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]
    contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_BASE_SEPOLIA), abi=abi)
    return contract.functions.balanceOf(Web3.to_checksum_address(address)).call()


def cobo_usdc_atomic(client: CoboClient, wallet_uuid: str) -> int:
    """Cobo 账本上的余额，换算成原子单位整数。"""
    return int(Decimal(get_balance(client, wallet_uuid)) * (10**USDC_DECIMALS))


def main() -> int:
    check = Check()
    evidence: dict = {}
    env = load_existing()
    api_key = env.get("AGENT_WALLET_API_KEY", "")
    wallet_uuid = env.get("AGENT_WALLET_WALLET_ID", "")
    agent_address = env.get("AGENT_WALLET_ADDRESS", "")
    pay_to = env.get("PAY_TO_ADDRESS", "")

    if not (api_key and wallet_uuid and agent_address and pay_to):
        print("缺少凭证：先跑 `make t0-probe`")
        return 1

    print("== 0. 前置检查 ==")
    try:
        health = httpx.get(f"{PAYEE}/health", timeout=5).json()
        check.that("A0e.0 收款方在跑", health.get("pay_to") == pay_to, f"pay_to={health.get('pay_to')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  收款方连不上（{exc}）——先跑 `make payee`")
        return 1

    with CoboClient(api_key=api_key) as client:
        balance_before = get_balance(client, wallet_uuid)
        print(f"  agent 余额（{TOKEN_ID}）= {balance_before}")
        check.that(
            "A0e.1 agent 钱包有 Base Sepolia USDC（faucet 已领）",
            balance_before not in ("0", "0.0", ""),
            f"balance={balance_before}",
        )
        if balance_before in ("0", "0.0", ""):
            print("\n  ⇒ 还没领到币。这是**环境前置**，不是代码问题；领到后再跑本探针即可。")
            return 1

        # 付款前的两个基准：链上（权威）与 Cobo 账本（Dashboard 读的）。都取整数原子单位。
        cobo_before_atomic = cobo_usdc_atomic(client, wallet_uuid)
        chain_before = onchain_usdc_atomic(agent_address)
        print(f"  基准：链上={chain_before} 原子单位，Cobo 账本={cobo_before_atomic} 原子单位")

        # ---- 1. 拿 402 挑战 ----
        print("\n== 1. 向收款方拿 402 挑战 ==")
        resp = httpx.get(f"{PAYEE}/api/cheap", timeout=15)
        check.that("A0e.2 未付款请求回 402", resp.status_code == 402, f"status={resp.status_code}")
        challenge = resp.headers.get("PAYMENT-REQUIRED", "")
        check.that("A0e.3 拿到 PAYMENT-REQUIRED 挑战（原样 base64）", bool(challenge))
        decoded = json.loads(base64.b64decode(challenge))
        print(f"  challenge.accepts[0].amount = {decoded['accepts'][0]['amount']}")
        evidence["challenge"] = decoded

        # ---- 2. 建探针 Pact（专用，独立于 demo）----
        print("\n== 2. 建 A0e 探针 Pact ==")
        pact = client.submit_pact(
            wallet_id=wallet_uuid,
            intent="A0e：x402 付款全链路探针",
            name=f"pactpay-a0e-{uuid.uuid4().hex[:8]}",
            spec=build_x402_pact_spec(pay_to),
        )["result"]
        pact_id = pact["pact_id"]
        pact_key = client.get_pact(pact_id)["result"]["api_key"]
        check.that("A0e.4 探针 Pact active 且取到 pact-scoped key", pact.get("status") == "active" and bool(pact_key), pact_id)

        # ---- 3. Cobo 签名并提交 x402 付款 ----
        print("\n== 3. Cobo /payment（protocol=x402）==")
        request_id = str(uuid.uuid4())  # 幂等键：每次必须换新
        try:
            pay_resp = client.payment(
                wallet_uuid,
                protocol="x402",
                request_id=request_id,
                x402_payment_required=challenge,
                api_key=pact_key,
                expect_error=True,
            )
        except PolicyDenied as exc:
            print(f"  被策略拒了：{exc.code} {json.dumps(exc.raw, ensure_ascii=False)[:400]}")
            check.that("A0e.5 x402 付款未被拒（限内应当放行）", False, exc.code)
            return 1

        print(f"  响应：{json.dumps(pay_resp, ensure_ascii=False)[:600]}")
        result = (pay_resp or {}).get("result") or {}
        check.that("A0e.5 付款已提交", result.get("status") in ("completed", "submitted"), str(result.get("status")))
        retry_headers = result.get("retry_headers") or {}
        check.that(
            "A0e.6 拿到 retry_headers['PAYMENT-SIGNATURE']",
            "PAYMENT-SIGNATURE" in retry_headers,
            f"keys={sorted(retry_headers.keys())}",
        )
        # 顺带把"tx_hash 在 x402 下是 null"这条官方行为钉在证据里
        print(f"  tx_hash = {result.get('tx_hash')!r}（x402 下按官方 schema 应为 null）")
        evidence["payment_result"] = result

        # ---- 4. 用 retry_headers 重放原始请求 ----
        print("\n== 4. 重放原始请求（带 PAYMENT-SIGNATURE）==")
        replay = httpx.get(f"{PAYEE}/api/cheap", headers=retry_headers, timeout=30)
        check.that("A0e.7 重放后拿到 200（收款方认了这笔付款）", replay.status_code == 200, f"status={replay.status_code}")
        body = replay.json() if replay.status_code == 200 else {}
        print(f"  数据正文：{json.dumps(body, ensure_ascii=False)[:300]}")
        check.that(
            "A0e.8 真的取回了数据正文（断言具体字段，不是只看非空）",
            body.get("report_id") == "cheap-raw-001",
            str(body.get("report_id")),
        )

        tx_hash = ""
        pay_response_header = replay.headers.get("PAYMENT-RESPONSE", "")
        if pay_response_header:
            settlement = json.loads(base64.b64decode(pay_response_header))
            print(f"  PAYMENT-RESPONSE = {json.dumps(settlement, ensure_ascii=False)}")
            # v2 里字段叫 transaction，不叫 txHash
            tx_hash = settlement.get("transaction", "") or ""
            check.that("A0e.9 结算回执 success=true", settlement.get("success") is True, str(settlement.get("success")))
            evidence["settlement"] = settlement
        check.that("A0e.10 拿到链上交易哈希（PAYMENT-RESPONSE.transaction）", bool(tx_hash), tx_hash)

        # ---- 5. 链上复核 ----
        if tx_hash:
            print("\n== 5. 链上复核（Base Sepolia RPC）==")
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(BASE_SEPOLIA_RPC, request_kwargs={"timeout": 30}))
            check.that("A0e.11 RPC 连上 Base Sepolia", w3.is_connected(), f"chain_id={w3.eth.chain_id if w3.is_connected() else '?'}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            check.that("A0e.12 交易已上链且成功", receipt.status == 1, f"block={receipt.blockNumber}")
            # 解 USDC 的 Transfer 事件，证明"钱真的动了"。
            # 用 `pactpay.onchain` 里那份**同一个**实现——demo（A4）也调它。
            # 之前这里是一段内联拷贝，两份逻辑会各自漂移。
            moved = decode_usdc_transfers(list(receipt.logs), usdc_address=USDC_BASE_SEPOLIA)
            check.that("A0e.13 交易里有 USDC Transfer 事件", len(moved) == 1, f"条数={len(moved)}")
            if moved:
                value = moved[0]["value"]
                check.that("A0e.14 转账金额 = 5000 原子单位（0.005 USDC）", value == 5000, f"value={value}")
                evidence["transfer_value"] = value

        # ---- 5b. 余额变化：链上精确断言 ----
        # 为什么不在付款后立刻读 Cobo 的余额：它的账本要滞后若干秒才反映这笔支出，
        # 立刻读会拿到旧值 → 断言假失败（这个坑真踩过）。链上没有滞后，所以拿它做权威断言。
        print("\n== 5b. 余额变化 ==")
        chain_after = onchain_usdc_atomic(agent_address)
        delta = chain_before - chain_after
        print(f"  链上余额：{chain_before} -> {chain_after}（差 {delta} 原子单位）")
        check.that(
            f"A0e.15 链上余额精确减少 {CHEAP_PRICE_ATOMIC} 原子单位"
            f"（={CHEAP_PRICE_ATOMIC / 10**USDC_DECIMALS} USDC）",
            delta == CHEAP_PRICE_ATOMIC,
            f"实际差 {delta}",
        )
        evidence["onchain_balance"] = {"before": chain_before, "after": chain_after, "delta": delta}

        # Cobo 账本会滞后；Dashboard（T8）读的正是它，所以要证明它收敛到同一个精确值。
        expected_cobo = cobo_before_atomic - CHEAP_PRICE_ATOMIC
        cobo_after_atomic = None
        waited = 0
        for _ in range(COBO_SETTLE_TRIES):
            cobo_after_atomic = cobo_usdc_atomic(client, wallet_uuid)
            if cobo_after_atomic == expected_cobo:
                break
            time.sleep(COBO_SETTLE_INTERVAL_S)
            waited += COBO_SETTLE_INTERVAL_S
        check.that(
            "A0e.15b Cobo 账本收敛到精确值（Dashboard 读的是它）",
            cobo_after_atomic == expected_cobo,
            f"期望 {expected_cobo}，实际 {cobo_after_atomic}（等了 {waited}s）",
        )
        evidence["cobo_balance"] = {
            "before": cobo_before_atomic,
            "after": cobo_after_atomic,
            "expected": expected_cobo,
        }

        # ---- 6. 审计行（带 wallet_id 才看得全）----
        print("\n== 6. 审计日志 ==")
        try:
            audit = client.list_audit_logs(wallet_id=wallet_uuid, limit=20)
            rows = ((audit or {}).get("result") or {}).get("items") or []
        except CoboError:
            saved, client.api_key = client.api_key, pact_key
            try:
                audit = client.list_audit_logs(limit=20)
                rows = ((audit or {}).get("result") or {}).get("items") or []
            finally:
                client.api_key = saved
        actions = sorted({r.get("action") for r in rows if isinstance(r, dict)})
        print(f"  action 取值 = {actions}")
        check.that("A0e.16 审计里有 transfer 相关记录", any("transfer" in str(a) for a in actions), str(actions))
        evidence["audit_actions"] = actions
        evidence["audit_sample"] = rows[:5]

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))

    print("\n" + "=" * 64)
    if check.ok:
        print(f"A0e：全部 {len(check.results)} 条断言通过 ✅ —— x402 全链路成立")
    else:
        failed = [n for n, ok, _ in check.results if not ok]
        print(f"A0e：{len(failed)} 条断言未通过 ❌")
        for name in failed:
            print(f"  - {name}")
    print(f"证据已写入 {EVIDENCE_PATH}")
    print("=" * 64)
    return 0 if check.ok else 1


if __name__ == "__main__":
    sys.exit(main())
