"""`make revoke-check` —— 证明**人类随时能收权，而且收回是立刻生效的**。

## 这为什么是重要的一环

前面所有证据都在证明"Agent 撞上边界会被拒"。但还有一个问题没回答：
**人类中途改主意了怎么办？** 在此之前只有一个答案：等 24 小时到期。

平台其实提供了撤销（`POST /api/v1/pacts/{id}/revoke`），但**文档里有、从来没实测过**
（研究笔记里明确标着 "❓ never called live"）。本模块把它打掉。

## 为什么撤销必须发生在平台侧

如果有人把"急停开关"实现在我们自己的 agent 循环里（`if kill_switch: break`），
那**一个被攻破或被幻觉驱动的 Agent 直接跳过那个判断就行了**——它终究是自己让着自己。

撤销 Pact 不一样：**后续每一次签名都要经过 Cobo**，而 Cobo 手里那份状态
已经不认这个 Pact 了。Agent 就算完全失控，也拿不到一个有效签名。

## 零成本设计（重要）

本检查**不花一分钱**，因为它全程只用**超限转账**：

- 超限转账在**签名前**就被策略拒掉，钱根本不会动；
- 所以"撤销前"和"撤销后"都是被拒，但**拒绝的理由不同**；
- 就算撤销**没生效**，最多也只是拿到和撤销前一样的策略拒绝——**永远不会真的转出钱**。

## 判据

| 阶段 | 期望观察到的 | 说明 |
|---|---|---|
| ① 撤销前 | `denied` + `TRANSFER_LIMIT_EXCEEDED` | 说明**这个 key 是被接受的**，拒绝来自策略（边界生效） |
| ② 撤销 | Pact 状态变成 `revoked` | 平台确认收权 |
| ③ 撤销后 | 同一个 key、同一个请求 → **拒绝理由变了** | 说明 key 已失效，**不是**策略在拒 |

**③ 必须有别于 ①**——如果撤销后理由还是 `TRANSFER_LIMIT_EXCEEDED`，
那只能说明策略还在拒，**证明不了撤销生效**。这一条是本检查的核心。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any

from .cobo import CoboClient, CoboError, PolicyDenied
from .config import CHAIN_ID, TOKEN_ID, load_existing
from .demo import build_demo_pact_spec

#: 一个**必然超限**的金额（Pact 的单笔上限是 0.01）。
#: 用它是因为它无论如何都会被策略挡住 —— 撤销万一没生效也不会转出钱。
OVER_LIMIT = "0.5"


def _attempt_transfer(client: CoboClient, pact: dict[str, Any], amount: str) -> dict[str, Any]:
    """用 Pact 的 key 试一笔转账，把结果归一化成可比较的形状。"""
    wallet_uuid = pact["wallet_uuid"]
    pay_to = pact["pay_to"]
    try:
        result = client.transfer(
            wallet_uuid,
            {"chain_id": CHAIN_ID, "token_id": TOKEN_ID,
             "src_addr": pact["agent_address"], "dst_addr": pay_to, "amount": amount},
            api_key=pact["api_key"],
        )
        # 没被拒 = 真的转出去了。对超限金额来说这是**异常**，必须如实报，不能吞掉。
        return {"outcome": "allowed", "code": None, "reason": None, "result": result}
    except PolicyDenied as denied:
        return {"outcome": "denied", "code": denied.code, "reason": denied.reason,
                "http_status": denied.status}
    except CoboError as error:
        return {"outcome": "error", "code": (error.body or {}).get("error", {}).get("code")
                if isinstance(error.body, dict) else None,
                "reason": str(error), "http_status": error.status}


def run(*, sleep=None, client: CoboClient | None = None) -> int:
    creds = load_existing()
    for key in ("AGENT_WALLET_API_KEY", "AGENT_WALLET_WALLET_ID", "AGENT_WALLET_ADDRESS"):
        if not creds.get(key):
            print(f"缺 {key}：先跑 `make t0-probe` 申请凭证", file=sys.stderr)
            return 2

    owns_client = client is None
    client = client or CoboClient(api_key=creds["AGENT_WALLET_API_KEY"])
    try:
        return _run(client, creds, sleep=sleep)
    finally:
        if owns_client:
            client.close()


def _run(client: CoboClient, creds: dict[str, str], *, sleep) -> int:
    wallet_uuid = creds["AGENT_WALLET_WALLET_ID"]
    agent_address = creds["AGENT_WALLET_ADDRESS"]
    pay_to = creds["PAY_TO_ADDRESS"]

    print("\n" + "=" * 64)
    print("提交一份**临时** Pact（与 demo 同规则：单笔 ≤ 0.01，24h 累计 ≤ 0.1）")
    print("=" * 64)
    pact = client.submit_pact(
        wallet_id=wallet_uuid,
        intent="revoke-check：证明人类随时能收权",
        spec=build_demo_pact_spec(pay_to),
        name="pactpay-revoke-check",
    )["result"]
    pact_id = pact["pact_id"]
    pact_key = client.get_pact(pact_id)["result"]["api_key"]
    print(f"  pact_id = {pact_id}")
    print(f"  状态    = {client.get_pact(pact_id)['result']['status']}")

    state = {
        "wallet_uuid": wallet_uuid,
        "agent_address": agent_address,
        "pay_to": pay_to,
        "api_key": pact_key,
    }

    # ---- ① 撤销前：同一个 key 能通过认证，被**策略**拒 ----
    print(f"\n① 撤销前：用这个 key 试一笔**必然超限**的转账（{OVER_LIMIT} USDC，上限 0.01）")
    before = _attempt_transfer(client, state, OVER_LIMIT)
    print(f"   → outcome={before['outcome']}  code={before['code']}")
    print(f"     http_status={before.get('http_status')}")

    if before["outcome"] == "allowed":
        # 超限金额居然转出去了 = 边界根本没生效。这是**重大异常**，必须中止。
        print("\n✗ 超限转账竟然成功了 —— 策略边界没生效，本检查的前提不成立。停止。")
        return 1
    if before["outcome"] != "denied":
        print(f"\n✗ 撤销前拿到的不是策略拒绝（{before['outcome']}）—— 无法作为对照。停止。")
        return 1

    # ---- ② 撤销 ----
    print(f"\n② 撤销这份 Pact")
    revoked = client.revoke_pact(pact_id)
    status_after = client.get_pact(pact_id)["result"]["status"]
    print(f"   revoke 响应 success={revoked.get('success')}")
    print(f"   撤销后状态 = {status_after}")

    # ---- ③ 撤销后：同一个 key、同一个请求 ----
    print(f"\n③ 撤销后：**同一个 key、同一个请求**再试一次")
    after = _attempt_transfer(client, state, OVER_LIMIT)
    print(f"   → outcome={after['outcome']}  code={after['code']}")
    print(f"     http_status={after.get('http_status')}")

    # ---- 判据 ----
    print("\n" + "=" * 64)
    ok = True
    if status_after != "revoked":
        print(f"✗ Pact 状态是 {status_after!r}，不是 'revoked' —— 撤销没有被平台确认")
        ok = False
    else:
        print("✓ 平台确认 Pact 已撤销")

    if after["outcome"] == "allowed":
        print("✗ 撤销后超限转账**竟然被放行**了 —— 撤销没有生效")
        ok = False
    elif after["code"] == before["code"]:
        # 理由一模一样 ⇒ 还是策略在拒 ⇒ 证明不了撤销生效
        print(f"✗ 撤销后的拒绝理由与撤销前**完全相同**（都是 {after['code']}）——")
        print("  那只说明策略还在拒，**证明不了撤销生效**。")
        ok = False
    else:
        print(f"✓ 拒绝理由变了：{before['code']} → {after['code']}")
        print("  撤销前是**策略**在拒（key 有效）；撤销后是**key 已失效**在拒。")

    if before["code"] != after["code"] and after["outcome"] == "denied":
        print("\n✓ 结论：撤销立刻生效，且它发生在**平台侧** ——")
        print("  Agent 就算完全失控、就算跳过自己所有的检查，也拿不到有效签名。")
    print("=" * 64)
    return 0 if ok else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
