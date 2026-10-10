"""`make agent` —— 让**模型自己驱动**一次真实的采购，全程留痕。

## 和 `make demo` 的区别

| | `make demo` | `make agent` |
|---|---|---|
| 谁决定买什么 | LLM 出一份计划，**脚本**执行 | **模型每一步自己调工具** |
| 超限那笔被拒时 | 脚本处理拒绝 | **模型亲眼看见拒绝，自己决定怎么办** |
| 采购顺序 | 计划里定死 | 模型看着结果决定 |
| 证据 | 完整（A4/A5c 三重证据） | 工具调用流水 + 每笔结果 + 链上复核 |

两者都真花钱、都真上链、都写证据。`demo` 用来演示**边界**（有完整的"签名前拦截"三重证据），
`agent` 用来演示**自主**（模型在看得到结果的前提下自己决策）。

## 证据里多出来的一样东西

`transcript`：每一次工具调用、参数、返回，**逐条**记进 `run.json`。
于是"它当时看到了什么、于是做了什么决定"是**可以复核的**，而不是一句"模型决定的"。

这是"可审计"这个词在这里的具体含义：不是"我们相信模型"，是
"**它每一步的输入输出都留下来了，谁都能去看**"。
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .agent import probe_default_deny
from .agent_loop import StepLimitExceeded, run_agent
from .cobo import CoboClient
from .config import CHAIN_ID, TOKEN_ID, load_existing
from .demo import (
    BUDGET_USD,
    DEFAULT_DENY_AMOUNT,
    OUTSIDE_WHITELIST,
    PAYEE_BASE,
    TASK,
    build_demo_pact_spec,
)
from .evidence import (
    EVIDENCE_DIR,
    OUTCOMES,
    append_record,
    purchase_as_evidence,
    read_chain,
    run_dir_for,
    write_anchor,
    write_counts,
    write_run_meta,
)
from .onchain import current_block_number, verify_usdc_transfer
from .payee_lifecycle import ensure_payee
from .planner import usd_to_atomic
from .preconditions import find_balance, require_balance
from .tools import ToolContext, to_json


def _print_transcript(ctx: ToolContext) -> None:
    """把模型看到的一切打印出来 —— 这是"自主"的可见证据。"""
    print("\n" + "=" * 64)
    print("Agent 的工具调用流水（模型当时看到的就是这些）")
    print("=" * 64)
    for entry in ctx.transcript:
        if entry["kind"] != "tool_call":
            print(f"  [步 {entry['step']}] 模型没有调工具，只说了句：{entry['content'][:80]}")
            continue
        mark = "💰" if entry["spends_money"] else "  "
        args = ", ".join(f"{k}={v!r}" for k, v in entry["args"].items())
        print(f"\n  {mark} [步 {entry['step']}] {entry['tool']}({args})")
        result = entry["result"]
        if result.get("outcome") == "paid":
            print(f"       → ✅ 成功，拿到数据 {result.get('data')}")
            print(f"         链上交易 {result.get('settlement_tx')}")
        elif result.get("outcome") == "denied":
            print(f"       → ⛔ 被拒绝：{result.get('code')}")
            print(f"         触发原因：{result.get('policy_reason')}")
            print(f"         命中策略：{result.get('policy_id')}")
            print(f"         {result.get('chain_trace')}")
        elif result.get("outcome") == "failed":
            print(f"       → ⚠️ 未结算：{result.get('reason')}")
        elif "error" in result:
            print(f"       → 工具报错：{result['error']}")
        else:
            print(f"       → {to_json(result)[:200]}")


def _run(api_key: str, wallet_uuid: str, agent_address: str, pay_to: str) -> int:
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    run_dir = run_dir_for(run_id, started_at)
    chain_path = run_dir / "chain.jsonl"
    counter: dict[str, int] = {}

    with CoboClient(api_key=api_key) as client:
        print(f"\n提交 Pact（单笔 ≤ 0.01，24h 累计 ≤ 0.1，24h 后失效）…")
        pact = client.submit_pact(
            wallet_id=wallet_uuid,
            intent=f"{TASK}，最多花 {BUDGET_USD} USDC",
            spec=build_demo_pact_spec(pay_to),
            name=f"pactpay-agent-{run_id[:8]}",
        )["result"]
        pact_id = pact["pact_id"]
        pact_status = client.get_pact(pact_id)["result"]["status"]
        pact_key = client.get_pact(pact_id)["result"]["api_key"]
        print(f"  Pact {pact_id}  status={pact_status}")
        print(f"  **Agent 看不到这个 Pact 里写了什么规则** —— 它只知道自己的预算 {BUDGET_USD}")

        balances = client.list_balances(wallet_uuid, chain_id=CHAIN_ID, token_id=TOKEN_ID).get("result") or []
        found = find_balance(balances, CHAIN_ID, TOKEN_ID) or {}
        print(f"\n前置检查：余额 {found.get('amount', '?')} USDC")
        require_balance(balances, CHAIN_ID, TOKEN_ID, Decimal("0.005"), label="本次 agent 运行")

        run_start_block = current_block_number()

        ctx = ToolContext(
            client=client,
            wallet_uuid=wallet_uuid,
            pact_api_key=pact_key,
            payee_base=PAYEE_BASE,
            agent_address=agent_address,
            budget_usd=BUDGET_USD,
        )

        print("\n" + "=" * 64)
        print("Agent 开始自主采购（模型自己决定每一步）")
        print("=" * 64)

        try:
            report = run_agent(ctx, TASK)
        except StepLimitExceeded as error:
            print(f"\n✗ {error}")
            return 1

        _print_transcript(ctx)

        print("\n" + "=" * 64)
        print(f"Agent 的交付说明：\n  {report}")
        print("=" * 64)

        # ---- 默认拒绝检查（R6）：与采购无关，是每次运行都要重跑的命题 ----
        print("\n默认拒绝检查（付给白名单外的地址）：")
        deny = probe_default_deny(
            client, wallet_uuid, pact_key=pact_key, chain_id=CHAIN_ID, token_id=TOKEN_ID,
            src_addr=agent_address, dst_addr=OUTSIDE_WHITELIST, amount=DEFAULT_DENY_AMOUNT,
        )
        print(f"  {deny['outcome']}  码={deny['code']}  原因={deny['reason']}")

        # ---- 落证据 ----
        audits = client.list_audit_logs(wallet_id=wallet_uuid, limit=50)
        audit_rows = ((audits or {}).get("result") or {}).get("items") or []
        for outcome in ctx.outcomes:
            payload = purchase_as_evidence(
                outcome, agent_address=agent_address, pay_to=pay_to,
                amount_atomic=int(usd_to_atomic(outcome.amount_usd)),
            )
            payload["wallet_before"] = payload["wallet_after"] = {}
            if outcome.outcome == "paid":
                a4 = verify_usdc_transfer(
                    outcome.settlement_tx or "",
                    expect_from=agent_address, expect_to=pay_to,
                    expect_value=int(usd_to_atomic(outcome.amount_usd)),
                )
                payload["onchain_check"] = a4
                print(f"\n  A4 链上复核（{outcome.resource}）："
                      f"{'✅' if a4['ok'] else '✗'} 区块 {a4.get('block_number')}")
                if not a4["ok"]:
                    raise RuntimeError(
                        f"{outcome.resource} 记成 paid 但链上核对不过：{a4['errors']}"
                        " —— 宁可中止，也不写一条自己都验不过的证据"
                    )
            append_record(chain_path, payload, kind="purchase")
            counter[outcome.outcome] = counter.get(outcome.outcome, 0) + 1

        # 默认拒绝是**独立的一项检查**（kind="default_deny"），不是一次采购结果。
        # 第一版在这里多加了 `counter["denied"] += 1`，于是运行时计数器说 denied=2
        # 而链里只有 1 条 —— **被 `make verify` 当场抓住**（计数对不上，exit 1）。
        # 校验器在这里做的正是它该做的事：不信任运行时的自我汇报，去链里数一遍。
        append_record(
            chain_path,
            {"default_deny": deny, "outcomes": ctx.report},
            kind="default_deny",
        )

        write_run_meta(run_dir, {
            "run_id": run_id,
            "started_at": started_at,
            "task": TASK,
            "budget_usd": str(BUDGET_USD),
            "pact_id": pact_id,
            "pact_status": pact_status,
            "payee": PAYEE_BASE,
            "wallet_uuid": wallet_uuid,
            "agent_address": agent_address,
            "mode": "agent",
            "report": report,
            "transcript": ctx.transcript,
            "audit_logs": audit_rows,
        })
        write_counts(run_dir, {name: counter.get(name, 0) for name in OUTCOMES})
        write_anchor(
            run_dir, read_chain(chain_path),
            agent_address=agent_address,
            from_block=run_start_block, to_block=current_block_number(),
        )

    paid = ctx.paid_outcomes()
    denied = ctx.denied_outcomes()
    print("\n" + "=" * 64)
    print(f"汇总：买成 {len(paid)} 笔，被拒 {len(denied)} 笔，"
          f"花掉 {ctx.spent} USDC（预算 {BUDGET_USD}）")
    print(f"证据：{run_dir}/（链 {len(read_chain(chain_path))} 条 + {len(audit_rows)} 条审计行"
          f" + {len(ctx.transcript)} 条工具流水）")
    print(f"校验：python -m pactpay.evidence.verify {run_dir}")
    print("=" * 64)

    if deny.get("outcome") != "denied":
        print("\n✗ 默认拒绝没有生效 —— fail-closed 是错的（R6 不成立）")
        return 1
    return 0


def main() -> int:
    creds = load_existing()
    for key in ("AGENT_WALLET_API_KEY", "AGENT_WALLET_WALLET_ID", "AGENT_WALLET_ADDRESS"):
        if not creds.get(key):
            print(f"缺 {key}：先跑 `make t0-probe` 申请凭证", file=sys.stderr)
            return 2

    ensure_payee(PAYEE_BASE)
    return _run(
        creds["AGENT_WALLET_API_KEY"],
        creds["AGENT_WALLET_WALLET_ID"],
        creds["AGENT_WALLET_ADDRESS"],
        creds["PAY_TO_ADDRESS"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
