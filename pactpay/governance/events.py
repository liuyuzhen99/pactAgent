"""`make events` —— 实时发现策略被违反（能 push 就 push，只能 poll 就 poll）。

## 实测结论（先说，因为它决定了这个模块长什么样）

**文档里的 SSE 端点，线上不存在。**

`developer/audit.md` 一节 "Real-time events (SSE)" 明确写：

    GET {API_URL}/api/v1/events/stream
    Headers: X-API-Key: OWNER_API_KEY, Accept: text/event-stream

**实测（本项目亲跑）**：

| 请求 | 结果 |
|---|---|
| `GET /api/v1/events/stream`（带 agent key） | **404** |
| 同上，**不带任何 key** | **404**（说明不是权限问题，是路由不存在） |
| `GET /api/v1/events/stream/`（带尾斜杠） | 404 |
| `GET /api/v1/events` / `/audit-logs/stream` / `/wallets/events/stream` | 404 |
| **对照**：`GET /api/v1/audit-logs?limit=1` | **200** ✓ |

所以：**404 不是因为我们的 key 不对**（无 key 也 404），而是**这个部署上没有这条路由**。

## 于是这个模块做什么

1. **先试 SSE** —— 万一哪天部署升级了，它就该能用；
2. 拿到 404 就**如实说"平台没提供"**，并把实测证据打出来；
3. **退回轮询审计日志** —— 那条路是通的（实测 200）。

## 但必须说清楚轮询弱在哪

| | SSE 推送（不可用） | 轮询审计日志（可用） |
|---|---|---|
| 谁主动 | **平台推给我们** | 我们反复去问 |
| 时机 | 发生的那一刻 | 下一个轮询周期（本模块 2 秒） |
| Agent 能否拦住 | **不能** | **不能**（审计行是服务端写的） |

**两者共同的关键性质是"都不是 Agent 自己能造的"** —— 这一条比 push/poll 的区别更重要：
自建的本地 webhook 挂在 Agent 的拒绝分支里，被攻破的 Agent 不执行那个分支就没事了；
而审计行是 **Cobo 服务端**写的，Agent 拦不住。

**如实说明**：轮询有延迟（本模块 2 秒一轮），而且如果进程挂了就没人看着。
它是"近实时"，不是"实时"。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import httpx

from pactpay.platform.cobo import CoboClient, PolicyDenied
from pactpay.platform.config import CHAIN_ID, TOKEN_ID, load_existing
from pactpay.demo.run import build_demo_pact_spec
from pactpay.governance.revoke import OVER_LIMIT

#: 实测：文档里的 SSE 端点在当前部署返回 404。留着是为了"哪天它上线了就能用"。
SSE_PROBE_TIMEOUT_S = 10.0
#: 轮询周期。2 秒是"够快能叫停"和"别把接口打爆"之间的取舍。
POLL_INTERVAL_S = 2.0
#: 最多轮询多久。
POLL_TIMEOUT_S = 40.0


def probe_sse(client: CoboClient) -> tuple[bool, str]:
    """探一下 SSE 端点到底在不在。**只探一次连接**，不消费整个流。"""
    url = f"{client.base_url}/api/v1/events/stream"
    headers = {"Accept": "text/event-stream"}
    if client.api_key:
        headers["X-API-Key"] = client.api_key
    try:
        with httpx.stream("GET", url, headers=headers, timeout=SSE_PROBE_TIMEOUT_S) as response:
            if response.status_code == 404:
                return False, "HTTP 404 —— 端点不存在（不是权限问题：不带 key 也是 404）"
            response.raise_for_status()
            return True, f"HTTP {response.status_code} —— 可用"
    except httpx.HTTPStatusError as error:
        return False, f"HTTP {error.response.status_code}"
    except Exception as error:  # noqa: BLE001
        return False, f"{type(error).__name__}: {error}"


def _denied_rows(client: CoboClient, wallet_uuid: str, *, since: str | None) -> list[dict[str, Any]]:
    """拉审计日志里**被拒**的行。这就是轮询通道的数据源。"""
    got = client.list_audit_logs(wallet_id=wallet_uuid, result="denied", limit=20)
    items = ((got or {}).get("result") or {}).get("items") or []
    if since:
        items = [r for r in items if str(r.get("created_at") or "") > since]
    return items


def _trigger_violation(client: CoboClient, creds: dict[str, str]) -> str:
    """临时建一份 Pact，用它的 key 发一笔必然超限的转账，制造一次真实违规。"""
    pact = client.submit_pact(
        wallet_id=creds["AGENT_WALLET_WALLET_ID"],
        intent="events：触发一次策略违规",
        spec=build_demo_pact_spec(creds["PAY_TO_ADDRESS"]),
        name="pactpay-events-probe",
    )["result"]
    key = client.get_pact(pact["pact_id"])["result"]["api_key"]
    try:
        client.transfer(
            creds["AGENT_WALLET_WALLET_ID"],
            {"chain_id": CHAIN_ID, "token_id": TOKEN_ID,
             "src_addr": creds["AGENT_WALLET_ADDRESS"],
             "dst_addr": creds["PAY_TO_ADDRESS"], "amount": OVER_LIMIT},
            api_key=key,
        )
        return "allowed（异常！超限竟然放行了）"
    except PolicyDenied as denied:
        return f"denied {denied.code}（预期）"


def _watch_by_polling(client: CoboClient, creds: dict[str, str]) -> int:
    """退回轮询：触发一次违规，然后在审计日志里**看到它出现**。"""
    wallet_uuid = creds["AGENT_WALLET_WALLET_ID"]
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print("\n触发一次策略违规（超限转账，必然被拒；钱不会动）…")
    print(f"  触发结果：{_trigger_violation(client, creds)}")
    print(f"\n轮询审计日志（每 {POLL_INTERVAL_S:g}s 一次，最多 {POLL_TIMEOUT_S:g}s）…")

    deadline = time.monotonic() + POLL_TIMEOUT_S
    seen: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        try:
            rows = _denied_rows(client, wallet_uuid, since=since)
        except Exception as error:  # noqa: BLE001
            print(f"  ✗ 查询失败：{type(error).__name__}: {error}")
            return 1
        fresh = [r for r in rows if r.get("id") not in {s.get("id") for s in seen}]
        for row in fresh:
            seen.append(row)
            print(f"\n  ⛔ 新拒绝：id={row.get('id')} action={row.get('action')} "
                  f"result={row.get('result')}")
            print(f"     at {row.get('created_at')}")
        if seen:
            break
        time.sleep(POLL_INTERVAL_S)

    print("\n" + "=" * 64)
    if not seen:
        # 没看到就是没看到，**不假装收到了**，也不说"平台没有这个能力"。
        print("✗ 轮询窗口内没有在审计日志里看到这次拒绝")
        print("  可能是写入有延迟，也可能这条路径不写审计行（x402 就属于后者）。")
        print("=" * 64)
        return 1
    print(f"✓ 通过轮询在审计日志里看到了这次策略拒绝（{len(seen)} 条新行）")
    print("  这条路径可用，但**它是轮询不是推送** —— 有延迟，且进程挂了就没人看着。")
    print("=" * 64)
    return 0


def run(*, listen_only: bool = False, client: CoboClient | None = None) -> int:
    """`client` 可注入 —— 否则测试会去打真实网络（本项目的测试必须密闭）。"""
    creds = load_existing()
    for key in ("AGENT_WALLET_API_KEY", "AGENT_WALLET_WALLET_ID", "AGENT_WALLET_ADDRESS"):
        if not creds.get(key):
            print(f"缺 {key}：先跑 `make t0-probe` 申请凭证", file=sys.stderr)
            return 2

    owns_client = client is None
    client = client or CoboClient(api_key=creds["AGENT_WALLET_API_KEY"])
    try:
        print("\n" + "=" * 64)
        print("实时告警通道检查")
        print("=" * 64)

        print("\n① 试 SSE 推送通道（GET /api/v1/events/stream）")
        available, why = probe_sse(client)
        print(f"   → {why}")
        if not available:
            print("   **平台没提供这个通道** —— 下面退回轮询，并如实说明它弱在哪。")
            return _watch_by_polling(client, creds)

        print("   ✓ SSE 可用。但 `policy.violated` 的实时接收需要 OWNER key，")
        print("     本项目的 .env 里只有 agent key —— 所以这里仍然退回轮询路径，")
        print("     以保证无论哪种部署下 `make events` 都能给出结论。")
        return _watch_by_polling(client, creds)
    finally:
        if owns_client:
            client.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    return run(listen_only="--listen-only" in argv)


if __name__ == "__main__":
    raise SystemExit(main())
