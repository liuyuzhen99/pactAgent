"""Pact 规格（PactSpec）的构造函数。

**为什么单独成一个模块**：`build_x402_pact_spec` 原先住在 `pactpay/spike/a0e_probe.py`
里，而 `demo.py` 又从那里 import —— 生产命令依赖探针脚本，方向是反的（评审 B9）。
探针本该能随时删掉；被生产路径依赖之后就删不动了。现在两边都从 `pact_spec` 取。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pactpay.platform.config import CHAIN_ID, TOKEN_ID

#: A0e 探针用的单笔上限：只够付一次 /api/cheap（0.005）。
X402_CAP = "0.01"


def build_x402_pact_spec(pay_to: str, *, cap: str = X402_CAP) -> dict[str, Any]:
    """允许 agent 用 x402 付款给这个商户，单笔上限 `cap`。

    `when` 里限定目标地址＝商户地址：这样即使别处出错，也不会意外允许付给别人。
    这是 R6「默认拒绝」的正面用法——只有被这条 allow 明确覆盖的目标才能付款。
    """
    return {
        "execution_plan": "走通 x402 付款全链路（402 → Cobo 签名提交 → retry_headers 重放）。",
        "policies": [
            {
                "name": "allow-x402-to-merchant",
                "type": "transfer",
                "rules": {
                    "effect": "allow",
                    "when": {
                        "chain_in": [CHAIN_ID],
                        "token_in": [{"chain_id": CHAIN_ID, "token_id": TOKEN_ID}],
                        "destination_address_in": [{"chain_id": CHAIN_ID, "address": pay_to}],
                    },
                    "deny_if": {"amount_gt": cap},
                },
            }
        ],
        "completion_conditions": [{"type": "time_elapsed", "threshold": "3600"}],
    }


#: 按**额度**计的完成条件类型。它们与累计拒绝阈值抢同一根线，必须严格错开。
SPEND_COMPLETIONS = frozenset({"amount_spent", "amount_spent_usd"})


def check_deny_strictly_below_spend_completions(spec: dict[str, Any]) -> dict[str, Any]:
    """检查 PactSpec 里"按额度计的完成条件"是否**严格大于**累计拒绝阈值。

    **为什么要做成一个真函数**（评审第 2 轮）：原来这条不变量写在测试里，
    形式是 `for cond in spec["completion_conditions"]: if cond["type"] in SPEND_COMPLETIONS: ...`。
    而当前 demo 的完成条件**只有 `time_elapsed`** —— 循环体一次都不执行，
    断言恒真。**一个空转的 for 循环不是检查，是摆设**：它绿着，却什么也没守。

    抽成函数后就能对它做**阳性对照**：喂一个真的取等号的 spec，它必须报错。
    没有阳性对照的检查，证明不了它抓得住任何东西。

    R-x10 的机理：`amount_spent` 是**终止**条件、`rolling_24h` 是**拒绝**条件。
    两者取等号时，完成条件会抢在拒绝之前把 Pact 结束掉 —— 跨线那笔拿到的会是
    "Pact 已结束"而不是累计拒绝，演示要证的"硬边界"就变了味，而且先命中哪条不稳定。
    """
    limits = ((spec.get("deny_if") or {}).get("usage_limits") or {})
    caps: list[tuple[str, Decimal]] = []
    for name, rule in limits.items():
        value = (rule or {}).get("amount_gt")
        if value is not None:
            caps.append((name, Decimal(str(value))))

    problems: list[str] = []
    checked = 0
    for cond in spec.get("completion_conditions") or []:
        if cond.get("type") not in SPEND_COMPLETIONS:
            continue
        checked += 1
        threshold = Decimal(str(cond.get("threshold")))
        for name, cap in caps:
            if cap >= threshold:
                problems.append(
                    f"完成条件 {cond['type']}={threshold} 不大于 {name} 的拒绝阈值 {cap} —— "
                    f"完成条件会先命中，跨线那笔将拿到「Pact 已结束」而不是累计拒绝（R-x10）"
                )
    return {"ok": not problems, "problems": problems, "spend_completions_checked": checked}
