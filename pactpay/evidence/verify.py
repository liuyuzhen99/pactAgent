"""运行级证据校验（A7a / A7b）：`python -m pactpay.evidence.verify <run 目录>`。

校验五件事，缺一不可：

1. **链是否完好**（`chain_ok`）——改过 / 删过 / 换过顺序都会被查出来。
2. **锚是否对得上**（`anchor_ok`）——总条数 / **分 kind 条数** / 尾哈希。
   没有它的话，链尾那条 `default_deny`（R6 的唯一证据）删掉都没人发现：
   因为 `count_records(records, "purchase")` **只数购买记录**（评审 G2 实测）。
3. **计数是否对得上**（`counts_ok`）——链里数出来的条数 vs 运行时另写一份的
   `counts.json`。**两个独立来源对上才算数**；只从链里数一遍是自己数自己，证明不了什么。
4. **幂等是否正确**（`idempotency_ok`，A7b）——`request_id` 不得重复，
   且不得出现 `idempotent == true` 的记录。否则"net重放"会被当成一笔新交易记账。
5. **链上锚点**（`onchain_ok`）——每条 `paid` 记录都必须在 Base Sepolia 上找到
   对应的 USDC 转账；并且**链上这个窗口里的转账笔数必须等于证据里的 paid 条数**。

**这套校验能证明什么、不能证明什么**（别夸大 —— 第 5 轮评审 G1 就是在打这一条）：

- **能证明**：
  - 本地这一份证据**没有被改动过、也没有被删掉一段**，*除非*改动者
    把整条链连同 `chain_anchor.json` **一起重算**。
  - **哪怕他一起重算了**，`paid` 记录也跑不掉：链上那笔 USDC 转账不在我们硬盘上。
    "把成功那笔擦成被拒"会让**链上的转账笔数**与证据里的 paid 条数对不上账。
    伪造一条 `paid` 则需要伪造一笔链上交易 —— 做不到。
- **不能证明**：
  - **denied 记录**没有链上痕迹可锚，所以它们只能靠"链 + 锚"这一层
    （即：能被重算）。被拒的部分依靠 Cobo 审计行（由 Cobo 签发）做外部佐证。
  - 文件内容与 Cobo 那边一致 —— 那靠审计行本身是 Cobo 签发的。

所以：**"不可篡改"这四个字只对 `paid` 记录成立**（靠链上交易）；
其余部分准确的说法是"**篡改可检测，除非连锚一起重算**"。宁可把话说小，不说大。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pactpay.evidence import (
    ANCHOR_FILENAME,
    OUTCOMES,
    ChainCorruptError,
    count_records,
    read_chain,
    verify_anchor,
    verify_chain,
)


def _unreadable(run_dir: Path, why: str) -> dict[str, Any]:
    """证据读不动时的统一结构化结论：**一律 ok=False**。

    绝不把"读不出来"当成"没问题"。这条规则在本项目已经被违反三次
    （`tx_hash` 空断言、余额快照恒真、`count=None` 被 `or 0`），所以收敛成一个函数。
    """
    return {
        "run_dir": str(run_dir),
        "records": 0,
        "chain_ok": False,
        "chain_why": why,
        "anchor_ok": False,
        "anchor_why": why,
        "onchain_ok": False,
        "onchain_why": why,
        "counts": None,
        "recorded_counts": None,
        "counts_ok": False,
        "idempotency_ok": False,
        "problems": [why],
        "ok": False,
    }


# 上游复核最多翻几页、每页多大。滚动日志里旧 run 的行会被挤出第一页，
# 所以**必须翻页**（第 6 轮 W5）。
UPSTREAM_PAGE_SIZE = 100
UPSTREAM_MAX_PAGES = 60

# 上游复核**钉住的字段**。原来只钉 `id`+`action`（第 6 轮 W3）：
# 于是把某行的 `result` 从 `denied` 改成 `allowed` 照样通过，
# 而看板会按 SOURCE_LABELS 把它渲染成"Cobo 审计日志（不可篡改）"。
UPSTREAM_PINNED_FIELDS = ("action", "result", "created_at", "resource_id")


def verify_audit_rows_upstream(
    audit_rows: list[dict[str, Any]], *, wallet_id: str
) -> tuple[bool, str]:
    """把证据里的审计行**回 Cobo 现场核对**：这些行还在不在、`action` 对不对得上。

    **为什么这是必要的**（第 5 轮评审 G2 的残留洞）：

    整个证据包（链、锚、counts.json、run.json）都是本地文件。谁能写它们，
    谁就能把"删掉一条记录"做得天衣无缝 —— 本轮实测：删掉 R6 的 `default_deny`
    记录 + 重算全链哈希 + 同步锚的 total/kinds/head_hash → `✓ 证据校验通过`，exit 0。

    链上锚点解决不了这个：**默认拒绝根本没有链上交易**。

    但审计行不一样 —— 它们是 **Cobo 服务端**写的事件日志，本地只能"抄一份"，
    **改写不了服务端**。所以这里重新拉一次审计日志：证据里引用的每条 id
    都必须还在，且 `action` 与证据里记的一致。伪造/删除审计行会在这里露馅。

    取不到（网络、权限、超出保留窗口）时**判失败**，不判通过：宁可不判，也不放行。
    """
    if not audit_rows:
        return False, "证据里没有审计行快照 ⇒ 无法做上游复核（宁可不判）"

    wanted = {str(row.get("id")): row for row in audit_rows}
    if not wanted:
        return False, "审计行快照里没有任何 id ⇒ 无法做上游复核（宁可不判）"

    # **必须翻页取全**（第 6 轮 W5，重大）：原来硬编码 `limit=100`，
    # 而审计日志是**滚动**的——旧 run 的行会被新行挤出第一页。
    # 实测：27 个真实 run 里 **25 个**因此被误判成"审计行被改过或删过"，
    # 1250 行诚实证据被指控成伪造。**一个会诬告好证据的检查，比没有检查更糟。**
    try:
        from pactpay.platform.config import load_existing
        from pactpay.platform.cobo import CoboClient

        api_key = load_existing().get("AGENT_WALLET_API_KEY", "")
        if not api_key:
            return False, "拿不到凭据 ⇒ 无法做上游复核（宁可不判）"

        live_by_id: dict[str, dict[str, Any]] = {}
        pages_fetched = 0
        exhausted = False   # 上游翻到底了（再没有下一页）
        all_found = False   # 待核对的行已经全部拿到（不必再翻）
        with CoboClient(api_key=api_key) as client:
            cursor: str | None = None
            # 最多翻 MAX_PAGES 页；翻到"要的行全找到"或"上游到底了"就停。
            for _ in range(UPSTREAM_MAX_PAGES):
                params: dict[str, Any] = {"limit": UPSTREAM_PAGE_SIZE}
                if wallet_id:
                    params["wallet_id"] = wallet_id
                if cursor:
                    params["after"] = cursor
                page = client.list_audit_logs(**params)
                pages_fetched += 1
                result = (page or {}).get("result") or {}
                items = result.get("items") or []
                for item in items:
                    live_by_id[str(item.get("id"))] = item
                cursor = result.get("next_cursor")
                if not cursor or not items:
                    exhausted = True
                    break
                if wanted.keys() <= live_by_id.keys():
                    all_found = True
                    break  # 要的行都找到了，不必再翻
    except Exception as error:  # noqa: BLE001 - 任何失败都收敛成"判不了"
        return False, f"上游复核失败（{type(error).__name__}: {error}）⇒ 宁可不判"

    # **三态，不是两态**（W5 的核心修复）：
    #   - 找到且一致        → 通过
    #   - 找到但字段不一致  → **确认被篡改**（这是唯一能下"伪造"结论的情形）
    #   - 翻遍了也没找到    → **判不了**（可能是超出上游保留窗口），
    #                         **绝不能**说成"被改过或删过"
    # 只有在**两种情形之一**成立时才能下结论：
    #   - 待核对的行全部拿到了（`all_found`）→ 可以逐字段比
    #   - 翻到了上游的尽头（`exhausted`）→ "没找到"可以解释为超出保留窗口
    # 否则（撞到翻页上限）就是"判不了"。
    # 注意：`break` 出来时 `exhausted` 仍是 False，所以**不能**只看 `exhausted`
    # —— 我第一版就是这么写反的，结果"找全了"反被判成"没覆盖"。
    if not (all_found or exhausted):
        return False, (
            f"翻了 {pages_fetched} 页仍未覆盖到全部待核对的审计行 ⇒ 无法做上游复核（宁可不判）"
        )

    mismatched: list[str] = []
    for rid, row in wanted.items():
        found = live_by_id.get(rid)
        if found is None:
            continue  # 归入"找不到"，下面统一处理
        for field in UPSTREAM_PINNED_FIELDS:
            if row.get(field) != found.get(field):
                mismatched.append(f"{rid}.{field}: 证据={row.get(field)!r} 上游={found.get(field)!r}")
    if mismatched:
        return False, (
            f"上游审计日志里**确认不一致**（共 {len(mismatched)} 处）：{mismatched[:5]} "
            "—— 审计行在上游不是这个样子"
        )

    not_found = sorted(rid for rid in wanted if rid not in live_by_id)
    if not_found:
        return True, (
            f"ok（{len(wanted) - len(not_found)}/{len(wanted)} 条在上游逐字段对上；"
            f"另有 {len(not_found)} 条翻遍 {pages_fetched} 页也没找到——"
            "**判不了**，可能是超出上游保留窗口，不据此判伪造）"
        )
    return True, f"ok（{len(wanted)} 条审计行在上游逐字段对上，翻了 {pages_fetched} 页）"


def verify_run(run_dir: Path, *, offline: bool = False) -> dict[str, Any]:
    """校验一个 run 目录，返回结构化结论（不抛异常，便于程序判断）。

    **"不抛异常"是契约，不是愿望**（评审第 3 轮 #6）：链文件被截断时原来会抛
    `JSONDecodeError`，CLI 打 traceback、看板 500 —— 一个"防篡改"的记录遇到损坏
    反而最脆。现在所有读取失败都收敛成 `ok=False` 的结构化结论。
    """
    run_dir = Path(run_dir)

    chain_path = run_dir / "chain.jsonl"
    try:
        records = read_chain(chain_path)
    except ChainCorruptError as corrupt:
        return _unreadable(run_dir, f"链文件损坏，无法解析：{corrupt}")
    except OSError as error:
        return _unreadable(run_dir, f"链文件读不出来：{type(error).__name__}: {error}")

    # run.json：存着这次运行的**声明**（计划、审计行快照、A5c 结论）。
    # 注意：它也是本地文件，攻击者能改 —— 所以它是**核对对象**，不是信任来源。
    # 真正的外部来源是 Cobo 服务端（下面的上游复核）。
    run_meta: dict[str, Any] = {}
    meta_path = run_dir / "run.json"
    try:
        if meta_path.exists():
            run_meta = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        run_meta = {}

    chain_ok, chain_why = verify_chain(records)

    # G2：链的锚（总条数 / 分 kind 条数 / 尾哈希）。
    # 只靠 `count_records(records, "purchase")` 的话，链尾那条 `default_deny`
    # ——R6 默认拒绝的唯一证据——删掉也没人发现（评审实测）。
    anchor_path = run_dir / ANCHOR_FILENAME
    try:
        anchor = json.loads(anchor_path.read_text()) if anchor_path.exists() else None
    except (json.JSONDecodeError, OSError) as error:
        return _unreadable(run_dir, f"锚文件读不出来：{type(error).__name__}: {error}")
    anchor_ok, anchor_why = verify_anchor(records, anchor)

    # **两边必须补零到同一组键再比**（本仓库自己踩到的真 bug）：
    # 运行时计数器（`count_outcomes`）总是写全 `paid/denied/failed` 三个键、
    # 缺席的记 0；而从链里数出来的（`count_records`）**只含出现过的 outcome**。
    # 直接 `==` 会让"只成功、没被拒"这类完全正常的运行被判成"计数对不上"。
    # demo 那次侥幸通过，只是因为它恰好同时有 paid 和 denied —— 掩盖了这个 bug。
    # G1：外部锚点（每条 paid 记录对一条真实链上交易）。
    # 这是整个证据集里**唯一不依赖本地文件**的判据 —— 也是"不可篡改"真正站得住的那一半。
    if offline:
        onchain_ok, onchain_problems = True, []
        onchain_why = "已跳过（--offline）：本地链无法证明自己没有连同哈希一起被重算"
    else:
        onchain_ok, onchain_problems = verify_onchain_anchors(records)

        # 窗口与地址来自锚 —— **它们是本地文件，可以被一起改**。
        # 这一点在 README 里如实写明：本校验器防的是"误改/漂移"，
        # 并用**链上事实**给 paid 记录做交叉核对；它**不是**防篡改。
        addr = (anchor or {}).get("agent_address")
        lo, hi = (anchor or {}).get("from_block"), (anchor or {}).get("to_block")

        if addr and lo is not None and hi is not None:
            recon_ok, recon_problems = reconcile_with_chain(
                records, agent_address=addr, from_block=int(lo), to_block=int(hi)
            )
            onchain_ok = onchain_ok and recon_ok
            onchain_problems.extend(recon_problems)
        else:
            onchain_ok = False
            onchain_problems.append(
                "锚里没有 agent_address / 区块窗口 ⇒ 无法与链上对账（宁可不判）"
            )

    # **上游复核**（G2 残留洞的第二道）：审计行来自 Cobo，是可以现场重查的**外部事实**。
    # 证据里存了 50 条审计行，这里逐条回 Cobo 核对 id 与 action 是否还在。
    # 本地文件可以被一起重写，但**重写不了 Cobo 服务端的日志**。
    if not offline:
        upstream_ok, upstream_why = verify_audit_rows_upstream(
            run_meta.get("audit_logs") or [], wallet_id=run_meta.get("wallet_uuid") or ""
        )
        if not upstream_ok:
            onchain_ok = False
            onchain_problems.append(upstream_why)
    # 无论走哪条分支都要有结论 —— 否则 `UnboundLocalError` 会让"校验"本身崩掉，
    # 而一个会崩的校验器比没有校验器更糟（它看起来在工作）。本仓库刚踩过这个坑。
    onchain_why = (
        "已跳过（--offline）：本地链无法证明自己没有连同哈希一起被重算"
        if offline
        else ("ok" if onchain_ok else "；".join(onchain_problems))
    )

    chain_counts_raw = count_records(records, "purchase")
    counts_path = run_dir / "counts.json"
    try:
        recorded_counts = json.loads(counts_path.read_text()) if counts_path.exists() else None
    except (json.JSONDecodeError, OSError) as error:
        return _unreadable(run_dir, f"计数器文件读不出来：{type(error).__name__}: {error}")

    def _normalize(counts: dict[str, int] | None) -> dict[str, int] | None:
        if counts is None:
            return None
        return {name: int(counts.get(name, 0)) for name in OUTCOMES}

    chain_counts = _normalize(chain_counts_raw)
    counts_ok = recorded_counts is not None and _normalize(recorded_counts) == chain_counts

    # A7b：幂等断言
    request_ids = [
        (r.get("payload") or {}).get("request_id")
        for r in records
        if (r.get("payload") or {}).get("request_id")
    ]
    duplicates = len(request_ids) != len(set(request_ids))
    replays = [r for r in records if (r.get("payload") or {}).get("idempotent") is True]
    idempotency_ok = not duplicates and not replays

    problems: list[str] = []
    if not chain_ok:
        problems.append(f"链不完整：{chain_why}")
    if not anchor_ok:
        problems.append(f"锚对不上：{anchor_why}")
    problems.extend(onchain_problems)
    if not counts_ok:
        problems.append(f"计数对不上：运行时计数器={recorded_counts}，链里数出来={chain_counts}")
    if duplicates:
        problems.append("request_id 有重复 ⇒ 幂等重放被当成了新交易")
    if replays:
        problems.append(f"有 {len(replays)} 条记录标记为 idempotent=true（重放，不是新交易）")

    return {
        "run_dir": str(run_dir),
        "records": len(records),
        "chain_ok": chain_ok,
        "chain_why": chain_why,
        "anchor_ok": anchor_ok,
        "anchor_why": anchor_why,
        "onchain_ok": onchain_ok,
        "onchain_why": onchain_why,
        "counts": chain_counts,
        "recorded_counts": recorded_counts,
        "counts_ok": counts_ok,
        "idempotency_ok": idempotency_ok,
        "problems": problems,
        "ok": chain_ok and anchor_ok and counts_ok and idempotency_ok and onchain_ok,
    }


def main(argv: list[str] | None = None) -> int:
    args = [a for a in (sys.argv[1:] if argv is None else argv) if a != "--offline"]
    offline = "--offline" in (sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法：python -m pactpay.evidence.verify [--offline] <run 目录>", file=sys.stderr)
        return 2

    result = verify_run(Path(args[0]), offline=offline)
    counts = result["counts"]
    print(f"chain_ok={str(result['chain_ok']).lower()}  ({result['chain_why']})")
    print(
        f"counts_ok={str(result['counts_ok']).lower()}  "
        f"paid={counts.get('paid', 0)} denied={counts.get('denied', 0)} "
        f"failed={counts.get('failed', 0)} pending={counts.get('pending', 0)}"
    )
    # `failed` 必须**显式**出现在输出里（评审 N4）：它意味着"钱可能动了、需人工核对"，
    # 只打印 allowed/denied/pending 会让一次只含 failed 的运行看起来"什么都没发生"。
    if counts.get("failed"):
        print(f"  ⚠️  有 {counts['failed']} 笔未结算（不是策略拒绝，钱可能已经动了，请人工核对）")
    print(f"anchor_ok={str(result['anchor_ok']).lower()}  ({result['anchor_why']})")
    # 链上锚点是**唯一不依赖本地文件**的判据；跳过时必须**明说跳过**，
    # 不能让人把"没验"读成"验过了"。
    print(f"onchain_ok={str(result['onchain_ok']).lower()}  ({result['onchain_why']})")
    print(f"idempotency_ok={str(result['idempotency_ok']).lower()}")
    for problem in result["problems"]:
        print(f"  ✗ {problem}")
    print("✓ 证据校验通过" if result["ok"] else "✗ 证据校验未通过")
    if result["ok"]:
        # **把话说准**（评审 G1）：这句必须说清证明了什么、没证明什么。
        # ≥ 的边界就是"能重算整条链的人也能重算锚"——那种篡改只有链上锚点挡得住，
        # 而链上锚点只覆盖 paid 记录（denied 记录本来就没有链上痕迹可锚）。
        print(
            "  说明：本地链 + 锚只能证明"
            "「这份文件没有被改动过，除非改动者把整条链与锚一起重算」。"
            "paid 记录另有一条**不依赖本地文件**的锚——链上交易（任何人可用 Base Sepolia 独立复算）。"
        )
    return 0 if result["ok"] else 1



# ---------------------------------------------------------------------------
# G1（第 5 轮评审，阻断）：**外部锚点**
#
# 评审实测：把 paid 记录改成 denied、然后重算整条链的 seq/prev_hash/hash，
# 再同步 counts.json —— `make verify` 照样打 ✓ 且 exit 0。
#
# 这不是"链写错了"，这是**我们说过头了**：一条没有外部锚点的哈希链，
# 只能证明"这份文件没有被改动过，**除非**改的人把哈希一起重算"。
# 能写这个文件的人就能重算。所以"不可篡改"这四个字，
# 靠链本身**永远**撑不起来。
#
# 真正撑得起来的是这一条：**paid 记录的 `settlement_tx` 是一条真实链上交易。**
# 它不在我们的硬盘上，在 Base Sepolia 上，任何人都能独立复算。
# 于是"把 paid 改成 denied"这种擦除，会留下一条**对不上账**的链上交易；
# 而"伪造一条 paid"需要伪造一条链上交易——做不到。
#
# 所以 `verify` 多了一道：**每条 paid 记录都必须能在链上找到对应的 USDC 转账**
# （从我们的地址、转给收款方、金额相符）。这是整个证据集里唯一一个
# **不依赖本地文件**的判据。
# ---------------------------------------------------------------------------

def onchain_anchor_reason(record: dict[str, Any], *, verify_fn: Any = None) -> tuple[bool, str]:
    """一条 paid 记录能否在链上找到对应交易。

    返回 `(ok, why)`。`ok=False` 表示**这条记录与链上事实不符**，
    是最严重的一类失败：它意味着证据被擦改过。
    `verify_fn` 注入口子（测试用）；默认走真实 RPC。
    """
    payload = record.get("payload") or {}
    if payload.get("outcome") != "paid":
        return True, "非 paid，无需链上锚定"
    tx = payload.get("settlement_tx")
    if not tx:
        return False, "paid 记录没有 settlement_tx ⇒ 无法锚定到链上（这一条可能被擦改过）"
    if verify_fn is None:
        from pactpay.platform.onchain import verify_usdc_transfer  # 延迟导入：离线跑 verify 时不拖 RPC 依赖

        verify_fn = verify_usdc_transfer
    try:
        amount = payload.get("amount_atomic")
        result = verify_fn(
            tx,
            expect_from=payload.get("agent_address") or "",
            expect_to=payload.get("pay_to") or "",
            expect_value=int(amount) if amount is not None else 0,
        )
    except Exception as exc:  # noqa: BLE001 —— 网络错误是"没验成"，不是"验过了"
        return False, f"链上复核没能完成（{type(exc).__name__}: {exc}）⇒ 宁可不判"
    ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
    why = result.get("why", "") if isinstance(result, dict) else ""
    return ok, why or ("链上交易与记录相符" if ok else "链上交易与记录不符")


def verify_onchain_anchors(
    records: list[dict[str, Any]], *, verify_fn: Any = None
) -> tuple[bool, list[str]]:
    """把链里每条 paid 记录都拿到链上核对一遍。

    离线时**不要**调用这个（它要打 RPC）；`verify` 用 `--offline` 跳过并**如实说明跳过**。
    """
    problems: list[str] = []
    paid = [r for r in records if (r.get("payload") or {}).get("outcome") == "paid"]
    for record in paid:
        ok, why = onchain_anchor_reason(record, verify_fn=verify_fn)
        if not ok:
            tx = (record.get("payload") or {}).get("settlement_tx") or "（无）"
            problems.append(f"链上锚点对不上：{tx} —— {why}")
    return not problems, problems


def reconcile_with_chain(
    records: list[dict[str, Any]],
    *,
    agent_address: str,
    from_block: int,
    to_block: int,
    count_fn: Any = None,
) -> tuple[bool, list[str]]:
    """**把证据与链上事实对账**（第 5 轮评审 G1 的正面回答）。

    上一版只做了"每条 paid 记录都去链上找一找"。评审立刻指出它的漏洞：
    把 paid 记录**擦成 denied 并重算整条链**之后，链里**一条 paid 都不剩**，
    于是"每条 paid 都核对"这句话**空洞地成立** —— ✓ + exit 0，成功故事被擦掉。

    根子在于：**只在证据内部找一致性，永远发现不了"证据少了一条"。**

    真正的对账必须问一个**证据之外**的问题：
    **"这段时间里，链上到底发生过几笔从我们地址发出的 USDC 转账？"**
    链上那笔转账不会因为我们改了一个 JSON 就消失。

    于是：链上数到 N 笔 ⇒ 证据里就必须有 N 条 paid 记录（且哈希逐一对上）。
    擦掉一条 paid，链上那一笔就成了**对不上的账**。

    `count_fn` 是注入口子（测试用），签名同 `onchain.count_usdc_transfers_from`。
    """
    if count_fn is None:
        from pactpay.platform.onchain import count_usdc_transfers_from

        count_fn = count_usdc_transfers_from

    onchain_txs = {
        str((r.get("payload") or {}).get("settlement_tx")).lower()
        for r in records
        if (r.get("payload") or {}).get("outcome") == "paid"
        and (r.get("payload") or {}).get("settlement_tx")
    }

    try:
        result = count_fn(agent_address, from_block=from_block, to_block=to_block)
    except Exception as exc:  # noqa: BLE001 —— 数不出来就是"没验成"
        return False, [f"链上对账没能完成（{type(exc).__name__}: {exc}）⇒ 宁可不判"]

    if not isinstance(result, dict) or not result.get("ok"):
        why = (result or {}).get("error") if isinstance(result, dict) else "返回形状不对"
        return False, [f"链上对账没能完成（{why}）⇒ 宁可不判"]

    # **重复引用同一笔交易要单独抓**（第 6 轮 W2，重大）：
    # 原来比的是 `len(set(tx_hashes))` —— **集合把重复塌掉了**，于是一条真实交易
    # 被两条 paid 记录引用时，"链上 1 笔 vs 证据 2 条"照样通过，
    # 证据就成了"成功 2 笔 / 0.01 USDC"，而链上只有 1 笔 / 0.005。
    paid_records = [
        r for r in records if (r.get("payload") or {}).get("outcome") == "paid"
    ]
    problems: list[str] = []
    if len(onchain_txs) != len(paid_records):
        problems.append(
            f"paid 记录里有重复引用的交易：{len(paid_records)} 条记录却只指向 "
            f"{len(onchain_txs)} 笔不同的链上交易 —— 同一笔被记了多次"
        )

    observed = int(result.get("count") or 0)
    if observed != len(paid_records):
        problems.append(
            f"链上对账对不上：Base Sepolia 上这个区间有 {observed} 笔从该地址发出的 USDC 转账，"
            f"而证据里有 {len(paid_records)} 条 paid 记录"
            + ("（证据里少了记录 —— 成功的那笔可能被擦掉了）" if observed > len(paid_records) else "")
        )
    return not problems, problems






if __name__ == "__main__":
    raise SystemExit(main())
