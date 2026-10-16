"""证据落盘与校验（T7）。

需求 R8：每一次策略决策（允许/拒绝/待审批）都要被记录、可导出。
需求 R9：Dashboard 要能把整条证据链展示出来。

这里**不发明新格式**——证据就是运行过程中真实发生的事实：
- 每笔采购的结果（成功则含链上交易哈希，被拒则含结构化拒绝原因与命中策略）
- 从 Cobo `GET /api/v1/audit-logs` 拉回的审计行（`result ∈ allowed/denied/pending/error`）

**为什么不自己造一份"审计日志"**：自己写的日志证明不了任何东西——它是自证的。
只有 Cobo 那边返回的审计行才是"不可篡改"的那一份，所以直接引用它。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pactpay.agent.pay import PurchaseOutcome

EVIDENCE_DIR = Path("evidence")


#: 一次采购可能得到的三种结果。**必须三种都算** —— 只数 paid/denied 会把
#: `failed`（策略放行了但没结算成功）静默丢掉，看板上就永远看不到基础设施故障，
#: 反而会把故障读成"边界生效"。
OUTCOMES = ("paid", "denied", "failed")


def count_outcomes(purchases: list[dict[str, Any]]) -> dict[str, int]:
    """按结果计数。**唯一实现**——demo 的看板、页面的汇总、证据校验都用它，
    避免三处各写一遍然后慢慢漂移（本函数替代了原来散在三处的 sum(...)）。"""
    counts = {name: 0 for name in OUTCOMES}
    for p in purchases:
        outcome = p.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
    return counts


def purchase_as_evidence(
    outcome: PurchaseOutcome,
    *,
    agent_address: str = "",
    pay_to: str = "",
    amount_atomic: int | None = None,
) -> dict[str, Any]:
    """把一次采购落成证据记录。

    `agent_address` / `pay_to` / `amount_atomic` 是**链上锚点**要用的三样：
    没有它们，`verify` 就没法把一条 `paid` 记录钉到一笔真实链上交易上，
    于是"把 paid 改成 denied 再重算整条链"这种擦改就无法被发现（第 5 轮评审 G1）。
    """
    return {
        # 链上锚点三件套：from / to / 金额（原子单位）。
        # 记的是**运行时的事实**，不是事后能凭空补的东西。
        "agent_address": agent_address,
        "pay_to": pay_to,
        "amount_atomic": str(amount_atomic) if amount_atomic is not None else None,
        "resource": outcome.resource,
        # Decimal 不能直接进 JSON，统一成字符串，避免浮点误差悄悄改掉金额
        "amount_usd": str(outcome.amount_usd),
        "description": outcome.description,
        "outcome": outcome.outcome,
        "http_status": outcome.http_status,
        "data": outcome.data,
        "settlement_tx": outcome.settlement_tx,
        "denial": outcome.denial,
        # A7b：幂等键与重放标记要进证据，`verify` 才能识破"重放被当成新交易"
        "request_id": outcome.request_id,
        "idempotent": outcome.idempotent,
    }


def parse_time(value: Any) -> datetime | None:
    """把时间戳解析成可比较的 datetime。

    两种格式都要吃：Cobo 回的是 `…Z` 结尾，而 Python `isoformat()` 出的是 `+00:00`。
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def match_audit_denial(
    denial: dict[str, Any],
    audit_logs: list[dict[str, Any]],
    *,
    since: str | None = None,
) -> dict[str, Any] | None:
    """在 Cobo 审计日志里找与这条拒绝对应的那一行；**找不到就返回 None**。

    找不到是正常结果，**不许凑**：实测（2026-10-12）x402 付款路径的拒绝
    根本不写审计行（等 2/8/20 秒都没有），只有普通转账路径的拒绝才有。
    如果这里在找不到时回退到"最近一条 denied 行"，Dashboard 就会声称这条拒绝
    "在不可篡改审计里"——而那是假的。宁可为空。

    `since` 是本次运行的开始时间。**必须有时间窗**：实测踩到过——审计日志里躺着一条
    14 分钟前、来自**另一次转账操作**的 `transfer.denied`，reason 与本次 x402 拒绝完全相同；
    只按 reason 匹配就会把它认成"本次拒绝的审计行"，于是页面又把假话说了一遍。
    """
    # **x402（payment）路径的拒绝不许去审计里找行** —— 实测它根本不写审计行。
    # 光靠时间窗不够：本次运行里的 A5c② 探针**自己**会造出一条
    # `transfer.denied`、reason 与 x402 拒绝**完全相同**的行，而且时间**晚于**该拒绝。
    # 于是页面会拿一条"探针的、事后才产生的"审计行去给 x402 拒绝作证
    # —— 实测抓到过（audit id 927625 被安到 `/api/premium` 卡片上）。
    # 判据必须看**这条拒绝走的是哪个 API**，而不是看 reason 字符串像不像。
    if denial.get("api") == "payment":
        return None
    wanted = denial.get("policy_reason") or denial.get("reason")
    if not wanted:
        return None
    floor = parse_time(since) if since else None
    for row in audit_logs:
        if not isinstance(row, dict) or row.get("result") != "denied":
            continue
        # 结构性核对：只有 `transfer.*` 这类动作才可能是转账路径拒绝的审计行。
        action = row.get("action") or ""
        if not action.startswith("transfer"):
            continue
        authz = row.get("authz_details") or {}
        if authz.get("reason") != wanted:
            continue
        # 金额也要对上：同一次运行里可能有**多笔**同 reason 的拒绝（探针 vs 真转账），
        # 金额是区分它们的独立佐证。缺失时不强求（老记录可能没有这个字段）。
        wanted_amount = (denial.get("details") or {}).get("amount") or denial.get("amount")
        row_amount = (row.get("request") or {}).get("amount")
        if wanted_amount is not None and row_amount is not None and str(wanted_amount) != str(row_amount):
            continue
        if floor is not None:
            created = parse_time(row.get("created_at"))
            # 时间读不出来时**按不匹配处理**：宁可说"审计里没有"，
            # 也不要拿一条时间不明的行去冒充本次拒绝的证据。
            if created is None or created < floor:
                continue
        return row
    return None


def denial_evidence_source(
    denial: dict[str, Any], audit_logs: list[dict[str, Any]], *, since: str | None = None
) -> str:
    """这条拒绝的证据来自哪里——**如实标注**，因为两条路径的来源真的不同。

    - `cobo_audit_log`：Cobo 把它记进了不可篡改审计日志（转账路径）。
    - `cobo_payment_response`：Cobo 只在付款响应里回了它，**审计日志里没有**
      （x402 路径）。它是 Cobo 签发的、机器可读的，但性质是"响应"而非"追加式日志"。
    """
    matched = match_audit_denial(denial, audit_logs, since=since)
    return "cobo_audit_log" if matched else "cobo_payment_response"


def within_run(row: dict[str, Any], since: str | None) -> bool:
    """这一条审计行是否落在本次运行的时间窗内。

    审计接口是按 wallet 返回**历史**的，所以不带时间窗就会把之前若干次运行的记录
    一起列出来——页面看起来像"成功了 11 笔"，而本次实际只有 1 笔。
    时间读不出来时按**不在**窗内处理（宁可少列，也不把来历不明的行算进来）。
    """
    if not since:
        return True
    floor = parse_time(since)
    if floor is None:
        return True
    created = parse_time(row.get("created_at"))
    return created is not None and created >= floor


# ---------------------------------------------------------------------------
# A7a / A7b：哈希链
#
# 为什么要有链：R8 说这些记录"不可篡改"。存一份普通 JSON 不叫不可篡改——
# 改一个数字谁也看不出来。每条记录把**上一条的哈希**算进自己的哈希里，
# 于是任何一处改动都会让它之后的所有记录对不上，链就断了。
#
# 注意边界：这证明的是"**这份文件自分发以来没被改过**"，它**不能**证明
# "文件里的内容与 Cobo 那边一致"——后者靠审计行本身（Cobo 签发）。
# 两件事都要有，缺一不可。
# ---------------------------------------------------------------------------

GENESIS_HASH = "0" * 64


def canonical_bytes(obj: Any) -> bytes:
    """确定性序列化：同一份内容永远得到同样的字节。

    `sort_keys=True` 是关键——否则字典顺序一变，哈希就变了，链会假报警。
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_record_hash(record: dict[str, Any]) -> str:
    """一条记录的哈希 = sha256(记录内容去掉 hash 字段 + 它记录的 prev_hash)。

    payload 必须被算进去，否则"改内容不改哈希"。
    """
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def append_record(path: Path, payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """向链尾追加一条记录（append-only），返回写好的记录。

    只做追加、从不重写已有行——这是"不可篡改"的一半；另一半由 `verify_chain` 提供。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_chain(path)
    prev = existing[-1]["hash"] if existing else GENESIS_HASH
    record: dict[str, Any] = {
        "seq": len(existing),
        "kind": kind,
        "prev_hash": prev,
        "payload": payload,
    }
    record["hash"] = compute_record_hash(record)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


class ChainCorruptError(Exception):
    """链文件读不动（某一行不是合法 JSON）。

    **为什么要有这个专门的异常**（评审第 3 轮 #6）：哈希链是**只追加**的，
    最可能的意外损坏就是"最后一行写了一半"（进程被杀、磁盘满）。
    原来 `json.loads` 裸奔，于是 `verify_run` **抛 JSONDecodeError**，
    直接违反它自己 docstring 写的"返回结构化结论（**不抛异常**，便于程序判断）"，
    CLI 打一坨 traceback、看板 500 —— **一个"防篡改"的记录，遇到损坏时
    反而比谁都脆**。

    现在：`read_chain` 抛这个**可识别**的异常，调用方各自给出结构化的处置。
    注意它**不是**"跳过坏行继续"—— 截断行必须被**报告**，不许被无声吃掉。
    """

    def __init__(self, path: Path, line_no: int, raw: str, cause: Exception) -> None:
        super().__init__(
            f"{path} 第 {line_no} 行不是合法 JSON（{type(cause).__name__}: {cause}）；"
            f"该行前 80 字符：{raw[:80]!r}"
        )
        self.path = path
        self.line_no = line_no
        self.raw = raw


def read_chain(path: Path) -> list[dict[str, Any]]:
    """读回整条链。文件不存在时返回空列表（还没写过任何记录）。

    某一行读不出来时抛 `ChainCorruptError`（带行号），**不吞、也不跳过**。
    """
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as cause:
            raise ChainCorruptError(path, line_no, line, cause) from cause
    return records


def verify_chain(records: list[dict[str, Any]]) -> tuple[bool, str]:
    """校验整条链。返回 `(是否完好, 说明)`。

    三重检查：序号连续、`prev_hash` 接得上、记录自身哈希与内容相符。
    三者缺一，删一条、改一条、换顺序都会被查出来。
    """
    prev = GENESIS_HASH
    for index, record in enumerate(records):
        if record.get("seq") != index:
            return False, f"第 {index} 条的 seq 是 {record.get('seq')!r}，应为 {index}"
        if record.get("prev_hash") != prev:
            return False, f"第 {index} 条的 prev_hash 接不上上一条（链断了）"
        actual = compute_record_hash(record)
        if actual != record.get("hash"):
            return False, f"第 {index} 条自身哈希不符（内容被改过）"
        prev = record["hash"]
    return True, f"ok（{len(records)} 条）"


def count_records(records: list[dict[str, Any]], kind: str) -> dict[str, int]:
    """按 payload.outcome 统计某一类记录的条数。

    这个计数是**从链里算出来的**；它的独立来源是运行时另写一份的 `counts.json`。
    两者对上才算数——自己数自己不算证据。
    """
    counts: dict[str, int] = {}
    for record in records:
        if record.get("kind") != kind:
            continue
        outcome = (record.get("payload") or {}).get("outcome")
        if outcome:
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# run 目录：一次运行 = 一个目录，里面是**只追加**的证据
#
#   evidence/run-<时间戳>-<run_id 前 8 位>/
#     chain.jsonl   哈希链：每条记录都在运行时**当场追加**（不是跑完一次性写）
#     counts.json   运行时**自己**边跑边加的计数器，与链是两个独立来源
#     run.json      运行元信息（任务/预算/Pact/审计行快照…）
#
# 为什么拆三个文件：校验要拿**两个独立来源**对账。如果计数也从链里算，
# 那就是自己数自己，证明不了什么。
# ---------------------------------------------------------------------------


def run_dir_for(run_id: str, started_at: str, base: Path = EVIDENCE_DIR) -> Path:
    """本次运行的证据目录。名字带时间戳与 run_id，所以每次运行各自独立。"""
    stamp = parse_time(started_at) or datetime.now(timezone.utc)
    name = f"run-{stamp.strftime('%Y%m%dT%H%M%SZ')}-{run_id[:8]}"
    path = Path(base) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_run_meta(run_dir: Path, meta: dict[str, Any]) -> Path:
    path = Path(run_dir) / "run.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return path


def write_counts(run_dir: Path, counts: dict[str, int]) -> Path:
    """写运行时计数器。**这份必须由运行时自己维护**，不能从链里反推。"""
    path = Path(run_dir) / "counts.json"
    path.write_text(json.dumps(counts, ensure_ascii=False, indent=2))
    return path


def write_anchor(
    run_dir: Path,
    records: list[dict[str, Any]],
    *,
    agent_address: str = "",
    from_block: int | None = None,
    to_block: int | None = None,
) -> Path:
    """写链的锚（总条数 / **分 kind 条数** / 尾哈希）。

    **必须由链本身算出来**，所以它在语义上不是独立来源——它的作用是让
    "删掉链尾一条记录"这种篡改变成可检测（见 `verify_anchor` 的边界说明）。
    """
    path = Path(run_dir) / ANCHOR_FILENAME
    path.write_text(
        json.dumps(
            compute_anchor(
                records,
                agent_address=agent_address,
                from_block=from_block,
                to_block=to_block,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return path


def load_run(run_dir: Path) -> dict[str, Any]:
    """把一个 run 目录读回成 Dashboard/校验用的字典（形状与旧版单文件一致）。"""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").exists() else {}
    records = read_chain(run_dir / "chain.jsonl")
    meta["purchases"] = [r["payload"] for r in records if r.get("kind") == "purchase"]
    default_denies = [r["payload"] for r in records if r.get("kind") == "default_deny"]
    meta["default_deny"] = default_denies[-1] if default_denies else None
    meta["chain_records"] = len(records)
    return meta


# ---------------------------------------------------------------------------
# G2/G1（第 5 轮评审）：链的**锚**与"到底证明了什么"
#
# 评审在 a181a59 上实测了两件事：
#   (a) 删掉链尾那条 `default_deny` 记录（R6 的唯一证据）→ 仍然 ✓ + exit 0，
#       因为 `count_records(records, "purchase")` **只数 purchase**；
#   (b) 把 paid 记录改成 denied、然后**重算全部 seq/prev_hash/hash** →
#       仍然 ✓ + exit 0。
#
# (a) 是纯粹的漏洞，用锚可以堵死。(b) 不是漏洞，是**我们说过头了**：
# 一条**无外部锚点**的哈希链，只能证明"这份文件自生成以来没有被改动过
# **除非有人把整条链连同哈希一起重算**"。能写这个文件的人就能重算。
# 所以下面做两件事：
#   1. 写一份 `chain_anchor.json`：总条数、**每个 kind 的条数**、尾哈希。
#      它把"删一条"变成可检测的（G2）。
#   2. 把**外部锚点**接进来：paid 记录的 `settlement_tx` 是一条**真实链上交易**，
#      任何人都能去 Base Sepolia 复算。伪造一条 paid 记录需要伪造一条链上交易，
#      而那是做不到的（G1）。这才是"不可篡改"真正站得住的那一半。
#   3. 措辞收窄到上面这两条真正成立的范围。
# ---------------------------------------------------------------------------

ANCHOR_FILENAME = "chain_anchor.json"


def compute_anchor(
    records: list[dict[str, Any]],
    *,
    agent_address: str = "",
    from_block: int | None = None,
    to_block: int | None = None,
) -> dict[str, Any]:
    """算出链的锚：条数、**分 kind 的条数**、尾哈希。

    `kinds` 是关键：`count_records(records, "purchase")` 只数购买记录，
    于是链尾那条 `default_deny`（R6 默认拒绝的唯一证据）删掉也没人发现。
    分 kind 计数让它可检测，`total` 再用一个独立的量交叉核对。
    """
    kinds: dict[str, int] = {}
    for record in records:
        kind = record.get("kind") or "unknown"
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "total": len(records),
        "kinds": kinds,
        "head_hash": records[-1]["hash"] if records else GENESIS_HASH,
        # **链上对账的外部坐标**（G1）：没有它，"证据里没有 paid"就无法与
        # "链上其实发生过转账"对质，于是把 paid 擦成 denied 就能瞒过去。
        "agent_address": agent_address,
        "from_block": from_block,
        "to_block": to_block,
    }




def verify_anchor(records: list[dict[str, Any]], anchor: dict[str, Any] | None) -> tuple[bool, str]:
    """链与锚对不上就报错。

    锚本身也在同一个目录里、也可以被一起改掉——这不是缺陷，是**边界**：
    它挡的是"改了链但没同步改锚"（以及"删了尾记录"）。能重写整条链的人也能重写锚，
    那种篡改要靠**外部锚点**（paid 记录的链上交易，见 `onchain_anchor_reason`）。
    """
    if anchor is None:
        return False, "缺少 chain_anchor.json ⇒ 无法判断链是否被删改过（宁可不判）"
    fresh = compute_anchor(records)
    if anchor.get("total") != fresh["total"]:
        return False, f"总条数对不上：锚={anchor.get('total')}，链里={fresh['total']}"
    if (anchor.get("kinds") or {}) != fresh["kinds"]:
        return False, f"分 kind 条数对不上：锚={anchor.get('kinds')}，链里={fresh['kinds']}"
    if anchor.get("head_hash") != fresh["head_hash"]:
        return False, (
            f"尾哈希对不上：锚={str(anchor.get('head_hash'))[:16]}…，"
            f"链里={fresh['head_hash'][:16]}… ⇒ 链尾被删或被改"
        )
    return True, f"ok（{fresh['total']} 条，kinds={fresh['kinds']}）"
