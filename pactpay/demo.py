"""PactPay 演示主流程（T4/T5 的真实端到端）。

跑法：
    .venv/bin/python -m pactpay.demo

它做四件事（对应需求 R2→R5）：
1. 读用户给的任务与总预算（R2）
2. 让"大脑"出一份采购计划（R3）
3. 提交一份 **Pact** 把规则写死：只能付给这个商户、单笔 ≤ 0.01、
   24h 累计 ≤ 0.1、24 小时后自动失效（R1 / R2）
4. 按计划逐笔买：限内那笔应成功拿到数据（R4），超限那笔应被策略引擎
   **在签名前**拒绝并给出结构化原因（R5 / R7）

被拒那笔**不会留下任何链上痕迹**——这是"硬边界"的直接证据。
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from pactpay.block_evidence import (
    outcome_to_a5c_shape,
    summarize_a5c,
    check_no_onchain_transfer,
    count_transfers_in_window,
    judge_paid_requires_onchain,
    probe_matched_rule_denial,
    snapshot_wallet,
    wait_for_audit_corroboration,
)
from pactpay.agent import PurchaseOutcome, buy, probe_default_deny
from pactpay.cobo import CoboClient
from pactpay.evidence import (
    OUTCOMES,
    count_outcomes,
    append_record,
    purchase_as_evidence,
    read_chain,
    run_dir_for,
    write_anchor,
    write_counts,
    write_run_meta,
)
from pactpay.planner import default_planner, make_plan, usd_to_atomic
from pactpay.onchain import count_usdc_transfers_from, current_block_number, verify_usdc_transfer
from pactpay.payee_lifecycle import ensure_payee
from pactpay.preconditions import find_balance, require_balance
from pactpay.pact_spec import build_x402_pact_spec
from pactpay.config import CHAIN_ID as _CHAIN_ID, TOKEN_ID as _TOKEN_ID, load_existing

# 用户原始输入（R2）
TASK = "帮我买一份数据报告"
BUDGET_USD = Decimal("0.1")

# Pact 规则（R1）：单笔上限、累计上限、有效期
PER_TX_CAP = "0.01"
ROLLING_24H_CAP = "0.1"
PACT_TTL_SECONDS = "86400"  # 24 小时

PAYEE_BASE = "http://127.0.0.1:8001"
# 一个**不在 Pact 白名单里**的地址：付给它必然"未被任何策略覆盖" → 触发默认拒绝（R6）
# 链/币标识只在 `config.py` 定义一次（N7：原先 demo / cumulative_check / complete_check
# 各写一遍同样的字面量，改一处不传导，属于静默漂移的温床）。
CHAIN_ID = _CHAIN_ID
TOKEN_ID = _TOKEN_ID
OUTSIDE_WHITELIST = "0x1111111111111111111111111111111111111111"
DEFAULT_DENY_AMOUNT = "0.001"
# 计划里**真正会付出去**的钱：只有 /api/cheap 那笔（0.05 那笔会被策略拒绝、
# 默认拒绝那笔也不会付）。前置检查按这个数要余额。
# 本次演示**预计花掉**的钱。它必须是**从计划本身算出来的**，不是一个可以
# 被随手改成 0 的裸常量 —— 评审把 `require_balance(..., Decimal("0"))` 的实参清零，
# 于是"余额够不够"这道前置检查形同虚设，292 条测试全绿（G6）。
# 现在它由 `planned_spend()` 从同一份计划求和得出：**改它就是改计划**，
# 而计划是被 `test_demo_pact_spec` / `test_planner` 钉住的。
def select_planner():
    """选 Agent 的"大脑"：LLM 还是确定性逻辑。

    `PACT_PLANNER=llm`（**默认**）走 `llm_planner`，模型自己决定买哪几个接口；
    `PACT_PLANNER=deterministic` 用写死的两条，供离线跑和测试用。

    **默认是 llm**：这个演示要证明的是"一个自己算出该买什么的 Agent
    撞上了人类划的硬边界"，不是"一个按剧本走的程序走到了拒绝分支"。
    """
    choice = os.environ.get("PACT_PLANNER", "llm").strip().lower()
    if choice == "deterministic":
        return default_planner
    if choice == "llm":
        from pactpay.llm_planner import llm_planner

        return llm_planner
    raise RuntimeError(f"PACT_PLANNER 只能是 llm 或 deterministic，实际 {choice!r}")


def planned_spend(planner=None) -> Decimal:
    """本次演示计划花掉的总金额（各笔求和）。

    用的是**和 `_run` 同一份计划**（同一个 `TASK` / `BUDGET_USD`），
    所以它不可能与真正会发生的支出脱节 —— 想把它改成 0，就得改计划本身，
    而计划是被 `test_planner` / `test_demo_pact_spec` 钉住的（G6）。

    **只累加单笔上限之内的那些**：计划里超过 `PER_TX_CAP` 的那笔**预期就是被拒的**，
    它不会真的花掉钱。所以这个数说的是"本次预计**实际**花掉多少"，
    与原来的 `EXPECTED_SPEND = 0.005` 语义一致。
    """
    plan = make_plan(TASK, BUDGET_USD, planner or select_planner())
    cap = Decimal(PER_TX_CAP)      # PER_TX_CAP 是按 Pact spec 的字符串形式存的
    will_settle = [p for p in plan.purchases if p.amount_usd <= cap]
    if not will_settle:
        raise RuntimeError(
            "计划里没有任何一笔在单笔上限之内 ⇒ 本次演示不会真的花掉钱，"
            "余额前置检查没有意义（这一定是计划或上限被改坏了）"
        )
    return sum((p.amount_usd for p in will_settle), Decimal("0"))


def build_demo_pact_spec(pay_to: str) -> dict:
    """把 R1 的三条规则翻译成 Pact 的 policies + completion_conditions。

    - `when`：链、代币、目标地址三重限定 → 不在白名单里的目标**默认拒绝**（fail-closed）
    - `deny_if.amount_gt`：单笔上限（整币十进制，口径已由 T0 探针实测）
    - `deny_if.usage_limits.rolling_24h.amount_gt`：累计上限——**这是文档里唯一的
      累计"拒绝"机制**。
    - `completion_conditions`：**只用 `time_elapsed`**（R1 说的"24 小时后自动失效"）。

    **为什么完成条件里不放 `amount_spent`**（B2，第 7 步评审阻断项）：
    `amount_spent` 是"自动终止"不是"拒绝"。一旦让它和上面那条累计拒绝阈值**取同一个数**，
    累计爬到该数时**完成条件会先命中**、Pact 进入终态，于是本该被判"累计超限"的那一笔
    拿到的是「Pact 已结束」而不是累计拒绝 —— 演示要证明的硬边界就变成了另一回事，
    而且**不可稳定复现**（取决于两条谁先命中）。这条规则（账本里的 R-x10）现在由
    `tests/test_demo_pact_spec.py` 固化成不变量，不再只写在文档里。

    `amount_spent` / `tx_count` 这两种完成条件类型改由 `complete_check.py` 的
    **独立探针 Pact** 演示（那里没有 deny 规则，不存在阈值抢跑）。
    """
    spec = build_x402_pact_spec(pay_to)
    policy = spec["policies"][0]
    policy["name"] = "demo-allow-x402-to-merchant"
    policy["rules"]["deny_if"] = {
        "amount_gt": PER_TX_CAP,
        "usage_limits": {"rolling_24h": {"amount_gt": ROLLING_24H_CAP}},
    }
    spec["completion_conditions"] = [
        {"type": "time_elapsed", "threshold": PACT_TTL_SECONDS},
    ]
    return spec


def print_plan(plan) -> None:  # noqa: ANN001
    print(f'\n采购任务：「{plan.task}」  总预算：{plan.budget_usd} USDC')
    print("采购计划：")
    for i, p in enumerate(plan.purchases, 1):
        print(f"  {i}. {p.description:8s} {p.resource:14s} {p.amount_usd} USDC")


def describe_chain_trace(outcome: str, settlement_tx: str | None) -> str:
    """把"链上交易"这一行的措辞**按 outcome 派生**，禁止各处手写。

    评审第 3 轮 #1（阻断）：`print_outcome` 的 `else` 分支把 `failed` 也打印成
    「None = 签名前就被拦下，无链上痕迹」。但 `failed` 的含义是**策略放行了这笔、
    结算却失败了**——钱**可能已经动了**，恰恰**必须**去链上核对。
    把这两种截然不同的处境印成同一句话，是把这个项目要证明的事**说反**。

    这正是 N4 的形态（看板上修了"failed 不许说成被策略拒绝"，控制台却没修）。
    修法不是就地改那一行，而是**把措辞收进一个函数**：调用方只能给 outcome，
    措辞由这里唯一决定，两个渲染层再也不会漂移。
    """
    if outcome == "paid":
        return f"链上交易：{settlement_tx}"
    if outcome == "denied":
        return (
            f"链上交易：{settlement_tx}"
            f"（None = 签名前就被拦下，钱根本没动）"
        )
    if outcome == "failed":
        return (
            f"链上交易：{settlement_tx}"
            "（**付款未结算：策略放行了这笔，钱可能已经动了——必须人工核对链上，"
            "绝不能当成'边界拦住了'**）"
        )
    return f"链上交易：{settlement_tx}（未知 outcome={outcome!r}，不予解释）"


TAGS = {"paid": "✅ 支付成功", "denied": "⛔ 被策略拒绝", "failed": "⚠️  付款未结算"}


def _wait_for_block_after(settled_block: int | None, timeout_s: float = 60.0) -> int:
    """等到链头**严格晚于** `settled_block`，返回那个区块号（第 4 轮评审 F3）。

    A5c③ 要在一个"本运行自己那笔成功付款**不可能**落进来"的窗口上数 USDC 转账。
    最省事的做法是构造窗口时 `max(block_before, settled_block + 1)` —— 但那是
    **静默收窄**：如果这笔尝试真的落在 `settled_block` 那一块里，窗口会把它排除掉，
    结论就建立在一个比实际尝试**更小**的区间上；而且收窄之后
    `window[0] > control_window[1]` 恒成立，"相交则中止"那条断言**永远为假**——
    守卫沦为装饰。

    这里改成**把前提真正满足**：等出一个更新的区块再采样，等不到就抛。
    于是后续那条断言检查的是一个**可能不成立**的前提，而不是一句恒真的话。

    Base Sepolia 出块约 2 秒，60 秒上限足够；超时说明 RPC 有问题，
    这种情况下"判不了"远好过"给一个覆盖不全的窗口然后打 ✅"。
    """
    if settled_block is None:
        raise RuntimeError(
            "A5c 失败：本次运行里没有任何已结算的付款，无法为「无链上痕迹」做阳性对照 "
            "—— 没有对照就不能给通过（宁可不判，也不给恒真的绿灯）"
        )
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        head = current_block_number()
        if head is not None and head > settled_block:
            return head
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"A5c 失败：等了 {timeout_s:.0f} 秒也没等到晚于已结算区块 "
                f"{settled_block} 的新区块（链头 {head}）—— 无法构造一个"
                "「不含本运行自己那笔成功付款」的判定窗口，宁可不判"
            )
        time.sleep(1.0)


def failed_a5c_blocks(x402: dict, matched: dict) -> list[str]:
    """哪几条 A5c 判据**明确不成立**（纯函数）。

    只看 `is False`，不看"缺失"：`None` 表示"没测"，`False` 表示"测了，没通过"。
    把"没测"当成"没通过"会假报警；把"没通过"当成"没测"就是**把没证明当成证明了**。
    """
    return [
        name
        for name, block in (("x402 被拒那笔", x402), ("命中规则那笔", matched))
        if block.get("structured_denial") is False or block.get("no_chain_trace") is False
    ]


def require_a5c_holds(*, x402: dict, matched: dict) -> None:
    """A5c 判据没通过 ⇒ **中止整个演示**（纯函数，便于脱网单测）。

    这是本项目诚实性的最后一道闸：A5c③ 是"钱没动"的唯一证据，A5c① 是"拒绝是结构化的"的
    唯一证据。任一条**明确**不成立却继续跑完、照样落盘、照样 `exit 0`，
    就等于**把"没证明"当成"证明了"** —— 正是这个项目存在的意义所要防的那件事。

    **为什么抽成纯函数**（第 5 轮自查变异）：原来这段内联在 `main()` 里，
    覆盖它的测试是**读 AST** 找"某个 if 里有 a5c_failures 和 raise"。
    那种测试只钉住**写法**：把条件改成 `if False and a5c_failures:`、
    或者把它挪进一个永不执行的 `if`，AST 里那些名字和 `raise` 全都还在。
    这里改成**调函数看它抛不抛**。
    """
    failures = failed_a5c_blocks(x402, matched)
    if not failures:
        return
    detail = "；".join(
        f"{name} ①={'✅' if (x402 if name.startswith('x402') else matched).get('structured_denial') else '✗'}"
        f" ③={'✅' if (x402 if name.startswith('x402') else matched).get('no_chain_trace') else '✗'}"
        for name in failures
    )
    raise RuntimeError(
        f"A5c 失败（R7 未成立）：{detail}"
        " —— 判据没有通过，演示不给出成功结论（宁可不判，也不给恒真的绿灯）"
    )


def guard_a5c(trace: dict, *, label: str) -> dict:
    """A5c③ 的**中止判据**（纯函数，便于脱网单测）。

    **为什么抽出来**（第 5 轮自查变异）：原来这行内联在 `_run` 的采购循环里，
    写成 `if not trace["ok"]: raise`。把它改成 `if False:` 时 **292 条测试全绿** ——
    因为当时唯一"覆盖"它的测试 (`test_the_demo_aborts_when_a5c_does_not_hold`)
    是**读 AST** 找"某个 if 里有 a5c_failures 和 raise"，而它找到的其实是**另一个**
    if（聚合那一处），真正这行中止从来没被行为测试碰过。

    这条判据是**整个项目诚实性的最后一道闸**：A5c③ 是"钱没动"的**唯一**证据，
    它不成立却继续跑完、照样落盘、照样 exit 0，就等于**把"没证明"当成"证明了"**。
    所以它必须被"能调用的行为"钉住，而不是被"长得像"钉住。
    """
    if not trace.get("ok"):
        raise RuntimeError(f"A5c 失败（{label}）：{trace.get('why')}")
    return trace


def render_a4_ok(a4: dict[str, Any]) -> str:
    """把 A4 链上复核的结论渲染成**带 ✅ 的那一行**。

    **自己先验，再渲染**（第 5 轮评审 G4）：✅ 的产出与闸门必须是**同一处**。
    分开写的后果是评审实测过的那条 ——
    把守卫弱化成 `if not a4["ok"] and not a4["transfers"]:`，
    复核**失败**时照样打出 `A4 链上复核 ✅ … 0xdead → 0xbeef` 并把记录记成 paid。

    也拒绝"没测量过"的形状：`checked=False` 意味着压根没去链上查，
    那种情况下**不存在**可以渲染的 ✅。
    """
    if not a4.get("ok"):
        raise RuntimeError(f"A4 链上复核未通过，不得渲染成 ✅：{a4.get('why')}")
    if not a4.get("checked"):
        raise RuntimeError("A4 没有真的去链上查过（checked=False）⇒ 不许渲染 ✅")
    transfers = a4.get("transfers") or []
    if not transfers:
        raise RuntimeError("A4 判了 ok 却没给出任何转账明细 ⇒ 无法渲染（宁可不判）")
    moved = transfers[0]
    return (
        f"A4 链上复核 ✅ 区块 {a4['block_number']}，"
        f"USDC {moved['value']} 原子单位 {moved['from']} → {moved['to']}"
    )


def purchase_trace_lines(
    outcome: PurchaseOutcome, a4: dict[str, Any]
) -> tuple[list[str], int | None]:
    """一笔采购结束后要打印的**链上相关内容**，以及本轮新的"已结算区块"。

    返回 `(要打印的行, 新的 settled_block 或 None)`。

    **为什么把这行抽出来**（第 5 轮评审 G4）：判定逻辑原先内联在 `_run` 里，
    而 `_run` 没有测试覆盖。评审把调用点换成 `a4_line = "硬编码 ✅"`，
    或者把 `if outcome.outcome != "paid": return None` 改成 `if False:`，
    **都没有测试变红**。

    根子还是那句：**函数被测住了，调用点没有。**
    所以把"调用点"本身变成一个函数，让测试能直接打到它。

    `settled_block` 只由**已支付**的那笔产生 —— 它是 A5c③ 阳性对照的坐标，
    被拒的那笔没有链上交易，给不出这个坐标。
    """
    line = a4_line_for(outcome, a4)
    if line is None:
        return [], None
    return [line], int(a4["block_number"])


def a4_line_for(outcome: PurchaseOutcome, a4: dict[str, Any]) -> str | None:
    """**只有已支付的那笔**才有"链上复核"可渲染 —— 其余返回 None。

    **为什么单独抽出来**（第 5 轮评审 G4）：原来是内联在 `_run` 里的
    `if outcome.outcome == "paid": ... render_a4_ok(a4)`，而 `_run` 没有测试覆盖，
    于是"把 `== \"paid\"` 改成 `if True`"这种改动**无人变红** —— 被拒的那笔也会去
    渲染链上复核，而它压根没有链上交易可核。

    注意这里**不是**"没结算哈希就不验"那个逃逸写法：能不能渲染由 outcome 决定，
    而"记成 paid 就必须有可核的结算"由 `judge_paid_requires_onchain` **强行**保证
    （它会 raise）。`render_a4_ok` 自己也会在 `checked=False` 时 raise。
    """
    if outcome.outcome != "paid":
        return None
    return render_a4_ok(a4)


def print_outcome(outcome: PurchaseOutcome) -> None:
    # 三种结果必须分开说：`denied` 是**边界生效**（预期），`failed` 是**基础设施故障**（异常）。
    # 混成一个"没成功"会让看板把故障读成边界生效——正好把这个项目要证明的事说反。
    #
    # **`kind` 只读一次，标签与措辞都用它**（第 4 轮评审 F1，阻断项）。
    # 上一版从 `outcome.outcome` 现取两处、还把 `'paid'` 写死成字面量：
    # 于是把第三处的实参改成 `'denied'`，就能让 `failed` 打印出
    # 「签名前就被拦下，钱根本没动」**而 254 条测试全绿** ——
    # `describe_chain_trace` 孤立测得很漂亮，但**调用方挑的那个字符串没人管**。
    # 收成一个局部变量之后，"挑错 outcome"会同时改掉上面那行标签，一眼就看得出。
    kind = outcome.outcome
    tag = TAGS.get(kind)
    if tag is None:
        raise ValueError(
            f"未知的采购结果 {kind!r}：标签与措辞都不知道该怎么说它。"
            f"**不许猜**——猜错就会把故障说成边界生效。已知：{sorted(TAGS)}"
        )
    print(f"\n  {tag}  {outcome.resource}（{outcome.amount_usd} USDC，{outcome.description}）")
    if kind == "paid":
        print(f"     {describe_chain_trace(kind, outcome.settlement_tx)}")
        print(f"     拿到数据：{outcome.data}")
    else:
        d = outcome.denial or {}
        print(f"     HTTP {outcome.http_status}  码：{d.get('code')}")
        print(f"     触发原因：{d.get('policy_reason') or d.get('reason')}（{d.get('reject_reason')}）")
        print(f"     命中策略：{d.get('policy_id')}")
        print(f"     {describe_chain_trace(kind, outcome.settlement_tx)}")


def main() -> int:
    env = load_existing()
    api_key = env.get("AGENT_WALLET_API_KEY", "")
    wallet_uuid = env.get("AGENT_WALLET_WALLET_ID", "")
    agent_address = env.get("AGENT_WALLET_ADDRESS", "")
    pay_to = env.get("PAY_TO_ADDRESS", "")
    if not (api_key and wallet_uuid and pay_to):
        print("缺少凭证：先跑 `make t0-probe`")
        return 1

    # A11a：自带拉起收款方——"一条命令跑完"必须是真的，不能要求用户另开一个终端。
    # 已经起着就复用（返回 None，收尾时不去动别人的进程）。
    payee_proc = ensure_payee(PAYEE_BASE)
    if payee_proc is not None:
        print(f"已自动拉起收款方 {PAYEE_BASE}")
    try:
        return _run(api_key, wallet_uuid, agent_address, pay_to)
    finally:
        if payee_proc is not None:
            payee_proc.terminate()


def _run(api_key: str, wallet_uuid: str, agent_address: str, pay_to: str) -> int:

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    planner = select_planner()
    plan = make_plan(TASK, BUDGET_USD, planner)
    print(f"\nAgent 大脑：{getattr(planner, '__name__', planner)}"
          f"（PACT_PLANNER={os.environ.get('PACT_PLANNER', 'llm')}）")
    print_plan(plan)
    # 证据目录现在就建：链记录要**边跑边追加**，不是跑完一次性写
    run_dir = run_dir_for(run_id, started_at)
    chain_path = run_dir / "chain.jsonl"
    # 运行时**自己**维护的计数器（与链是两个独立来源，校验时对账用）
    counter: dict[str, int] = {}

    with CoboClient(api_key=api_key) as client:
        print(f"\n提交 Pact（单笔 ≤ {PER_TX_CAP}，24h 累计 ≤ {ROLLING_24H_CAP}，"
              f"{int(PACT_TTL_SECONDS) // 3600}h 后失效）…")
        pact = client.submit_pact(
            wallet_id=wallet_uuid,
            intent=f"{TASK}，最多花 {BUDGET_USD} USDC",
            spec=build_demo_pact_spec(pay_to),
            name=f"pactpay-demo-{run_id[:8]}",
        )["result"]
        pact_id = pact["pact_id"]
        pact_status = client.get_pact(pact_id)["result"]["status"]
        print(f"  Pact {pact_id}  status={pact_status}")

        # 用 **pact-scoped key**（不是根 key）执行——策略才真正挂在这次执行上
        pact_key = client.get_pact(pact_id)["result"]["api_key"]

        # ---- 前置检查：余额够不够（不够就明确报错，绝不允许跑出"缺一笔"的假证据）----
        balances = client.list_balances(wallet_uuid, chain_id=CHAIN_ID, token_id=TOKEN_ID).get("result") or []
        balance_ok = find_balance(balances, CHAIN_ID, TOKEN_ID) or {}
        expected_spend = planned_spend()
        print(f"\n前置检查：余额 {balance_ok.get('amount', '?')} USDC，本次预计花掉 {expected_spend} USDC")
        require_balance(balances, CHAIN_ID, TOKEN_ID, expected_spend, label="本次演示")

        print("\n开始逐笔采购：")
        # **运行窗口的左端**：链上对账要用它去数"这段时间发生过几笔转账"（评审 G1）。
        # 必须在任何一笔付款**之前**取，否则会漏掉第一笔。
        run_start_block = current_block_number()
        outcomes: list[PurchaseOutcome] = []
        # A5c③ 的阳性对照需要"已知发生过转账的区块"，就是第一笔付款的结算区块。
        settled_block: int | None = None
        # x402 被拒那笔的 ③ **实测结果**（后面打印要用它，不许硬编码）
        x402_trace: dict | None = None
        for purchase in plan.purchases:
            # 钱包快照仍然保留（写进证据供人看），但它**不再是**"无链上痕迹"的判据——
            # 实测这个度量滞后、不敏感（见下面 A5c③ 处的说明）。
            wallet_before = snapshot_wallet(
                client, wallet_uuid, pact_id, chain_id=CHAIN_ID, token_id=TOKEN_ID
            )
            # A5c③ 的判定窗口：这笔尝试**前后紧邻**的区块区间。
            #
            # **下界必须严格晚于本运行自己那笔已结算付款的区块**（第 4 轮评审 F3，阻断项）。
            # 上一版写的是 `block_before = current_block_number()`，然后在构造窗口时
            # `lower = max(block_before, settled_block + 1)` **悄悄把窗口收窄**。
            # 两个问题：
            #   ① 收窄是静默的 —— 如果这笔尝试真的落在 `settled_block` 那一块里，
            #      窗口会把它**排除掉**，于是"没找到痕迹"是在一个**比实际尝试更小**的
            #      区间上得出的结论（覆盖缺口）；
            #   ② 收窄之后 `window[0] > control_window[1]` 恒成立，那条"相交则中止"
            #      的断言**永远不可能触发**——守卫是戏。
            # 现在把前提**显式满足**：等出一个严格更新的区块再采样，然后断言这个前提，
            # 断言就成了真的能失败的东西（等不到就中止，而不是偷偷缩小范围）。
            #
            # 但**第一笔采购时 `settled_block` 本来就是 None**（还没有任何已结算的付款），
            # 这时"晚于结算区块"没有适用对象，照常采样即可 —— 这不是放行：
            # 下面构造 A5c③ 窗口时会因为 `settled_block is None` 而**直接中止**，
            # 所以不存在"没有阳性对照却给出 ✅"的路径。
            if settled_block is None:
                block_before = current_block_number()
            else:
                block_before = _wait_for_block_after(settled_block)
            outcome = buy(
                client, PAYEE_BASE, purchase, wallet_uuid=wallet_uuid, pact_api_key=pact_key
            )
            block_after = current_block_number()
            wallet_after = snapshot_wallet(
                client, wallet_uuid, pact_id, chain_id=CHAIN_ID, token_id=TOKEN_ID
            )
            print_outcome(outcome)

            # A4：链上复核。只查 receipt 存在只能证明"链上发生过一笔交易"，
            # 证明不了"付款方的钱真的动了、且动的是这一笔"。所以要解出 USDC 的
            # Transfer 事件，逐字段核对 **from / to / value** 三个要素。
            #
            # **守卫不许写成 `if outcome.outcome == "paid" and outcome.settlement_tx:`**
            # （B3，第 7 步评审阻断项）：那样写等于"没有结算哈希就不验"，
            # 而"没有结算哈希却记成 paid"正是要防的静默降级——守卫本身成了逃逸口。
            # 现在的口径是：**凡是记成 paid 的，必须能通过链上复核**，拿不出哈希就直接炸。
            # 守卫本体抽到 `judge_paid_requires_onchain`，**为了能脱网单测**（N3/M4）。
            # 内联时"改回逃逸写法"没有任何测试会红。
            a4 = judge_paid_requires_onchain(
                outcome,
                verify_usdc_transfer,
                expect_from=agent_address,
                expect_to=pay_to,
                expect_value=int(usd_to_atomic(purchase.amount_usd)),
            )
            # **闸门与 ✅ 在同一处**（第 5 轮评审 G4）。
            # 原先写成"上面一个 `if not a4['ok']: raise`，下面一个 `if a4['checked']: print ✅`"，
            # 评审把上面弱化成 `if not a4["ok"] and not a4["transfers"]:` 就能在
            # **复核失败**的情况下打出 `A4 链上复核 ✅ … 0xdead → 0xbeef` 并记成 paid。
            # 根子还是"判据与它的产物分了家"。现在 `render_a4_ok` **自己先验再渲染**：
            # 想打出这个 ✅，就必须先通过 `a4["ok"]`，没有第二条路。
            # A4 只对**已支付**的那笔有意义（判定逻辑见 `purchase_trace_lines`）。
            trace_lines, new_settled_block = purchase_trace_lines(outcome, a4)
            for trace_line in trace_lines:
                print(f"     {trace_line}")
            if new_settled_block is not None:
                settled_block = new_settled_block

            payload = purchase_as_evidence(
                    outcome,
                    agent_address=agent_address,
                    pay_to=pay_to,
                    amount_atomic=int(usd_to_atomic(outcome.amount_usd)),
                )
            payload["wallet_before"] = {k: str(v) for k, v in wallet_before.items()}
            payload["wallet_after"] = {k: str(v) for k, v in wallet_after.items()}
            if outcome.outcome == "failed":
                # `failed` 是**异常**结果（策略放行了但没结算成功），钱**可能已经动了**。
                # 它不属于"A5c③ 被拒那笔无链上痕迹"要证的那件事——硬套会得出
                # "被拒那笔留下了链上痕迹"这种**说反了**的结论，所以单独报出来。
                raise RuntimeError(
                    f"付款未结算（{outcome.resource}）：{outcome.denial.get('reason') if outcome.denial else ''}"
                    " —— 钱可能已经动了，请人工核对钱包余额后再重跑"
                )
            if outcome.outcome == "denied":
                # A5c③：**被策略拒绝**那笔必须没有链上痕迹。
                #
                # **判据在评审第 2 轮被换掉（N2，阻断项）**：原来比的是"钱包余额与 Pact
                # 交易计数在紧邻窗口内没变"。但实测证明那个度量**不敏感** ——
                # Cobo 余额接口滞后，17 次真实运行里 16 次，**确已链上结算**的 paid 那笔
                # 同样报"余额未变"；`progress_tx_count` 更是 17/17 全为 0→0。
                # 也就是说那个 ✅ 是**恒真**的：它证明不了"钱没动"，
                # 只证明了"这个仪表看不见钱动"。
                #
                # 现在改为**链上日志**（无滞后），并且**先做阳性对照**：
                # 同一个查询必须在"已知发生过转账"的那**一个区块**上数到 ≥1 笔；
                # 对照不过就报"判不了"，**绝不给 ✅**。
                #
                # `settled_block is None` = 本运行**没有任何已结算的付款**，
                # 于是拿不到阳性对照。这时必须中止：没有对照，"数到 0 笔"证明不了任何事
                # （宁可不判，也不给一个恒真的绿灯）。
                if settled_block is None:
                    raise RuntimeError(
                        "A5c 失败：本次运行里没有任何已结算的付款，无法为「无链上痕迹」"
                        "做阳性对照 —— 没有对照就不能给通过"
                    )
                if None in (block_before, block_after):
                    raise RuntimeError("A5c 失败：拿不到区块高度，无法判定窗口")

                # **目标窗口必须与"本运行自己那笔已结算付款"不相交**（评审第 3 轮 #2，阻断项）。
                #
                # 原来 `window = (block_before, block_after)`，而 `block_before` 是在
                # 发起这笔**被拒**采购之前取的。可 `settled_block` 是**上面那笔 paid**
                # 的结算区块 —— 两者只差几秒，实测两个真实运行的余量都**只有 1 个区块**
                # （`window[0] == settled_block + 1`）。只要 RPC 头稍微滞后、
                # 或那笔 paid 的结算区块正好等于 `block_before`，**已结算的转账就会落进
                # 被测窗口**，于是 `count != 0` → 判 `trace_found` → demo 报
                # "钱真的动了，不是签名前拦住" —— 拿**自己那笔成功的付款**
                # 去指控**这笔被拒的付款**留下了痕迹。
                #
                # **窗口下界严格晚于结算区块 —— 这是上面 `_wait_for_block_after` 保证的。**
                # 这里断言的是那个**前提**，不是一句恒真的话：等不到新区块时
                # `_wait_for_block_after` 已经中止；万一它被改坏（比如改成返回
                # `settled_block` 本身），这条断言就是能失败的兜底。
                #
                # 旧写法 `lower = max(block_before, settled_block + 1)` 是**静默收窄**：
                # 它让 `window[0] > control_window[1]` 恒成立，于是"相交则中止"永远不触发
                # —— 评审用一个穷举搜索证明了那个分支在 [0,40)³ 上**不可能被触发**。
                # **窗口不在这里拼**（第 5 轮评审 G5）：
                # 原先调用点自己拼 `window=(block_before, block_after)`，于是"窗口有多宽"
                # 由调用点说了算 —— 评审把它改成 `(block_after, block_after)`
                # （整体挪到尝试之后）或让 `block_after = block_before`（宽度 0），
                # 292 条测试全绿而屏幕照样打 ✅。
                # 现在只交出三个**采样事实**，窗口由 `check_no_onchain_transfer`
                # 自己构造并校验形状（详见 `build_measurement_window`）。
                trace = check_no_onchain_transfer(
                    subject=agent_address,
                    block_before=block_before,
                    block_after=block_after,
                    control_block=settled_block,
                    count_fn=count_transfers_in_window,
                )
                payload["no_chain_trace"] = {
                    "ok": trace["ok"],
                    "verdict": trace["verdict"],
                    "why": trace["why"],
                    # 窗口从**判据的返回**里取：它才是构造窗口的地方（G5）。
                    # 调用点手里根本没有 window 变量，也就没法偷偷收窄它。
                    "window": trace["window"],
                    "control_window": trace["control_window"],
                    "count": trace["count"],
                    "control_count": trace["control_count"],
                }
                guard_a5c(trace, label="x402 被拒那笔")
                x402_trace = payload["no_chain_trace"]
                print(f"     A5c③ 无链上痕迹：{trace['why']}")

            outcomes.append(outcome)
            # 当场入链（append-only），并更新独立计数器
            append_record(chain_path, payload, kind="purchase")
            counter[outcome.outcome] = counter.get(outcome.outcome, 0) + 1

        # ---- R6 默认拒绝（走**转账**路径，所以这条拒绝**会**进 Cobo 审计日志）----
        print("\n默认拒绝检查（付给白名单外的地址）：")
        default_deny = probe_default_deny(
            client,
            wallet_uuid,
            pact_key=pact_key,
            chain_id=CHAIN_ID,
            token_id=TOKEN_ID,
            src_addr=agent_address,
            dst_addr=OUTSIDE_WHITELIST,
            amount=DEFAULT_DENY_AMOUNT,
        )
        if default_deny["outcome"] != "denied":
            raise RuntimeError(
                "默认拒绝没有生效：一笔未被任何策略覆盖的转账竟然通过了。"
                "R6（fail-closed）不成立，本次演示结果不可信。"
            )
        print(f"  ⛔ 被默认拒绝  {OUTSIDE_WHITELIST}")
        print(f"     拒绝码：{default_deny['code']}")
        print(f"     触发原因：{default_deny['policy_reason']}（没有任何允许策略匹配）")
        print(f"     {describe_chain_trace(default_deny['outcome'], default_deny['settlement_tx'])}")
        append_record(chain_path, default_deny, kind="default_deny")
        # 稍等，让 Cobo 把这条拒绝写进审计日志（实测写入很快，但接口有轻微延迟）
        time.sleep(3)

        # R8：审计日志直接引 Cobo 那一份（自己写的日志是自证的，证明不了什么）。
        # 实测：不带 wallet_id 只看得到"调用者自己"的动作，带 wallet_id 才看得全。
        # ---- A5c② 需要一个**命中规则**的拒绝（带 policy_id），
        # 因为默认拒绝那条结构上就没有 policy_id，x402 那条又不进审计日志。
        print("\nA5c② 探针：故意打一笔超过单笔上限的转账（应命中 deny_if）…")
        # ③ 要求"钱真的没动"是**测出来的**：探针前后各采一次钱包快照再比对。
        # 原先这里从没采样过，打印出来的"③ 无链上痕迹 ✅"是硬编码字面量（评审 B7）。
        before_probe = snapshot_wallet(client, wallet_uuid, pact_id, chain_id=CHAIN_ID, token_id=TOKEN_ID)
        probe_block_before = current_block_number()
        matched = probe_matched_rule_denial(
            client,
            wallet_uuid,
            pact_key=pact_key,
            chain_id=CHAIN_ID,
            token_id=TOKEN_ID,
            src_addr=agent_address,
            dst_addr=pay_to,
            amount=str(Decimal(PER_TX_CAP) * 5),   # 0.05 > 单笔上限 0.01
        )
        probe_block_after = current_block_number()
        after_probe = snapshot_wallet(client, wallet_uuid, pact_id, chain_id=CHAIN_ID, token_id=TOKEN_ID)
        # 探测活动留下的链上痕迹也要用**同一个敏感判据**（链上日志 + 阳性对照）。
        # 快照仍然采（写进证据给人看），但快照差**不作判据** —— 它滞后、不敏感。
        # 阳性对照取**恰好那一个区块**（收据已明确告知），不用 ±5：见上面同款说明。
        matched_trace = (
            check_no_onchain_transfer(
                subject=agent_address,
                block_before=probe_block_before,
                block_after=probe_block_after,
                control_block=settled_block,
                count_fn=count_transfers_in_window,
            )
            if settled_block is not None and None not in (probe_block_before, probe_block_after)
            else None
        )
        print(f"  拒绝码 {matched['code']}  policy_id={matched['policy_id']}")

        audits = client.list_audit_logs(wallet_id=wallet_uuid, limit=50)
        audit_rows = ((audits or {}).get("result") or {}).get("items") or []

        # ---- A5c 证据小结（R7：拒绝发生在**签名之前**）----
        # 三重证据里 ① ③ 对两条路径都成立；**② 只对转账路径成立**：
        # 实测确认 x402 付款的拒绝**不进审计日志**（等 2/8/20 秒都没有）。
        # 所以这里对转账那条要真找到，对 x402 那条**如实说取不到**——不放宽、不冒充。
        # 实测：审计行**比操作晚一点**才写进去，查询抢在前面会误判成"审计里没有"。
        # 所以这里轮询等待（有上限），而不是查一次就下结论。
        corroboration = wait_for_audit_corroboration(
            client,
            wallet_uuid,
            reason=matched["reason"],
            action="transfer.denied",
            since=started_at,
        )
        x402_denied = [o for o in outcomes if o.outcome == "denied"]
        a5c = summarize_a5c(
            outcome_to_a5c_shape(x402_denied[0]) if x402_denied else None,
            matched,
            corroboration,
            matched_trace,
            x402_trace,
        )
        x4 = a5c["x402_denial"]
        mr = a5c["matched_rule_denial"]

        # 打印的每个数字/勾都来自上面那份**测量结果**，不再有硬编码的 "403 ✅"
        print("\nA5c 签名前拦截·三重证据：")
        if x4["structured_denial"]:
            # ③ 的措辞与勾**都来自实测**（链上窗口 + 阳性对照）。
            # 原先这里硬编码 `③ 余额/交易数未变 ✅`，是评审 B7 点名的那类假证据。
            print(f"  x402 被拒那笔  ① HTTP {x4['http_status']} 结构化拒绝 ✅（码 {x4['code']}）"
                  f"  ③ 无链上痕迹 {'✅' if x4['no_chain_trace'] else '✗'}（{x4['no_chain_trace_why']}）"
                  f"  ② 审计佐证 ✗（{x4['why_audit_missing']}）")
        print(f"  命中规则那笔    ① HTTP {mr['http_status']} {'✅' if mr['http_status'] else '✗'}"
              f"  ③ 无链上痕迹 {'✅' if mr['no_chain_trace'] else '✗'}（{mr['no_chain_trace_why']}）"
              f"  ② {corroboration['why']}")
        print(f"     policy_id 交叉核对：探针 {mr['probe_policy_id']} vs 审计 {mr['audit_policy_id']}"
              f" → {'✅ 一致' if mr['policy_id_matches_audit'] else '✗ 不一致或缺失'}")
        print(f"  （默认拒绝那笔的审计行**结构上没有 policy_id**——本来就没策略被命中，"
              f"这正是 fail-closed 的含义）")

        # **判据说"没通过"时，整个演示必须失败退出**（第 4 轮评审 F3c，阻断项）。
        #
        # 原来这里打印完就往下走：A5c③ 显示 ✗（比如阳性对照失败、或真的测到痕迹），
        # demo 却照样 `return 0`、证据照样落盘 —— 于是一份**没有通过 R7 的运行**
        # 在外壳看来是成功的。评审指出：`mr["no_chain_trace"] is False` 时无人中止。
        #
        # 这正是本项目最该避免的那种失败：**把"没证明"当成"证明了"**。
        # 判据说什么就是什么——不通过就中止，让人看见。
        require_a5c_holds(x402=x4, matched=mr)

    paid = [o for o in outcomes if o.outcome == "paid"]
    denied = [o for o in outcomes if o.outcome == "denied"]

    write_run_meta(
        run_dir,
        {
            "run_id": run_id,
            "started_at": started_at,
            "task": TASK,
            "budget_usd": str(BUDGET_USD),
            "pact_id": pact_id,
            "pact_status": pact_status,
            "payee": PAYEE_BASE,
            "wallet_uuid": wallet_uuid,
            "agent_address": agent_address,
            "audit_logs": audit_rows,
            # A5c 小结与上面打印的是**同一份**测量结果（`summarize_a5c`），
            # 不在两处各拼一遍——原先这里 `policy_id` 写了两遍、后者覆盖前者，
            # 把 A5a 要的"探针 policy_id == 审计 policy_id"交叉核对在产物里销毁了（评审 B7）。
            "a5c": a5c,
        },
    )
    # **写进产物的计数器要把三个键都写全**（第 4 轮评审 F10）。
    # `counter` 只在某个结果真的出现过时才建键，所以一次"1 成功 1 被拒"的运行
    # 写出来只有 `{"paid": 1, "denied": 1}` —— 而提交信息里写着"计数器写全三个键
    # （缺席记 0）"。产物和说明对不上，看的人没法判断 `failed` 到底是 0 还是**没记**。
    # 补零**只补键**：值是运行时自己数的，不从链里反推（`counts_ok` 靠这个独立性才有意义）。
    write_counts(run_dir, {name: counter.get(name, 0) for name in OUTCOMES})
    # 链的锚：总条数 / 分 kind 条数 / 尾哈希。
    # 没有它的话，链尾那条 `default_deny`（R6 的唯一证据）删掉也没人发现（评审 G2）。
    write_anchor(
        run_dir,
        read_chain(chain_path),
        # 外部坐标：agent 地址 + 本次运行的区块窗口。
        # `verify` 用它们去链上数转账笔数，与证据里的 paid 条数对账 ——
        # 这是唯一能发现"成功那笔被擦掉了"的判据（评审 G1）。
        agent_address=agent_address,
        from_block=run_start_block,
        to_block=current_block_number(),
    )

    print("\n" + "=" * 64)
    tally = count_outcomes([purchase_as_evidence(o) for o in outcomes])
    print(
        f"汇总：成功 {tally['paid']} 笔，被拒 {tally['denied']} 笔"
        + (f"，未结算 {tally['failed']} 笔" if tally["failed"] else "")
    )
    print(f"证据：{run_dir}/（链 {len(outcomes) + 1} 条 + {len(audit_rows)} 条 Cobo 审计行）")
    print(f"校验：python -m pactpay.evidence.verify {run_dir}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
