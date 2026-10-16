"""A5b：累计总预算（R2）真的会被执行吗？

**为什么单独做一个检查**：demo 那份 Pact 的单笔上限是 0.01、总额 0.1，
所以要 **≥11 笔**成功付款（0.01×11 = 0.11）才会触发累计拒绝——与演示"恰好两笔"冲突。
本检查用**独立的第二份 Pact**，把窗口阈值设成**可达的值**（默认 0.015），
跑几笔小额把它打爆，验的是**同一条代码路径**。

**如实说明**：demo Pact 上 `0.1` 那一档**没有被直接打爆**，因为算术够不着。
这里验的是机制本身会生效，不是"0.1 这个数字被验过了"。

**机制纠错（重要，别再搞混）**：`amount_spent` 是**完成条件**——累计到达阈值时 Pact
**自动终止**，它**不会拒绝**跨线的那一笔，那一笔会被正常执行、然后 Pact 才结束。
那样累计会**超出预算最多一个单笔上限**（0.1 → 0.11），对宣称"硬边界"的项目是命题级漏洞。
唯一能**拒绝**累计超支的是 `deny_if.usage_limits.rolling_{1h,24h,7d,30d}`。
"""

from __future__ import annotations

import sys
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx

from pactpay.platform.cobo import CoboClient, PolicyDenied
from pactpay.demo.payee_lifecycle import ensure_payee
from pactpay.platform.config import load_existing

# 链/币标识**只有一处定义**（`config.py`）。原先这里、`demo.py`、`complete_check.py`
# 各写一遍同样的字面量 —— 改一处不会传导到别处，是"四处同名常量"那类静默漂移的温床（N7）。
from pactpay.platform.config import CHAIN_ID, TOKEN_ID  # noqa: E402

PAYEE_URL = "http://127.0.0.1:8001/api/cheap"
CHEAP_USD = Decimal("0.005")
DEFAULT_THRESHOLD = "0.015"
MAX_ATTEMPTS = 8


def build_cumulative_pact_spec(
    threshold: str = DEFAULT_THRESHOLD,
    chain_id: str = CHAIN_ID,
    token_id: str = TOKEN_ID,
) -> dict[str, Any]:
    """只放**一条 rolling 规则**的 Pact——这样拒绝只可能来自累计。

    两条硬约束：
    - **不放 per-tx 规则**：否则 per-tx 拒绝会冒充累计拒绝（两者载荷不可区分）。
    - **完成条件只留 `time_elapsed`**：若放 `amount_spent`，累计到阈值时 Pact 会
      **先 COMPLETED**，下一笔报"已结束"而不是累计拒绝，断言必然假失败。
    """
    return {
        "execution_plan": "验证累计总预算：rolling_24h 窗口达到阈值后，下一笔被拒绝。",
        "policies": [
            {
                "name": "cumulative-window-cap",
                "type": "transfer",
                "rules": {
                    "effect": "allow",
                    "when": {
                        "chain_in": [chain_id],
                        "token_in": [{"chain_id": chain_id, "token_id": token_id}],
                    },
                    "deny_if": {"usage_limits": {"rolling_24h": {"amount_gt": threshold}}},
                },
            }
        ],
        "completion_conditions": [{"type": "time_elapsed", "threshold": "86400"}],
    }


def check_cumulative_denial(
    attempts: list[dict[str, Any]], threshold: Decimal
) -> dict[str, Any]:
    """对"逐笔尝试"做三层可证伪判别。返回 `{ok, why, denied_at, sum_before_denial}`。

    - **① 拒绝发生在第 N 笔且 N > 1**：per-tx 规则绝不可能拒绝第 1 笔之后的小额，
      所以这是最本质的判别，且不依赖阈值语义是 `>` 还是 `>=`。
    - **② 拒绝载荷里的 counter 读数必须自洽**：code 是 `COUNTER_LIMIT_EXCEEDED`、
      `limit_value` 等于本次配置的阈值、`current_total + increment > limit_value`、
      且 `current_total` 与本轮逐笔累计一致。
    - 任何一条不成立 ⇒ `ok=False`，并说明为什么不成立。

    **为什么删掉了方案原本的两条启发式**（"前 N−1 笔都在单笔上限内"、
    "累计和 ≥ 阈值 − 单笔上限"）：它们是在"两类拒绝载荷不可区分"这个**错误前提**下
    设计的弱代理。2026-10-12 实测推翻了那个前提——累计拒绝自带精确读数，
    ② 直接验算读数即可。留着它们会变成**永远不会独立触发的死分支**
    （变异测试证实：删掉这两条检查，没有任何用例变红）。**能删的弱判据就删掉，别留给人看。**
    """
    denied_at = next((i for i, a in enumerate(attempts) if a.get("outcome") != "paid"), None)

    if denied_at is None:
        return {
            "ok": False,
            "why": "整轮都没有一笔被拒 ⇒ 累计上限根本没生效",
            "denied_at": None,
            "sum_before_denial": sum((a["amount"] for a in attempts), Decimal(0)),
        }

    if denied_at == 0:
        return {
            "ok": False,
            "why": "第 1 笔就被拒 ⇒ 不是累计拒绝（per-tx 或白名单才会这样），N>1 不成立",
            "denied_at": 0,
            "sum_before_denial": Decimal(0),
        }

    # ---- 决定性证据：拒绝载荷本身带着计数器的读数 ----
    #
    # 方案原本以为两类拒绝"载荷不可区分"，只能靠"N>1"这种弱启发式去猜。
    # **2026-10-12 实测推翻了这一点**：累计窗口拒绝的响应里明确带着
    # `reason/code = counter_limit_exceeded / COUNTER_LIMIT_EXCEEDED` 以及
    # `window` / `metric` / `limit_value` / `current_total` / `increment`。
    # 于是可以直接验算，不必猜：
    #   · 读数必须是 counter 类（而不是 per-tx 的 matched_pact_transfer_deny_if）
    #   · 窗口内已累计 current_total，本笔 increment 会把总额推过 limit_value
    #   · 且 limit_value 必须**正好等于**本检查配的阈值
    total = sum((a["amount"] for a in attempts[:denied_at]), Decimal(0))
    denial = attempts[denied_at].get("denial") or {}
    details = denial.get("details") or {}
    if denial.get("code") != "COUNTER_LIMIT_EXCEEDED":
        return {
            "ok": False,
            "why": f"拒绝码是 {denial.get('code')!r}，不是累计窗口的 COUNTER_LIMIT_EXCEEDED "
                   f"⇒ 这次拒绝不是累计造成的（可能是 per-tx 或白名单）",
            "denied_at": denied_at,
            "sum_before_denial": total,
        }

    def _dec(key: str) -> Decimal | None:
        raw = details.get(key)
        try:
            return Decimal(str(raw)) if raw is not None else None
        except Exception:  # noqa: BLE001 - 解析不了就当作缺失，下面会报出来
            return None

    limit_value, current_total, increment = _dec("limit_value"), _dec("current_total"), _dec("increment")
    if None in (limit_value, current_total, increment):
        return {
            "ok": False,
            "why": f"累计拒绝里缺少 limit_value/current_total/increment，无法验算：{details}",
            "denied_at": denied_at,
            "sum_before_denial": total,
        }
    if limit_value != threshold:
        return {
            "ok": False,
            "why": f"拒绝读数的 limit_value={limit_value} 与本检查配置的阈值 {threshold} 不符",
            "denied_at": denied_at,
            "sum_before_denial": total,
        }
    if current_total + increment <= limit_value:
        return {
            "ok": False,
            "why": f"验算不成立：current_total({current_total}) + increment({increment}) "
                   f"并未超过 limit_value({limit_value}) ⇒ 这次拒绝的理由与读数矛盾",
            "denied_at": denied_at,
            "sum_before_denial": total,
        }
    if current_total != total:
        return {
            "ok": False,
            "why": f"拒绝读数里的 current_total({current_total}) 与本轮逐笔累计({total}) 不一致",
            "denied_at": denied_at,
            "sum_before_denial": total,
        }

    return {
        "ok": True,
        "why": f"第 {denied_at + 1} 笔被拒；窗口内已累计 {current_total}，本笔 {increment} 会超过上限 "
               f"{limit_value}（此前累计 {total}，阈值 {threshold}）",
        "denied_at": denied_at,
        "sum_before_denial": total,
    }


def _pay(client: CoboClient, wallet_uuid: str, pact_key: str) -> dict[str, Any]:
    """发一笔 x402 付款，返回 `{outcome, policy_id, http_status, code}`。"""
    challenge = httpx.get(PAYEE_URL, timeout=20).headers["PAYMENT-REQUIRED"]
    request_id = str(uuid.uuid4())
    try:
        result = client.payment(
            wallet_uuid,
            protocol="x402",
            request_id=request_id,
            x402_payment_required=challenge,
            api_key=pact_key,
        )
    except PolicyDenied as denied:
        return {
            "outcome": "denied",
            "amount": CHEAP_USD,
            "http_status": denied.status,
            "code": denied.code,
            "policy_id": (denied.details or {}).get("policy_id"),
            "denial": {"code": denied.code, "reason": denied.reason, "details": denied.details},
            "request_id": request_id,
            "idempotent": False,
        }
    inner = result.get("result") or {}
    return {
        "outcome": "paid",
        "amount": CHEAP_USD,
        "http_status": 200,
        "code": None,
        "policy_id": None,
        "request_id": request_id,
        "idempotent": bool(inner.get("idempotent")),
    }


def main() -> int:
    env = load_existing()
    api_key = env.get("AGENT_WALLET_API_KEY", "")
    wallet_uuid = env.get("AGENT_WALLET_WALLET_ID", "")
    if not (api_key and wallet_uuid):
        print("缺少凭证：先跑 `make t0-probe`。", file=sys.stderr)
        return 2

    threshold = Decimal(DEFAULT_THRESHOLD)
    payee_proc = ensure_payee("http://127.0.0.1:8001")
    try:
        return _run(api_key, wallet_uuid, threshold, payee_proc)
    finally:
        if payee_proc is not None:
            payee_proc.terminate()


def judge_a5b(
    *,
    policies_count: int,
    verdict_ok: bool,
    verdict_why: str,
    fresh_first_outcome: str,
) -> tuple[bool, list[str]]:
    """A5b 的**最终结论**（纯函数，便于脱网单测）。

    **为什么抽出来**（第 5 轮评审 G7）：原来判据是一条条累加的
    （`failed |= not verdict["ok"]` …），评审把**任意一条**改成
    `failed |= False` 就能让一次"累计上限从未触发"的运行打出
    `✓ A5b 成立：累计总预算会被拒绝` 并 exit 0 —— 292 条测试全绿。
    屏幕上的结论必须从**一组被观察到的输入**算出来，而不是靠若干处 `|=` 的副作用。

    任何一条不成立 ⇒ 结论不成立。这正是这个模块存在的意义：
    它要证明"累计上限**真的**会触发"，而不是"我们跑了几个步骤"。
    """
    problems: list[str] = []
    if policies_count != 1:
        problems.append(f"策略数组应只有 1 条（只放 rolling，拒绝只能来自它），实际 {policies_count} 条")
    if not verdict_ok:
        problems.append(f"累计判别未成立：{verdict_why}")
    if fresh_first_outcome != "paid":
        problems.append(
            f"新建 Pact 的第一笔是 {fresh_first_outcome}（不是 paid）"
            " ⇒ 累计计数器可能是**钱包作用域**（未随新 Pact 清零）"
        )
    return (not problems), problems


def _run(api_key, wallet_uuid, threshold, payee_proc) -> int:
    with CoboClient(api_key=api_key) as client:
        # ---- 第二份 Pact（独立于 demo）----
        pact = client.submit_pact(
            wallet_id=wallet_uuid,
            intent="A5b：验证累计总预算会被拒绝",
            name=f"cumulative-{uuid.uuid4().hex[:8]}",
            spec=build_cumulative_pact_spec(DEFAULT_THRESHOLD),
        )["result"]
        pact_id = pact["pact_id"]
        detail = client.get_pact(pact_id)["result"]
        # ③ 回读策略数组长度 == 1（证明只有 rolling 一条，拒绝只能来自它）
        # 实测（2026-10-12）：策略数组在 **spec.policies** 下，不是顶层 policies——
        # 顶层只有 id/status/api_key/spec/progress_* 等。
        policies = (detail.get("spec") or {}).get("policies") or []
        policies_count = len(policies)
        print(f"1) 第二份 Pact {pact_id}  status={detail.get('status')}  policies={policies_count} 条")
        if len(policies) != 1:
            print(f"   ✗ 期望只有 1 条策略（只放 rolling），实际 {len(policies)} 条")
        pact_key = detail["api_key"]

        # ---- 逐笔打，直到被打爆 ----
        print(f"2) 逐笔付款（每笔 {CHEAP_USD}，阈值 {threshold}）：")
        attempts: list[dict[str, Any]] = []
        for i in range(MAX_ATTEMPTS):
            attempt = _pay(client, wallet_uuid, pact_key)
            attempts.append(attempt)
            mark = "✅" if attempt["outcome"] == "paid" else "⛔"
            print(f"   {mark} 第 {i + 1} 笔 -> {attempt['outcome']}"
                  f"  code={attempt['code']}  policy_id={attempt['policy_id']}")
            if attempt["outcome"] != "paid":
                break
            time.sleep(1)

        verdict = check_cumulative_denial(attempts, threshold)
        print(f"3) 判别：{'✓' if verdict['ok'] else '✗'} {verdict['why']}")

        # ---- ④ 证伪"计数器是钱包作用域"：新建 Pact 的第一笔必须仍然放行 ----
        # 若计数器挂在钱包上，新建 Pact 不会清零，第一笔会"开局即被拒"，
        # 那 ① 的 N>1 会失败、报出来是个误导性的假失败。
        print("4) 证伪钱包作用域：新建一份 Pact，它的第一笔必须仍被放行")
        fresh = client.submit_pact(
            wallet_id=wallet_uuid,
            intent="A5b：证伪累计计数器是钱包作用域",
            name=f"cumulative-fresh-{uuid.uuid4().hex[:8]}",
            spec=build_cumulative_pact_spec("999"),
        )["result"]
        fresh_key = client.get_pact(fresh["pact_id"])["result"]["api_key"]
        first = _pay(client, wallet_uuid, fresh_key)
        print(f"   新建 Pact 的第一笔 -> {first['outcome']}  code={first['code']}")
        if first["outcome"] != "paid":
            print("   ✗ 新建 Pact 的第一笔就被拒 ⇒ 累计计数器是**钱包作用域**（未随新 Pact 清零）")

        # ---- 交叉核对：两次拒绝来自不同策略 ----
        print(f"5) 本次累计拒绝的 policy_id = {attempts[-1]['policy_id']}")

    # **结论一次性算出来**（G7）：不靠沿途若干处 `|=` 的副作用。
    ok, problems = judge_a5b(
        policies_count=policies_count,
        verdict_ok=bool(verdict["ok"]),
        verdict_why=str(verdict["why"]),
        fresh_first_outcome=first["outcome"],
    )
    if not ok:
        for problem in problems:
            print(f"  ✗ {problem}")
    print("\n" + ("✓ A5b 成立：累计总预算会被拒绝，且不是 per-tx 规则冒充的" if ok else "✗ A5b 未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
