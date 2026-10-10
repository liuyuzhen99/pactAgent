"""A5c：证明"拒绝发生在**签名之前**"（R7）。

R7 说："策略引擎在签名前直接拒绝，而不是事后发现。"要证明这一点，不能只看"收到一个拒绝"
——那也可能是"先付了、再回滚"（虽然测试网上不会真回滚，但**论证上**必须排除）。

**三重独立证据**（缺一不可）：
1. 收到**结构化拒绝**（带机器可读的码；HTTP 状态是**实测值**，不同路径不同 ——
   超限转账是 403，超限的 x402 付款是 422。**不许硬编码 403**）；
2. 该拒绝能在 Cobo 审计日志里**按 action 过滤**找到，且带 `policy_id`
   （不过滤会把同一时段别的拒绝记录混进来）。**注意 x402 付款的拒绝实测不进审计日志**，
   这条对它是"取不到"，如实标注而非冒充；
3. **链上确实没有产生转账** —— 判据是 `eth_getLogs` 数该地址在窗口内的 USDC `Transfer`，
   **并配阳性对照**（同一查询必须在已知发生过转账的窗口上数到 ≥1 笔）。
   评审第 2 轮 N2 换掉的旧判据是"紧邻两次钱包快照的余额/交易数不变"：
   那个度量**不敏感** —— 实测 17 次运行里 16 次，**确已结算**的付款同样报"余额未变"，
   `tx_count` 更是 17/17 全为 `0→0`。它的"没变化"分不清"钱没动"和"仪表看不见钱动"。

**为什么不用 `tx_hash is None`**：实测确认 x402 付款的 `PaymentResult.tx_hash`
**成功时也是 null**（文档：只对 `request_close`/`withdraw` 返回）。"为空"对成功与失败
**都成立**，所以它**不构成任何证据**——拿它当判据等于"功能全坏也过"。
这是方案里被抓出来的最严重缺陷，本模块就是它的替代品。

**采样窗口为什么必须"紧邻"**：演示里第一笔 0.005 是**成功**的。若拿整段演示前后对比，
余额本来就该减少，这条断言会**因为第一笔成功而假失败**。所以只比较被拒那笔的前后。
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any


def snapshot_wallet(client: Any, wallet_uuid: str, pact_id: str, *, chain_id: str, token_id: str) -> dict[str, Any]:
    """采样钱包快照：USDC 余额 + Pact 的交易计数。

    交易数取 `get_pact` 的 `progress_tx_count`（实测该字段存在）——它是**这次授权**下
    成功交易的计数，比钱包总交易数更贴近"这一笔有没有真的发生"。
    """
    balances = client.list_balances(wallet_uuid, chain_id=chain_id, token_id=token_id).get("result") or []
    balance = Decimal("0")
    for row in balances:
        if row.get("chain_id") == chain_id and row.get("token_id") == token_id:
            balance = Decimal(str(row.get("amount") or "0"))
            break
    detail = client.get_pact(pact_id)["result"]
    return {"balance": balance, "tx_count": int(detail.get("progress_tx_count") or 0)}


def audit_corroboration(
    *,
    reason: str,
    audit_logs: list[dict[str, Any]],
    since: str | None,
    action: str,
) -> dict[str, Any]:
    """A5c②：该拒绝能否在 Cobo 审计日志里找到对应的一行。

    三条限定，缺一不可：
    - **按 `action` 过滤**：不过滤会把同一时段别的拒绝记录混进来；
    - **限定在本次运行的时间窗内**：否则上一次运行的记录会被拿来当本次的证据；
    - **必须是 `denied` 且带 `policy_id`**：没有 policy_id 就没法指认是哪条策略拦的。

    **实测事实（2026-10-12）**：x402 付款的拒绝**不进**审计日志（等 2/8/20 秒都没有），
    只有**转账**路径的拒绝才进。所以这条佐证对 x402 路径**取不到**——
    此时必须如实返回 `ok=False`，**不许放宽成"随便找一条 denied 就算数"**。
    """
    from pactpay.evidence import parse_time

    floor = parse_time(since) if since else None
    for row in audit_logs:
        if row.get("action") != action or row.get("result") != "denied":
            continue
        authz = row.get("authz_details") or {}
        if authz.get("reason") != reason:
            continue
        if floor is not None:
            created = parse_time(row.get("created_at"))
            if created is None or created < floor:
                continue
        policy_id = authz.get("policy_id")
        if not policy_id:
            return {
                "ok": False,
                "why": f"找到审计行 {row.get('id')} 但其中没有 policy_id，无法指认是哪条策略拦的",
                "audit_id": row.get("id"),
                "policy_id": None,
            }
        return {
            "ok": True,
            "why": f"审计行 {row.get('id')} 佐证（action={action}，policy_id={policy_id}）",
            "audit_id": row.get("id"),
            "policy_id": policy_id,
        }
    return {
        "ok": False,
        "why": f"审计日志里没有 action={action}、reason={reason} 且在本轮时间窗内的 denied 记录"
               f"（x402 付款路径本来就不写审计行——这是实测事实，不是漏检）",
        "audit_id": None,
        "policy_id": None,
    }


def probe_matched_rule_denial(
    client: Any,
    wallet_uuid: str,
    *,
    pact_key: str,
    chain_id: str,
    token_id: str,
    src_addr: str,
    dst_addr: str,
    amount: str,
) -> dict[str, Any]:
    """打一笔**超过单笔上限**的转账，取回**命中规则**的拒绝。

    **为什么 A5c② 需要这个**：A5c② 要求"该拒绝能在审计日志里找到、且带 `policy_id`"。
    但默认拒绝（fail-closed）那条**结构上就没有 `policy_id`**——本来就没有任何策略被命中；
    而 x402 那笔的拒绝**根本不进审计日志**。两条路都满足不了 ②。
    所以这里专门造一个**命中 `deny_if`** 的拒绝：它有 `policy_id`，也确实会进审计日志。

    超限竟然成功 ⇒ 直接抛错，**不许静默返回 None**——那正是这个演示要证明的反面。
    """
    from pactpay.cobo import PolicyDenied

    try:
        client.transfer(
            wallet_uuid,
            {
                "chain_id": chain_id,
                "token_id": token_id,
                "src_addr": src_addr,
                "dst_addr": dst_addr,
                "amount": amount,
            },
            api_key=pact_key,
        )
    except PolicyDenied as denied:
        details = denied.details or {}
        return {
            "outcome": "denied",
            # ① 的 HTTP 状态取自**实测**，供上层如实展示（不许硬编码 403）
            "http_status": denied.status,
            "code": denied.code,
            "reason": denied.reason,
            "policy_id": details.get("policy_id"),
            "requested_amount": amount,
            "details": details,
        }
    raise RuntimeError(
        f"超限转账 {amount} 竟然成功了 —— 策略引擎没有拦住，本演示要证明的命题不成立"
    )


def outcome_to_a5c_shape(outcome: Any) -> dict[str, Any]:
    """把 `PurchaseOutcome` 摊成 `summarize_a5c` 需要的扁平形状。

    **为什么需要它**：`PurchaseOutcome` 把机器可读的拒绝码放在 `denial` 子字典里，
    而 `summarize_a5c` 要的是顶层的 `code`/`policy_id`。实跑时直接传 `__dict__`
    导致 `code` 取到 **None** —— 第 ① 项显示"✅"却没有任何可核的码（评审实测暴露）。
    转换只做这一件事，并且有专门的测试钉住形状。
    """
    denial = getattr(outcome, "denial", None) or {}
    return {
        "http_status": getattr(outcome, "http_status", None),
        "code": denial.get("code"),
        "policy_id": denial.get("policy_id"),
        "reason": denial.get("reason"),
        "outcome": getattr(outcome, "outcome", None),
    }


# A5c③ 的**测量字段清单**：一个真正的测量结果必然带这些键。
# 用它挡住"调用点把测量结果换成一个硬编码常量"——
# 这是第 5 轮评审 G3 的形态：函数体被测住了，**喂给函数的值**没有。
_MEASURED_KEYS = ("ok", "verdict", "count", "control_count")

# `check_no_onchain_transfer` 可能返回的**全部** verdict。
# 这里必须与那个函数的实际返回**同源** —— 我第一版是手写 ("ok", "inconclusive")，
# 结果真跑起来 `no_trace` 就被自己的守卫拦下了（守卫抓到了真实的不一致，
# 但根因是"清单是手抄的而不是导出的"）。所以定义成常量，两边共用。
VERDICT_NO_TRACE = "no_trace"
VERDICT_TRACE_FOUND = "trace_found"
VERDICT_INCONCLUSIVE = "inconclusive"
_LEGAL_VERDICTS = (VERDICT_NO_TRACE, VERDICT_TRACE_FOUND, VERDICT_INCONCLUSIVE)
# 只有这一个 verdict 代表"证明成立"。
_VERDICT_PROVEN = VERDICT_NO_TRACE


def require_measured_trace(trace: Any, *, label: str) -> dict[str, Any]:
    """**拒绝不是测量结果的东西。**

    第 5 轮评审实测：`x402_trace = {"ok": True, "why": "硬编码"}` 能让 292 条测试全绿，
    而屏幕上照样打 ✅。根子是**判据的函数体被测住了，判据的输入没有**。

    所以把闸门挪到**产出 ✅ 的那一步**：`summarize_a5c` 只接受带齐
    `ok / verdict / count / control_count` 的字典。一个手写的常量给不出这些字段，
    也编不出合法的 `verdict`，于是**当场报错**，而不是安静地打一个 ✅。

    宁可炸，也不给一个来路不明的绿灯。

    **`None` 是例外，而且必须是例外**：它表示"这一侧根本没做测量"，
    语义是"没证明"（→ False），不是"伪造"。伪造是**给了个东西但不是测量结果**，
    那才要炸。两者混为一谈会把"没测"变成崩溃，也会把"伪造"变成安静的 False。
    """
    if trace is None:
        return None
    if not isinstance(trace, dict):
        raise TypeError(f"{label} 的 trace 不是字典（{type(trace).__name__}）⇒ 不是测量结果")
    missing = [k for k in _MEASURED_KEYS if k not in trace]
    if missing:
        raise ValueError(
            f"{label} 的 trace 缺少测量字段 {missing} ⇒ "
            "这不是 `check_no_onchain_transfer` 的返回，不能拿来当证据"
        )
    if trace.get("verdict") not in _LEGAL_VERDICTS:
        raise ValueError(
            f"{label} 的 trace verdict={trace.get('verdict')!r} 不是 "
            f"{_LEGAL_VERDICTS} 之一 ⇒ 不是合法的测量结论"
        )
    # 阳性对照必须留下**数值**：`None` 表示"没数成"，那不是证据。
    if trace.get("verdict") == _VERDICT_PROVEN and not isinstance(trace.get("control_count"), int):
        raise ValueError(
            f"{label} 的 trace 判了 ok，却没有整数 control_count ⇒ "
            "阳性对照没有真的跑过，这个 ok 不可信"
        )
    return trace


def summarize_a5c(
    x402_denied: dict[str, Any] | None,
    matched: dict[str, Any],
    corroboration: dict[str, Any],
    matched_trace: dict[str, Any] | None,
    x402_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A5c「签名前拦截」三重证据的**结构化小结**（纯函数，便于脱网单测）。

    R7 要证的是"拒绝发生在签名之前"，靠三重证据：
      ① 拿到**结构化**拒绝（带机器可读的码）
      ② 该拒绝**能在 Cobo 审计日志里找到**（带 policy_id）
      ③ 链上**没有产生转账**（钱真的没动；判据为链上日志 + 阳性对照，见 `check_no_onchain_transfer`）

    三处曾经不实（B7，第 7 步评审阻断项），这里逐条改成**测量值**：
    - ① 的 HTTP 状态**取自实测返回**，不再在打印里硬编码 `403`；
    - ③ 取自 `check_no_onchain_transfer` 的**链上日志计数 + 阳性对照**（评审第 2 轮 N2
      换掉的：原判据是钱包余额/Pact 计数的快照差，而那个度量**不敏感** ——
      实测 17 次运行里 16 次，**确已结算**的付款同样报"余额未变"。
      旧函数 `check_block_left_no_trace` 已随之删除，不再被任何代码调用），
      也不再是用"存在结构化拒绝"这个派生布尔冒充（那是把结论当证据）；
    - 探针的 `policy_id` 与审计行的 `policy_id` **分成两个键**
      （`probe_policy_id` / `audit_policy_id`），并把"两者相等"算成显式结论
      `policy_id_matches_audit`。原先两者同名，后者覆盖前者，等于把这个交叉核对
      **在产物里销毁了**。

    `x402` 那条的 ② 恒为"取不到"：实测确认 x402 付款的拒绝**不进** Cobo 审计日志
    ——如实标注，不冒充。
    """
    audit_policy_id = corroboration.get("policy_id")
    probe_policy_id = matched.get("policy_id")

    return {
        "x402_denial": {
            # ① 必须是**测出来的**：拿到结构化拒绝的必要条件是 HTTP 状态与机器可读码**都在**。
            # 第 4 轮评审 F2（重大）：这里原先是硬编码 `True`，于是 `demo.py` 会在
            # `① HTTP None 结构化拒绝 ✅（码 None）` 的情况下渲染出一个**凭空通过的 A5c①**
            # —— 正是 B7 那一类"拿结论冒充证据"。改为派生自实际拿到的字段，缺一即 False。
            "structured_denial": bool(
                x402_denied
                and x402_denied.get("http_status") is not None
                and x402_denied.get("code")
            ),
            "http_status": x402_denied.get("http_status") if x402_denied else None,
            "code": x402_denied.get("code") if x402_denied else None,
            "audit_corroboration": False,
            "why_audit_missing": "x402 付款的拒绝不进 Cobo 审计日志（实测事实）",
            # ③ 对 x402 这条同样是**测量值**（链上窗口 + 阳性对照），不是硬编码的 ✅。
            # `require_measured_trace` 挡住"调用点换成常量"（G3）：缺测量字段就当场炸，
            # 而不是安静地打一个来路不明的 ✅。
            "no_chain_trace": bool(
                (require_measured_trace(x402_trace, label="x402 被拒那笔的 A5c③") or {}).get("ok")
            ),
            "no_chain_trace_why": (x402_trace or {}).get("why"),
        },
        "matched_rule_denial": {
            "http_status": matched.get("http_status"),
            "code": matched.get("code"),
            "reason": matched.get("reason"),
            "requested_amount": matched.get("requested_amount"),
            # 两个来源分开存，**绝不同名**（同名会让后者覆盖前者）
            "probe_policy_id": probe_policy_id,
            # **必须取审计那一侧的值。** 这里原先是 `probe_policy_id`（评审第 2 轮 M11）——
            # 两个键共用同一个变量，于是下面的 `policy_id_matches_audit` 变成
            # `probe == probe` 恒真，演示页上"探针 X vs 审计 X → ✅ 一致"两边是同一个 X。
            # 交叉核对的全部意义就是拿**两个来源**对撞；同一个来源对撞等于没核。
            "audit_policy_id": audit_policy_id,
            # 把交叉核对算成显式结论，而不是留给人在两份 JSON 之间肉眼比对
            "policy_id_matches_audit": bool(
                probe_policy_id and audit_policy_id and probe_policy_id == audit_policy_id
            ),
            "audit_corroboration": bool(corroboration.get("ok")),
            "audit_id": corroboration.get("audit_id"),
            # ③ 必须是**链上日志的测量值**；没采样到就是 False（未知 ≠ 通过）。
            # 同样过 `require_measured_trace`（G3）。
            "no_chain_trace": bool(
                (require_measured_trace(matched_trace, label="命中规则那笔的 A5c③") or {}).get("ok")
            ),
            "no_chain_trace_why": (matched_trace or {}).get("why"),
        },
    }


def wait_for_audit_corroboration(
    client: Any,
    wallet_id: str,
    *,
    reason: str,
    action: str,
    since: str | None,
    timeout_s: float = 15.0,
    interval_s: float = 1.0,
    limit: int = 50,
) -> dict[str, Any]:
    """轮询审计日志，直到找到那一行或超时。

    **为什么必须轮询**（实测踩到）：转账拒绝的审计行**比操作本身晚一点**才写进去。
    查询抢在写入之前，就会把"审计里确实有这一行"误判成"没有"——
    那是个会误导人的**假失败**，会让人以为策略拒绝没被审计。

    超时后**如实报"没等到"**，绝不无限等下去，也绝不编一条出来。
    """
    deadline = time.monotonic() + timeout_s
    result: dict[str, Any] = {}
    while True:
        response = client.list_audit_logs(wallet_id=wallet_id, limit=limit)
        rows = ((response or {}).get("result") or {}).get("items") or []
        result = audit_corroboration(reason=reason, audit_logs=rows, since=since, action=action)
        if result["ok"] or time.monotonic() >= deadline:
            break
        time.sleep(interval_s)
    if not result.get("ok"):
        result["why"] = f"等了 {timeout_s:.0f} 秒仍未出现（{result.get('why')}）"
    return result


def _safe_count(count_fn: Any, subject: str, window: tuple[int, int]) -> dict[str, Any]:
    """把 `count_fn` 调成**永远不抛、且 count 一定是非负整数**的形状。

    这是我自己对 N2 的修复做对抗测试时抓出来的三个洞（评审还没提，先自己补）：

    1. `count_fn` **抛异常**（RPC 抖动、DNS 挂）——原来会直接冒泡炸掉整个 demo，
       而不是报"判不了"。证据链的可信度要求：**查询失败 ≠ 没有转账**。
    2. `count_fn` 返回 `None` —— 原来 `None.get("ok")` 直接 AttributeError。
    3. **最危险的一个**：`{"ok": True, "count": None}` 原来经 `or 0` 变成 `0`，
       于是走进"0 笔 ⇒ 无痕迹"分支，**打出一个假的 ✅**。
       这正是我上一轮被抓的那类错误（把"未知"当成"通过"）的翻版。

    现在：拿不到**非负整数**计数，一律判 `inconclusive`（ok=False）。
    """
    try:
        result = count_fn(subject, window[0], window[1])
    except Exception as error:  # noqa: BLE001 - 任何异常都只表示"测不了"
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    if not isinstance(result, dict) or not result.get("ok"):
        err = (result or {}).get("error") if isinstance(result, dict) else "count_fn 返回了非字典"
        return {"ok": False, "error": err or "查询未成功"}

    count = result.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return {"ok": False, "error": f"计数不是非负整数：{count!r}"}

    return {"ok": True, "count": count}


def count_transfers_in_window(subject: str, from_block: int, to_block: int) -> dict[str, Any]:
    """给 `check_no_onchain_transfer` 用的**具名**查询口子。

    原先这里是调用点里的一个匿名 lambda：
    `lambda subject, a, b: count_usdc_transfers_from(subject, from_block=a, to_block=b)`。
    评审把 `to_block=b` 改成 `to_block=a`（把查询区间压成一个点）**无人变红**（G5）——
    匿名 lambda 没法被单独测，也没有名字可以被指认。

    具名之后它是一条能被直接测的边界：见
    `test_the_window_adapter_actually_queries_the_whole_window`。
    """
    from pactpay.onchain import count_usdc_transfers_from

    return count_usdc_transfers_from(subject, from_block=from_block, to_block=to_block)


def build_measurement_window(
    *, block_before: int | None, block_after: int | None, control_block: int | None
) -> tuple[tuple[int, int], tuple[int, int], str]:
    """从**采样到的区块高度**构造被测窗口与阳性对照窗口，并校验它们的形状。

    **为什么把构造搬进来**（第 5 轮评审 G5，重大）：

    原先窗口是在**调用点**拼好再传进来的：
    `window = (block_before, block_after)`，然后断言 `window[0] > control_window[1]`。
    问题是"窗口有多宽"这件事**完全由调用点说了算** —— 评审把这些变异注进去，
    292 条测试全绿、屏幕照样打 ✅：

    - `window = (block_after, block_after)` —— 窗口整体挪到**尝试之后**，等于没测
    - `block_after = block_before` —— 窗口宽度 0
    - `if window[0] <= control_window[1]` 改成 `> block_after + 100` —— 兜底断言被废掉
    - `and False` —— 同上

    根子还是那一句：**判据的函数体被测住了，判据的输入没有。**
    所以现在**窗口由判据自己造**，调用点只能交出三个"我在哪采的样"的**事实**
    （`block_before` 在发起被拒采购**之前**采、`block_after` 在**之后**采、
    `control_block` 是那笔**已结算**付款所在的区块），造不出一个"看起来对"的窗口。

    三条形状校验，任一不成立就返回空窗口（调用方会判 `inconclusive`，**不给 ✅**）：

    1. 三个高度都得拿得到（拿不到就是"判不了"）。
    2. `block_before < block_after` —— 采样必须真的跨过了那次尝试。
       这一条直接干掉"窗口宽度 0"和"窗口挪到尝试之后"。
    3. `control_block < block_before` —— 阳性对照那个区块必须在被测窗口**之前**。
       否则那笔**已结算**的转账会落进被测窗口，于是拿自己那笔成功的付款
       去指控这笔被拒的付款"留下了痕迹"。
    """
    if block_before is None or block_after is None or control_block is None:
        return (0, -1), (0, -1), "拿不到区块高度（采样失败）⇒ 无法构造窗口"
    if block_before >= block_after:
        return (0, -1), (0, -1), (
            f"采样区间不成立：[{block_before}, {block_after}] —— "
            "`block_before` 必须**严格早于** `block_after`（采样要真的跨过那次被拒的尝试）"
        )
    if control_block >= block_before:
        return (0, -1), (0, -1), (
            f"阳性对照区块 {control_block} 没有早于被测窗口下界 {block_before} ⇒ "
            "本运行自己那笔已结算的转账会落进被测窗口，"
            "于是会拿它去指控这笔被拒的付款留下了痕迹"
        )
    return (block_before, block_after), (control_block, control_block), ""


def check_no_onchain_transfer(
    *,
    subject: str,
    block_before: int | None,
    block_after: int | None,
    control_block: int | None,
    count_fn: Any,
) -> dict[str, Any]:
    """判定"这笔尝试**没有**产生链上 USDC 转账"—— A5c③ 的正确形态。

    **为什么重写**（评审第 2 轮 N2，阻断项）：

    旧判据是"钱包余额与 Pact 交易计数在紧邻窗口内没变"。但那个度量**不敏感**：
    Cobo 的余额接口滞后，实测 17 次真实运行里 **16 次**，**确已链上结算**的 paid 那笔
    同样报"余额未变"；`progress_tx_count` 更是 **17/17 全为 `0→0`**。
    换言之，**真正动过钱的付款也会显示"无变化"** —— 那个 ✅ 是恒真的，
    它证明不了"钱没动"，只是证明了"这个仪表盘看不见钱动"。

    **正确形态 = 一个敏感度量 + 一次阳性对照**：

    1. **阳性对照先行**：同一个查询、同一个地址，在**已知发生过转账**的窗口
       （paid 那笔的结算区块附近）上**必须数到 ≥ 1 笔**。
       对照失败 ⇒ 度量是瞎的 ⇒ 返回 `ok=False` 且 `verdict="inconclusive"`。
    2. 对照通过后，才用"窗口内计数为 0"去证明被拒那笔**确实没有链上痕迹**。

    **绝不在对照失败时给 ✅** —— 宁可报"判不了"，也不给一个恒真的绿灯。

    **窗口由本函数自己构造**（G5）：调用点交出的只是三个采样到的高度，
    见 `build_measurement_window`。这样"窗口有多宽""对照在哪"就不能被调用点悄悄改掉。
    """
    window, control_window, window_error = build_measurement_window(
        block_before=block_before, block_after=block_after, control_block=control_block
    )
    if window_error:
        return {
            "ok": False,
            "verdict": VERDICT_INCONCLUSIVE,
            "why": window_error,
            "count": None,
            "control_count": None,
            "window": list(window),
            "control_window": list(control_window),
        }
    control = _safe_count(count_fn, subject, control_window)
    if not control.get("ok"):
        return {
            "ok": False,
            "verdict": VERDICT_INCONCLUSIVE,
            "why": f"阳性对照查询失败（{control.get('error')}）⇒ 无法判定，不给通过",
            "count": None,
            "control_count": None,
            "window": list(window),
            "control_window": list(control_window),
        }
    if not control.get("count"):
        return {
            "ok": False,
            "verdict": VERDICT_INCONCLUSIVE,
            "why": (
                f"阳性对照没数到任何转账（窗口 {control_window} 内 0 笔）——"
                f"而这个窗口里**确实有一笔已结算的付款**。"
                "说明这个度量看不见转账 —— 它的「没有」不能用来证明「没发生」。"
            ),
            "count": None,
            "control_count": 0,
            "window": list(window),
            "control_window": list(control_window),
        }

    actual = _safe_count(count_fn, subject, window)
    if not actual.get("ok"):
        return {
            "ok": False,
            "verdict": VERDICT_INCONCLUSIVE,
            "why": f"目标窗口查询失败（{actual.get('error')}）⇒ 无法判定，不给通过",
            "window": list(window),
            "control_window": list(control_window),
            "count": None,
            "control_count": control.get("count"),
        }

    count = actual["count"]
    if count != 0:
        return {
            "ok": False,
            "verdict": VERDICT_TRACE_FOUND,
            "why": f"目标窗口 {window} 内数到 {count} 笔 USDC 转账 ⇒ 钱真的动了，不是签名前拦住",
            "count": count,
            "control_count": control.get("count"),
            "window": list(window),
            "control_window": list(control_window),
        }

    return {
        "ok": True,
        "verdict": VERDICT_NO_TRACE,
        "why": (
            f"链上无痕迹：目标窗口 {window} 内 0 笔 USDC 转账"
            f"（阳性对照在同一查询下数到 {control.get('count')} 笔，证明度量有效）"
        ),
        "count": 0,
        "control_count": control.get("count"),
        "window": list(window),
        "control_window": list(control_window),
    }


def judge_paid_requires_onchain(
    outcome: Any,
    verify_fn: Any,
    *,
    expect_from: str,
    expect_to: str,
    expect_value: int,
) -> dict[str, Any]:
    """A4 守卫的**可测形态**：凡记成 `paid` 的，必须能通过链上复核。

    **为什么把它抽出来**（评审第 2 轮 N3/M4，阻断项）：

    这条守卫原先**内联**在 `demo._run` 里，写成
    `if outcome.outcome == "paid" and outcome.settlement_tx:`。那是**逃逸口**——
    "记成 paid 却没有结算哈希"时整段被静默跳过，而这正是要防的静默降级。
    改成 `if outcome.outcome == "paid":` + 显式取哈希后，**它的正确性仍然零测试**：
    把守卫改回逃逸写法，187 个用例**全绿**（评审实测 M4）。

    根因是它没法离线测——它内联在一个要联网、要花钱的函数里。
    抽成纯函数并注入 `verify_fn` 后，逃逸写法与正确写法的差别就能被单测钉死：
    **判据是"凡是 paid 都必须验链上"，而不是"有哈希才验"。**
    """
    if getattr(outcome, "outcome", None) != "paid":
        return {"checked": False, "ok": True, "block_number": None, "why": "非 paid 结果，不做链上复核"}

    if not getattr(outcome, "settlement_tx", None):
        return {
            "checked": True,
            "ok": False,
            "block_number": None,
            "why": f"A4 失败：{outcome.resource} 记成了 paid 却没有结算哈希 —— 不能当作已付款",
        }

    result = verify_fn(
        outcome.settlement_tx,
        expect_from=expect_from,
        expect_to=expect_to,
        expect_value=expect_value,
    )
    if not result.get("ok"):
        return {
            "checked": True,
            "ok": False,
            "block_number": result.get("block_number"),
            "why": f"A4 失败：链上复核没过 —— {'；'.join(result.get('errors') or [])}",
            "transfers": result.get("transfers"),
        }

    return {
        "checked": True,
        "ok": True,
        "block_number": result.get("block_number"),
        "why": "链上复核通过",
        "transfers": result.get("transfers"),
    }
