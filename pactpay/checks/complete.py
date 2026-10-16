"""完成条件自动终止的检查（T9 / R10）。

需求 R10：完成条件（累计花费 / 交易次数 / 时间到期）触发 → Pact 自动终止。

本检查用一个 `tx_count: "1"` 的一次性 Pact：花掉一笔之后，Pact 应当**自动变成
`completed`**，并且**那把 pact-scoped key 立即失效**——失效后的错误码是
2026-10-12 实测出来的 `IDENTITY_INVALID_API_KEY`（HTTP 401），**不是**猜的。

为什么"自动终止"值得单独验：Pact 的四种完成条件里只要有一种不生效，
"授权会在任务结束后自动收回"这句话就不成立——agent 会拿着一把永久有效的钥匙。
"""

from __future__ import annotations

import sys
import time
import uuid
from typing import Any

import httpx

from pactpay.platform.cobo import CoboClient, PolicyDenied
from pactpay.demo.payee_lifecycle import ensure_payee
from pactpay.platform.config import CHAIN_ID as _CHAIN_ID, TOKEN_ID as _TOKEN_ID, load_existing

CHAIN_ID = _CHAIN_ID
TOKEN_ID = _TOKEN_ID
PAYEE_URL = "http://127.0.0.1:8001/api/cheap"
PER_TX_CAP = "0.05"
ONE_SHOT_TX_COUNT = "1"
# `time_elapsed` 探针等多久。取 60 秒：足够短到能跑完，又足够长到
# 不会被"提交后立刻查询"的时序误判。
TIME_ELAPSED_SECONDS = "60"
# `amount_spent` 探针的阈值：正好等于一笔 /api/cheap（0.005），一笔就能打到。
AMOUNT_SPENT_THRESHOLD = "0.005"

# 2026-10-12 实测逐字抄录：Pact 完成后，用同一把 key 再调用得到
# HTTP 401、code=IDENTITY_INVALID_API_KEY、reason=invalid_api_key。
EXPECTED_EXHAUSTED_CODE = "IDENTITY_INVALID_API_KEY"


def build_one_shot_pact_spec(
    chain_id: str = CHAIN_ID, token_id: str = TOKEN_ID, per_tx_cap: str = PER_TX_CAP
) -> dict[str, Any]:
    """一个只允许**一笔**交易的 Pact——验证 R10 的载体。

    `when` 故意只按链/币白名单（不限定目标地址），`deny_if` 给一个宽松的单笔上限，
    这样"第一笔能成功"只可能是这条 allow 规则放行的，不会掺进别的机制。
    """
    return {
        "execution_plan": "验证完成条件：tx_count 达到 1 后 Pact 自动终止、密钥立即失效。",
        "policies": [
            {
                "name": "one-shot-transfer",
                "type": "transfer",
                "rules": {
                    "effect": "allow",
                    "when": {
                        "chain_in": [chain_id],
                        "token_in": [{"chain_id": chain_id, "token_id": token_id}],
                    },
                    "deny_if": {"amount_gt": per_tx_cap},
                },
            }
        ],
        "completion_conditions": [{"type": "tx_count", "threshold": ONE_SHOT_TX_COUNT}],
    }


def build_time_elapsed_pact_spec(
    seconds: str = TIME_ELAPSED_SECONDS,
    chain_id: str = CHAIN_ID,
    token_id: str = TOKEN_ID,
    per_tx_cap: str = PER_TX_CAP,
) -> dict[str, Any]:
    """`time_elapsed` 完成条件的探针（R10 的第三种类型，也是 R1 真正写在 Pact 里的那种）。

    **为什么必须有它**（B8，第 7 步评审阻断项）：计划要求把 R1 的"24 小时后自动失效"
    从"只回读 spec 里写了什么"变成**实测过它真的会失效**；而且 demo Pact 用的完成条件
    类型就是 `time_elapsed`，只验 `tx_count` 等于**没验过 demo 真正依赖的那条路**。
    """
    return {
        "execution_plan": f"验证完成条件：{seconds} 秒后 Pact 自动失效（expired）。",
        "policies": [
            {
                "name": "time-elapsed-probe",
                "type": "transfer",
                "rules": {
                    "effect": "allow",
                    "when": {
                        "chain_in": [chain_id],
                        "token_in": [{"chain_id": chain_id, "token_id": token_id}],
                    },
                    "deny_if": {"amount_gt": per_tx_cap},
                },
            }
        ],
        "completion_conditions": [{"type": "time_elapsed", "threshold": seconds}],
    }


def build_amount_spent_pact_spec(
    threshold: str = AMOUNT_SPENT_THRESHOLD,
    chain_id: str = CHAIN_ID,
    token_id: str = TOKEN_ID,
    per_tx_cap: str = PER_TX_CAP,
) -> dict[str, Any]:
    """`amount_spent` 完成条件的探针（R10 的"累计花费达到上限"）。

    这是**完成条件**、不是拒绝规则——命中后 Pact 终止，而**跨线那笔本身会被执行**。
    正因为它是"终止"而不是"拒绝"，它和 deny 阈值**绝不可以取同一个数**，
    否则完成条件会抢在拒绝之前把 Pact 结束掉（账本里的 R-x10 / 评审 B2）。
    在这个探针里没有 deny 规则跟它抢，所以可以安全地单独验它。
    """
    return {
        "execution_plan": f"验证完成条件：累计花费达到 {threshold} 后 Pact 自动终止（completed）。",
        "policies": [
            {
                "name": "amount-spent-probe",
                "type": "transfer",
                "rules": {
                    "effect": "allow",
                    "when": {
                        "chain_in": [chain_id],
                        "token_in": [{"chain_id": chain_id, "token_id": token_id}],
                    },
                    "deny_if": {"amount_gt": per_tx_cap},
                },
            }
        ],
        "completion_conditions": [{"type": "amount_spent", "threshold": threshold}],
    }


def classify_after_completion(status: int, code: str | None) -> str:
    """Pact 终止后再调用，属于哪一类结果。

    - `"key_invalidated"`：正是期望的——key 随 Pact 终止而失效。
    - `"still_works"`：**完成条件没生效**，key 还活着（R10 不成立）。
    - `"other_error"`：别的错（网络/参数…），不能当成"失效"来记账。
    """
    if code == EXPECTED_EXHAUSTED_CODE:
        return "key_invalidated"
    if status == 200:
        return "still_works"
    return "other_error"


def wait_for_terminal_status(client: CoboClient, pact_id: str, tries: int = 6) -> str:
    """等 Pact 离开 active。实测终止是立刻生效的，但留几次重试以防接口延迟。"""
    status = "unknown"
    for _ in range(tries):
        status = client.get_pact(pact_id)["result"]["status"]
        if status != "active":
            return status
        time.sleep(2)
    return status


def probe_completion_condition(
    client: CoboClient,
    wallet_uuid: str,
    *,
    spec: dict[str, Any],
    label: str,
    expected_status: str,
    pay: bool,
    settle_seconds: int = 0,
) -> dict[str, Any]:
    """跑一个完成条件探针：提交 →（可选）付一笔 → 等 → 断言 Pact 落到期望的终态。

    `expected_status` 用实测值：`tx_count` / `amount_spent` 命中后是 `completed`，
    `time_elapsed` 到期后是 `expired`（文档里这是两个不同的终态）。
    """
    pact = client.submit_pact(
        wallet_id=wallet_uuid,
        intent=f"R10 完成条件探针：{label}",
        name=f"r10-{label}-{uuid.uuid4().hex[:8]}",
        spec=spec,
    )["result"]
    pact_id = pact["pact_id"]
    pact_key = client.get_pact(pact_id)["result"]["api_key"]
    initial = client.get_pact(pact_id)["result"]["status"]

    paid_ok = None
    pay_error = None
    if pay:
        challenge = httpx.get(PAYEE_URL, timeout=20).headers["PAYMENT-REQUIRED"]
        # **必须包 try**（评审 N9）：探针付款若被策略拒（`PolicyDenied`）或网络出错，
        # 原先会直接抛栈退出 `make complete-check`，用户看到的是一段 traceback
        # 而不是"这条完成条件没验成"。异常也是**结论**，要结构化地报出来。
        try:
            result = client.payment(
                wallet_uuid,
                protocol="x402",
                request_id=str(uuid.uuid4()),
                x402_payment_required=challenge,
                api_key=pact_key,
            )
            paid_ok = bool((result.get("result") or {}).get("status") == "completed")
        except Exception as exc:  # noqa: BLE001 —— 异常要变成结论，不是崩掉
            pay_error = f"{type(exc).__name__}: {exc}"
            paid_ok = False

    if settle_seconds:
        time.sleep(settle_seconds)

    final = wait_for_terminal_status(client, pact_id, tries=max(3, settle_seconds // 5 + 3))
    return {
        "label": label,
        "pact_id": pact_id,
        "initial_status": initial,
        "paid_ok": paid_ok,
        "pay_error": pay_error,
        "final_status": final,
        "expected_status": expected_status,
        # **`paid_ok` 必须参与判定**（评审 N9）：探针说"付了款所以完成条件才触发"，
        # 可付款本身失败了却照样报通过，那这条完成条件根本没被触发过。
        "ok": final == expected_status and paid_ok is not False,
    }


def aggregate_exit_code(any_failed: bool, probes: list[dict[str, Any]]) -> int:
    """把各探针的结论聚合成退出码。**抽成纯函数是为了能测**（评审 M17/M31）。

    原先这个聚合**内联在 `main()` 里**，形式是 `failed |= not elapsed["ok"]`。
    把其中任何一行改成 `failed |= False`（即"这条探针的结论不影响退出码"），
    209 个用例**全绿** —— 也就是说"探针说了算不算数"这件事根本没人验过。
    探针离线跑不了（要联网、要测试币），但**聚合逻辑不需要联网**，所以它可以被测。

    `any_failed` 交给调用方传，`probes` 里每个元素必须有 `ok` 与 `label`。
    """
    problems = [p.get("label") for p in probes if not p.get("ok")]
    failed = any_failed or bool(problems)
    if failed:
        detail = f"（未通过：{', '.join(str(x) for x in problems)}）" if problems else ""
        print(f"\n✗ 完成条件检查未通过{detail}")
        return 1
    print("\n✓ R10 成立：tx_count / time_elapsed / amount_spent 三种完成条件都实测触发，密钥立即失效")
    return 0


def main() -> int:
    env = load_existing()
    api_key = env.get("AGENT_WALLET_API_KEY", "")
    wallet_uuid = env.get("AGENT_WALLET_WALLET_ID", "")
    if not (api_key and wallet_uuid):
        print("缺少凭证：先跑 `make t0-probe`。", file=sys.stderr)
        return 2

    # 与 demo / cumulative-check 同样的理由：一条命令要能自己跑完，不要求用户另开终端
    payee_proc = ensure_payee("http://127.0.0.1:8001")
    try:
        return _run(api_key, wallet_uuid, payee_proc)
    finally:
        if payee_proc is not None:
            payee_proc.terminate()


def _run(api_key: str, wallet_uuid: str, payee_proc) -> int:
    failed = False
    # 每条探针的结论都要收进来 —— 聚合由 `aggregate_exit_code` 负责（可测）
    probes: list[dict[str, Any]] = []
    with CoboClient(api_key=api_key) as client:
        pact = client.submit_pact(
            wallet_id=wallet_uuid,
            intent="T9：验证完成条件触发后 Pact 自动终止",
            name=f"t9-complete-{uuid.uuid4().hex[:8]}",
            spec=build_one_shot_pact_spec(),
        )["result"]
        pact_id = pact["pact_id"]
        print(f"1) 提交 tx_count={ONE_SHOT_TX_COUNT} 的 Pact -> status={client.get_pact(pact_id)['result']['status']}")
        pact_key = client.get_pact(pact_id)["result"]["api_key"]

        challenge = httpx.get(PAYEE_URL, timeout=20).headers["PAYMENT-REQUIRED"]
        first = client.payment(
            wallet_uuid,
            protocol="x402",
            request_id=str(uuid.uuid4()),
            x402_payment_required=challenge,
            api_key=pact_key,
        )
        ok = bool((first.get("result") or {}).get("status") == "completed")
        print(f"2) 第 1 笔付款 -> {'成功' if ok else '失败'}")
        tx_count_probe = {"label": "tx-count-payment", "ok": ok}
        probes.append(tx_count_probe)
        failed |= not ok

        status = wait_for_terminal_status(client, pact_id)
        print(f"3) 触发后 Pact status = {status}")
        if status != "completed":
            print(f"   ✗ 期望 completed，实际 {status} —— R10 不成立")
            failed = True

        print("4) 用同一把 key 再调用一次：")
        status_code, code = 0, None
        challenge2 = httpx.get(PAYEE_URL, timeout=20).headers["PAYMENT-REQUIRED"]
        try:
            client.payment(
                wallet_uuid,
                protocol="x402",
                request_id=str(uuid.uuid4()),
                x402_payment_required=challenge2,
                api_key=pact_key,
            )
            status_code = 200
        except PolicyDenied as denied:
            status_code, code = denied.status, denied.code
        except Exception as exc:  # noqa: BLE001 - 要把真实状态码如实报出来
            status_code = getattr(exc, "status", 0)
            code = getattr(exc, "code", None)
            print(f"   异常：{type(exc).__name__}: {str(exc)[:160]}")

        verdict = classify_after_completion(status_code, code)
        print(f"   HTTP {status_code}  code={code}  -> {verdict}")
        if verdict != "key_invalidated":
            print(f"   ✗ 期望 {EXPECTED_EXHAUSTED_CODE}（key 随 Pact 终止失效），实际 {verdict}")
            failed = True

        # ---- R10 的另外两种完成条件类型（B8：原实现把 time_elapsed 探针丢了）----
        print(f"\n5) time_elapsed 探针（{TIME_ELAPSED_SECONDS}s 后应自动 completed）…")
        elapsed = probe_completion_condition(
            client,
            wallet_uuid,
            spec=build_time_elapsed_pact_spec(),
            label="time-elapsed",
            # **实测纠正**：`time_elapsed` 是**完成条件**，命中后 Pact 落到 `completed`。
            # 我原先按字面猜成 `expired`，探针第一次跑就报错了 —— 文档其实写得很清楚：
            # `expired` 专指"Pact 活着、但 `expires_at` 到了，**且没有** time_elapsed 完成条件"。
            # 两者是不同的终止路径，不能混。
            expected_status="completed",
            pay=False,
            settle_seconds=int(TIME_ELAPSED_SECONDS),
        )
        print(f"   status: {elapsed['initial_status']} -> {elapsed['final_status']}"
              f"（期望 {elapsed['expected_status']}）{'✓' if elapsed['ok'] else '✗'}")
        probes.append(elapsed)
        failed |= not elapsed["ok"]

        print(f"\n6) amount_spent 探针（累计达到 {AMOUNT_SPENT_THRESHOLD} 后应自动 completed）…")
        spent = probe_completion_condition(
            client,
            wallet_uuid,
            spec=build_amount_spent_pact_spec(),
            label="amount-spent",
            expected_status="completed",
            pay=True,
        )
        print(f"   付款 {'成功' if spent['paid_ok'] else '失败'}"
              f"{'（' + spent['pay_error'] + '）' if spent.get('pay_error') else ''}；"
              f"status: {spent['initial_status']} -> {spent['final_status']}"
              f"（期望 {spent['expected_status']}）{'✓' if spent['ok'] else '✗'}")
        probes.append(spent)
        failed |= not spent["ok"]

    return aggregate_exit_code(failed, probes)


if __name__ == "__main__":
    raise SystemExit(main())
