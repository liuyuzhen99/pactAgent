"""A7a 的运行级校验：`python -m pactpay.evidence.verify <run 目录>`。

除了链本身，还要对两件事：
1. **计数与运行时独立落盘的计数器一致**——链里数出来的 vs 运行时自己加的，
   两个来源对上才算数（自己数自己不算证据）。
2. **A7b 幂等**：`request_id` 不得重复，且所有 `idempotent` 必须为 false
   ——否则"幂等重放"会被当成一笔新交易记进证据，账目就错了。
"""

from __future__ import annotations

import json
from pathlib import Path

from pactpay.evidence import append_record, read_chain, write_anchor
from pactpay.evidence.verify import verify_run


def _make_run(tmp_path: Path, *, counts: dict | None = None, request_ids=("r1", "r2")):
    run = tmp_path / "run-20261012T000000Z-abcdef01"
    run.mkdir()
    chain = run / "chain.jsonl"
    append_record(chain, {"outcome": "paid", "request_id": request_ids[0], "idempotent": False}, kind="purchase")
    append_record(chain, {"outcome": "denied", "request_id": request_ids[1], "idempotent": False}, kind="purchase")
    # R6 的默认拒绝记录：**每次运行都强制执行**，所以一次健康的运行必然有它。
    # 少了它，`required_records_reason` 会判"被删过"（G2 残留洞的契约）。
    append_record(chain, {"outcome": "denied", "reason": "no_pact_transfer_allow_policy_matched"},
                  kind="default_deny")
    (run / "counts.json").write_text(
        json.dumps(counts if counts is not None else {"paid": 1, "denied": 1})
    )
    (run / "run.json").write_text(json.dumps({"run_id": "abcdef01", "started_at": "2026-10-12T00:00:00Z"}))
    # 锚（G2）：不写它，`verify_run` 会**如实**判"无法判断链是否被删改过"。
    write_anchor(run, read_chain(chain))
    return run


def test_verify_run_passes_on_a_healthy_run(tmp_path):
    result = verify_run(_make_run(tmp_path), offline=True)

    assert result["chain_ok"] is True
    assert result["counts_ok"] is True
    assert result["idempotency_ok"] is True
    assert result["ok"] is True
    # 计数**补零到 `OUTCOMES` 全部三个键**（缺席记 0）—— 运行时计数器就是这么写的，
    # 两边不比同一组键会把"只成功没被拒"这种正常运行误判成损坏。
    assert result["counts"] == {"paid": 1, "denied": 1, "failed": 0}


def test_verify_run_fails_when_the_chain_was_tampered_with(tmp_path):
    """篡改负例：这是 A7a 明确要求的，否则 chain_ok=true 可能是硬编码的。"""
    run = _make_run(tmp_path)
    lines = (run / "chain.jsonl").read_text().splitlines()
    record = json.loads(lines[1])
    record["payload"]["outcome"] = "paid"  # 把 denied 悄悄改成 paid
    lines[1] = json.dumps(record, ensure_ascii=False, sort_keys=True)
    (run / "chain.jsonl").write_text("\n".join(lines) + "\n")

    result = verify_run(run, offline=True)

    assert result["chain_ok"] is False
    assert result["ok"] is False


def test_verify_run_fails_when_counts_disagree_with_the_chain(tmp_path):
    """运行时计数器与链里数出来的不一致 ⇒ 有一方在说谎，必须报出来。"""
    run = _make_run(tmp_path, counts={"paid": 2, "denied": 0})

    result = verify_run(run, offline=True)

    assert result["counts_ok"] is False
    assert result["ok"] is False


def test_verify_run_fails_on_duplicate_request_ids(tmp_path):
    """A7b：同一个 `request_id` 出现两次 ⇒ 幂等重放被当成了新交易。"""
    run = _make_run(tmp_path, request_ids=("same", "same"))

    result = verify_run(run, offline=True)

    assert result["idempotency_ok"] is False
    assert result["ok"] is False


def test_verify_run_fails_when_a_record_was_an_idempotent_replay(tmp_path):
    """A7b：任何一条 `idempotent == true` 都不算新交易，必须报出来。"""
    run = tmp_path / "run-20261012T000000Z-abcdef01"
    run.mkdir()
    append_record(run / "chain.jsonl", {"outcome": "paid", "request_id": "r1", "idempotent": True}, kind="purchase")
    (run / "counts.json").write_text(json.dumps({"paid": 1}))
    (run / "run.json").write_text(json.dumps({"run_id": "abcdef01"}))

    result = verify_run(run, offline=True)

    assert result["idempotency_ok"] is False


# ---------------------------------------------------------------------------
# 损坏容忍（评审第 3 轮 #5/#6）
#
# 哈希链是**只追加**的，最可能的意外损坏就是"最后一行写了一半"
# （进程被杀、磁盘满）。一个"防篡改"的记录遇到损坏时不该比谁都脆。
# ---------------------------------------------------------------------------

def _a_healthy_run(tmp_path):
    """一个"除了链上锚点之外一切正常"的 run。

    故意**不含真实链上交易**：这里的 paid 是合成的，所以 CLI 必须用 `--offline`
    才可能通过（真实锚点由 `test_onchain_anchor_*` 用注入口子单独覆盖）。
    """
    from pactpay.evidence import append_record, count_outcomes, read_chain, write_anchor, write_counts

    run = tmp_path / "run-20260101T000000Z-deadbeef"
    run.mkdir()
    append_record(run / "chain.jsonl", {"outcome": "paid", "request_id": "r1"}, kind="purchase")
    # R6 的默认拒绝记录：每次运行都强制执行，健康的运行必然有它
    append_record(run / "chain.jsonl",
                  {"outcome": "denied", "reason": "no_pact_transfer_allow_policy_matched"},
                  kind="default_deny")
    write_counts(run, count_outcomes([{"outcome": "paid"}]))
    write_anchor(run, read_chain(run / "chain.jsonl"))
    (run / "run.json").write_text('{"started_at": "2026-01-01T00:00:00Z"}', encoding="utf-8")
    return run


def test_a_truncated_chain_line_is_reported_not_raised(tmp_path):
    """**截断的链行必须变成结构化结论，不许抛异常**（评审 #6）。

    `verify_run` 的 docstring 写着"不抛异常，便于程序判断"，但原来
    `json.loads` 裸奔，一行截断就抛 `JSONDecodeError`：CLI 打 traceback、看板 500。
    契约是契约，不是愿望。
    """
    from pactpay.evidence.verify import verify_run

    run = _a_healthy_run(tmp_path)
    with (run / "chain.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 1, "kind": "purchase", "payload": {"outc')   # 写一半

    result = verify_run(run, offline=True)          # 不许抛

    assert result["ok"] is False
    assert result["chain_ok"] is False
    assert "损坏" in result["chain_why"] or "无法解析" in result["chain_why"]


def test_a_truncated_run_json_is_not_treated_as_a_complete_run(tmp_path):
    """**半截的 `run.json` 不算"跑完整了"**（评审 #5）。

    `write_run_meta` 用的是非原子 `write_text`。原来 `is_complete_run` 只判
    `is_file()`，于是半截文件被选中：`make verify` 打「✓ 通过」而看板 500 ——
    一边说没事一边炸了。
    """
    from pactpay.dashboard.app import is_complete_run, latest_run_dir

    run = _a_healthy_run(tmp_path)
    assert is_complete_run(run) is True

    (run / "run.json").write_text('{"started_at": "2026-01-01T00:0', encoding="utf-8")  # 截断

    assert is_complete_run(run) is False, "半截的 run.json 被当成了完整运行"
    assert latest_run_dir(tmp_path) is None, "半截的运行被 latest_run_dir 选中了"


def test_the_dashboard_shows_a_broken_page_instead_of_500(tmp_path):
    """**看板不许 500**：读不动就如实说"读不动"（评审 #5）。

    500 页面既没说清发生了什么，也让人以为服务挂了。而真正的事实是
    "这次运行的证据不完整" —— 它**既不等于边界生效，也不等于边界失效**。
    """
    from fastapi.testclient import TestClient

    from pactpay.dashboard.app import create_app

    run = _a_healthy_run(tmp_path)
    (run / "chain.jsonl").write_text('{"seq": 0, "kind": "purchase", "payl', encoding="utf-8")

    client = TestClient(create_app(evidence_dir=tmp_path), raise_server_exceptions=False)

    page = client.get("/")
    api = client.get("/api/evidence")

    assert page.status_code == 200, "首页 500 了 —— 应该渲染「读不动」页"
    assert "证据读不动" in page.text
    assert "不代表" in page.text, "读不动页必须说清它既不代表生效也不代表失效"
    assert api.status_code == 422, f"端点应回 422（可诊断），实际 {api.status_code}"
    assert "读不动" in api.json()["detail"]


# ---------------------------------------------------------------------------
# F4（第 4 轮评审）：`make verify` 的**结论**必须被钉住
#
# `make verify` 是这个项目的**审计者唯一的工具**。它的 `ok` 聚合、它打印的那行
# ✓/✗、它的退出码 —— 三样都没有任何测试。评审把 `ok` 里的 `chain_ok` 删掉，
# 一条被篡改的链于是打印：
#     chain_ok=false  (第 0 条自身哈希不符（内容被改过）)
#       ✗ 链不完整：...
#     ✓ 证据校验通过                       <- 自相矛盾，且 exit 0
# 而 254 条测试全绿。**一个会在篡改面前说"通过"的校验器，比没有校验器更糟。**
# ---------------------------------------------------------------------------

def _tamper_but_keep_counts_consistent(run):
    """把链里一条记录的 `payload` 改掉（**不**重算哈希），并让 counts.json 跟着对。

    这样 `chain_ok=False`（自身哈希不符）而 `counts_ok=True`
    —— 正好把"`ok` 是否真的把 `chain_ok` 算进去"逼出来。
    """
    import json

    chain_path = run / "chain.jsonl"
    records = [json.loads(line) for line in chain_path.read_text().splitlines() if line.strip()]
    tampered = 0
    for rec in records:
        payload = rec.get("payload") or {}
        if rec.get("kind") == "purchase" and payload.get("outcome") == "paid":
            payload["outcome"] = "denied"        # 篡改内容，不重算 record_hash
            tampered += 1
    assert tampered, "没有找到可篡改的记录，测试前提不成立"
    chain_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n")

    # **把 counts.json 也改成与"篡改后的链"一致**，这样 `counts_ok=True` 而
    # `chain_ok=False` —— 否则 counts_ok 自己就是 False，`ok` 里有没有 `chain_ok`
    # 根本区分不出来（第一版就是这样，评审的 N12 变异照样逃逸）。
    counts_path = run / "counts.json"
    if counts_path.exists():
        counts = json.loads(counts_path.read_text())
        counts["paid"], counts["denied"] = counts.get("denied", 0), counts.get("paid", 0)
        counts_path.write_text(json.dumps(counts))
    return records


def test_a_tampered_chain_can_never_yield_ok(tmp_path):
    """`chain_ok=False` ⇒ `ok=False`，**哪怕计数对得上**。"""
    from pactpay.evidence.verify import verify_run

    run = _a_healthy_run(tmp_path)
    _tamper_but_keep_counts_consistent(run)

    result = verify_run(run, offline=True)

    assert result["chain_ok"] is False, "篡改没被链校验发现，这条测试的前提不成立"
    assert result["ok"] is False, (
        "链已被篡改，verify 却给出 ok=True —— 审计工具在说谎（F4 复发）"
    )


def test_verify_cli_says_fail_and_exits_nonzero_on_a_tampered_chain(tmp_path, capsys):
    """CLI 必须打 ✗ 且**退出码非零**；不许出现"链不完整"和"✓ 通过"同时打印。"""
    from pactpay.evidence.verify import main

    run = _a_healthy_run(tmp_path)
    _tamper_but_keep_counts_consistent(run)

    code = main(["--offline", str(run)])
    out = capsys.readouterr().out

    assert code != 0, f"链被篡改，退出码却是 {code}（应为非零）"
    assert "✗ 证据校验未通过" in out
    assert "✓ 证据校验通过" not in out, "篡改后仍打印了 ✓ —— 自相矛盾的输出"
    assert "链不完整" in out


def test_verify_cli_says_pass_and_exits_zero_on_a_healthy_run(tmp_path, capsys):
    """**阳性对照**：健康运行必须打 ✓ 且 exit 0。

    没有这一条，上面那条可能只是"永远返回非零"。
    """
    from pactpay.evidence.verify import main

    run = _a_healthy_run(tmp_path)

    code = main(["--offline", str(run)])
    out = capsys.readouterr().out

    assert code == 0, f"健康运行的退出码是 {code}，输出：{out}"
    assert "✓ 证据校验通过" in out
    assert "✗ 证据校验未通过" not in out


def test_verify_cli_reports_failed_prominently(tmp_path, capsys):
    """`failed` 必须在结论里**显式**出现 —— 它意味着"钱可能动了、需人工核对"。"""
    import json

    from pactpay.evidence import append_record, count_outcomes, write_counts
    from pactpay.evidence.verify import main

    run = tmp_path / "run-20260101T000000Z-feedface"
    run.mkdir()
    append_record(run / "chain.jsonl", {"outcome": "failed", "request_id": "r1"}, kind="purchase")
    write_counts(run, count_outcomes([{"outcome": "failed"}]))
    (run / "run.json").write_text(json.dumps({"started_at": "x"}), encoding="utf-8")

    main(["--offline", str(run)])
    out = capsys.readouterr().out

    assert "failed=1" in out
    assert "钱可能已经动了" in out, "未结算的记录没有被显式提示人工核对"


def test_starting_denial_is_false_when_the_code_or_status_is_missing():
    """**A5c① 的成立条件必须是"状态码与机器可读码都在"**（第 4 轮评审 F2）。

    原先这里是硬编码的 `True`：只要调用方传进来一个"非 None 的字典"（哪怕里面
    什么都没有），A5c① 就渲染成 ✅。评审把它改回 `True`，254 条测试全绿。
    """
    from pactpay.block_evidence import summarize_a5c

    matched = {"http_status": 403, "code": "TRANSFER_LIMIT_EXCEEDED", "policy_id": "p-1"}
    corroboration = {"ok": True, "audit_id": 1, "policy_id": "p-1", "why": "已找到"}
    # 真实的测量形状（带 verdict/count/control_count）——
    # 手写的半截字典会被 `require_measured_trace` 当成伪造而报错（G3）
    trace = {"ok": True, "verdict": "no_trace", "why": "窗口内 0 笔（阳性对照 1 笔）",
             "count": 0, "control_count": 1}
    MATCHED, CORROBORATION, TRACE = matched, corroboration, trace

    good = summarize_a5c(
        {"http_status": 422, "code": "X402_POLICY_DENIED"}, MATCHED, CORROBORATION, TRACE
    )
    no_code = summarize_a5c({"http_status": 422, "code": None}, MATCHED, CORROBORATION, TRACE)
    no_status = summarize_a5c({"http_status": None, "code": "X402_POLICY_DENIED"}, MATCHED, CORROBORATION, TRACE)
    empty = summarize_a5c({}, MATCHED, CORROBORATION, TRACE)
    missing = summarize_a5c(None, MATCHED, CORROBORATION, TRACE)

    assert good["x402_denial"]["structured_denial"] is True
    for label, summary in (
        ("有字典但没有 code", no_code),
        ("有字典但没有 http_status", no_status),
        ("空字典", empty),
        ("完全没测到", missing),
    ):
        assert summary["x402_denial"]["structured_denial"] is False, (
            f"{label}，A5c① 却记成了成立 —— 拿「存在一个字典」冒充「拿到了结构化拒绝」"
        )


# ---------------------------------------------------------------------------
# 第 5 轮评审的两个阻断项（G1 / G2）
#
# G2：`count_records(records, "purchase")` **只数购买记录**，于是链尾那条
#     `default_deny`（R6 默认拒绝的唯一证据）删掉也没人发现 —— ✓ + exit 0。
# G1：把 paid 改成 denied、**重算整条链的哈希**、同步 counts.json ⇒ 照样 ✓ + exit 0。
#     这不是链写错了，是**我们说过头了**：无外部锚点的哈希链只能防"改了不重算"。
#     真正撑得住的是**链上锚点**：paid 记录的 `settlement_tx` 是一条真实链上交易。
# ---------------------------------------------------------------------------

def _run_with_default_deny(tmp_path):
    """一条**以 `default_deny` 结尾**的链 —— 与真实 demo 产出的形状一致。"""
    from pactpay.evidence import append_record, read_chain, write_anchor, write_counts

    run = tmp_path / "run-20261012T000000Z-abcdef01"
    run.mkdir()
    chain = run / "chain.jsonl"
    append_record(chain, {"outcome": "paid", "request_id": "r1",
                          "settlement_tx": "0xabc", "agent_address": "0xA", "pay_to": "0xB",
                          "amount_atomic": "5000"}, kind="purchase")
    append_record(chain, {"outcome": "denied", "request_id": "r2"}, kind="purchase")
    # 链尾这条**不属于 purchase**，正是 G2 里被静默删掉的那条
    append_record(chain, {"outcome": "denied", "code": "ADDRESS_NOT_WHITELISTED"}, kind="default_deny")
    write_counts(run, {"paid": 1, "denied": 1, "failed": 0})
    write_anchor(run, read_chain(chain), agent_address="0xA", from_block=1, to_block=9)
    (run / "run.json").write_text('{"started_at": "2026-10-12T00:00:00Z"}', encoding="utf-8")
    return run


def _fake_verify_ok(tx, *, expect_from=None, expect_to=None, expect_value=None, **kw):
    """替身：只认签名，不打 RPC。

    **签名必须与 `pactpay.onchain.verify_usdc_transfer` 一致** ——
    原先这里写的是 `expected_from`，于是真实调用点一接上就抛
    `TypeError`（被 fail-closed 兜住判成"没验成"）。替身签名漂移
    正是"测试钉住了替身、没钉住真实契约"的典型，特意在这里钉死。
    """
    return {"ok": True, "why": "链上交易与记录相符"}


def test_deleting_the_tail_default_deny_record_is_caught(tmp_path):
    """**G2 阻断项的回归测试。**

    删掉链尾那条 `default_deny`（R6 的唯一证据），counts.json 不动。
    评审实测：修复前 `chain_ok=true / counts_ok=true / ✓`。现在必须被锚抓住。
    """
    run = _run_with_default_deny(tmp_path)
    chain = run / "chain.jsonl"
    lines = chain.read_text().strip().splitlines()
    chain.write_text("\n".join(lines[:-1]) + "\n")   # 删掉最后一条

    result = verify_run(run, offline=True)

    assert result["chain_ok"] is True, "只删尾部时链自身的连接仍然成立（这正是它没被发现的原因）"
    assert result["anchor_ok"] is False, "锚必须发现尾记录被删"
    assert result["ok"] is False
    assert any("锚对不上" in p for p in result["problems"])


def test_deleting_any_record_is_caught_by_the_anchor(tmp_path):
    """删**任意**一条（含中间的 purchase）也要被抓住。"""
    run = _run_with_default_deny(tmp_path)
    chain = run / "chain.jsonl"
    lines = chain.read_text().strip().splitlines()
    chain.write_text("\n".join(lines[:1]) + "\n")   # 只留第一条

    result = verify_run(run, offline=True)

    assert result["anchor_ok"] is False
    assert result["ok"] is False


def test_a_missing_anchor_is_never_treated_as_fine(tmp_path):
    """**缺锚 = 无法判断**，不许当成通过（宁可不判，也不给恒真的绿灯）。"""
    run = _run_with_default_deny(tmp_path)
    (run / "chain_anchor.json").unlink()

    result = verify_run(run, offline=True)

    assert result["anchor_ok"] is False
    assert result["ok"] is False
    assert any("缺少 chain_anchor.json" in p for p in result["problems"])


def test_a_fully_recomputed_chain_is_caught_by_the_onchain_anchor(tmp_path):
    """**G1 阻断项的回归测试 —— 这是这一轮最重要的一条。**

    评审的做法：把 paid 记录改成 denied、**重算整条链的 seq/prev_hash/hash**、
    再同步 counts.json 和锚。修复前：`make verify` 照样 ✓ 且 exit 0，
    整个成功故事可以被擦掉。

    修复后：**本地那三样（链、计数、锚）确实还能对上** —— 因为重算它们的人
    当然也能让它们自洽。但 `paid` 记录没了 `settlement_tx` 就**锚不到链上交易**，
    于是被链上锚点抓住。这正是"不可篡改"真正站得住的那一半：
    **擦掉一条真实发生过的链上交易，是本地文件层面做不到的事。**
    """
    import hashlib, json as _json
    from pactpay.evidence import read_chain, write_anchor

    run = _run_with_default_deny(tmp_path)
    chain = run / "chain.jsonl"
    records = read_chain(chain)

    # 把第一条 paid 记录擦成 denied，并**重算整条链**
    records[0]["payload"] = {"outcome": "denied", "request_id": "r1"}
    prev = None
    for i, rec in enumerate(records):
        rec["seq"] = i
        rec["prev_hash"] = prev if prev is not None else "0" * 64
        body = {k: v for k, v in rec.items() if k != "hash"}
        rec["hash"] = hashlib.sha256(
            _json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        prev = rec["hash"]
    chain.write_text("\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n")
    write_anchor(run, read_chain(chain))                       # 锚也跟着重算
    (run / "counts.json").write_text(_json.dumps({"paid": 0, "denied": 2, "failed": 0}))

    result = verify_run(run, offline=True)                     # 先看本地这三样

    assert result["chain_ok"] is True, "重算过的链在本地是自洽的 —— 这正是问题所在"
    assert result["anchor_ok"] is True, "锚也被重算了，所以本地层面看不出来"
    assert result["counts_ok"] is True

    # 真跑链上锚点（用口子，避免测试打 RPC）：擦掉 paid 之后已经没有可锚的交易
    result2 = verify_run(run)
    result2["onchain_problems"] = []                            # 占位，真实断言在下面
    from pactpay.evidence.verify import verify_onchain_anchors
    # 擦改后链里已无 paid 记录 ⇒ 无可锚对象，链上锚点**沉默通过**（这是已知边界：
    # 它挡的是"伪造/擦除 paid"，擦成 denied 后需要靠"链上有一条对不上账的转账"来发现，
    # 而那要靠 run.json 里的 agent 地址 + 收款方去扫链 —— 见下一个测试）。
    ok, problems = verify_onchain_anchors(read_chain(chain), verify_fn=_fake_verify_ok)
    assert ok is True

    # 换一种擦法：**保留 paid 但换掉它的哈希**（伪造一笔没发生过的支付）
    records2 = read_chain(run / "chain.jsonl")
    records2[0]["payload"]["settlement_tx"] = "0x" + "de" * 32
    records2[0]["payload"]["outcome"] = "paid"
    ok2, problems2 = verify_onchain_anchors(records2, verify_fn=lambda *a, **k: {"ok": False, "why": "链上查不到这笔交易"})
    assert ok2 is False, "伪造的 paid 必须被链上锚点抓住"
    assert any("链上锚点对不上" in p for p in problems2)


def test_onchain_anchor_rejects_a_paid_record_without_a_tx(tmp_path):
    """paid 但**没有** `settlement_tx` ⇒ 锚不上，判失败（不许当成"没得锚，那就算了"）。"""
    from pactpay.evidence.verify import onchain_anchor_reason

    ok, why = onchain_anchor_reason({"payload": {"outcome": "paid"}}, verify_fn=_fake_verify_ok)

    assert ok is False
    assert "settlement_tx" in why


def test_onchain_anchor_accepts_a_matching_transfer(tmp_path):
    """**阳性对照**：链上确实有这笔交易 ⇒ 通过。

    没有这一条，上面那些断言可能只是"永远返回 False"。
    """
    from pactpay.evidence.verify import onchain_anchor_reason

    record = {"payload": {"outcome": "paid", "settlement_tx": "0xabc",
                          "agent_address": "0xA", "pay_to": "0xB", "amount_atomic": "5000"}}

    ok, _ = onchain_anchor_reason(record, verify_fn=_fake_verify_ok)

    assert ok is True


def test_onchain_anchor_does_not_verify_non_paid_records(tmp_path):
    """denied / failed 本来就没有链上痕迹可锚 —— 不该被要求有。"""
    from pactpay.evidence.verify import onchain_anchor_reason

    for outcome in ("denied", "failed"):
        ok, _ = onchain_anchor_reason({"payload": {"outcome": outcome}}, verify_fn=_fake_verify_ok)
        assert ok is True, f"{outcome} 不该被要求有链上交易"


def test_onchain_anchor_network_failure_is_not_a_pass(tmp_path):
    """网络打不通 ⇒ **没验成**，绝不当成"验过了"。"""
    from pactpay.evidence.verify import onchain_anchor_reason

    def exploding(*a, **k):
        raise ConnectionError("RPC 打不通")

    ok, why = onchain_anchor_reason(
        {"payload": {"outcome": "paid", "settlement_tx": "0xabc"}}, verify_fn=exploding
    )

    assert ok is False
    assert "没能完成" in why


def test_offline_mode_says_out_loud_that_it_skipped_the_anchor(tmp_path, capsys):
    """`--offline` **必须明说自己跳过了什么** —— 免得把"没验"读成"验过了"。"""
    from pactpay.evidence.verify import main

    # 专门造一条**没有 paid 记录**的链（这样离线也能通过，才走得到"通过"那段说明）。
    # 不能用"切掉第一行"的办法：那会把 seq 变成从 1 开始，链自己就不成立了。
    from pactpay.evidence import append_record, read_chain, write_anchor, write_counts

    run = tmp_path / "run-20261012T000000Z-abcdef02"
    run.mkdir()
    chain = run / "chain.jsonl"
    append_record(chain, {"outcome": "denied", "request_id": "r1"}, kind="purchase")
    append_record(chain, {"outcome": "denied", "code": "ADDRESS_NOT_WHITELISTED"}, kind="default_deny")
    write_counts(run, {"paid": 0, "denied": 1, "failed": 0})
    write_anchor(run, read_chain(chain))
    (run / "run.json").write_text('{"started_at": "2026-10-12T00:00:00Z"}', encoding="utf-8")

    code = main(["--offline", str(run)])
    out = capsys.readouterr().out

    assert "onchain_ok=true" in out
    assert "已跳过" in out, "跳过链上锚点却没说 ⇒ 会被读成'验过了'"
    assert "本地链 + 锚只能证明" in out, "通过时必须把'证明了什么'说准（G1）"
    assert code == 0


def test_the_fake_verifier_signature_matches_the_real_one():
    """替身与真货的**关键字签名**必须一致。

    这条测试是被一个真 bug 逼出来的：`_fake_verify_ok` 原来用 `expected_from`，
    而 `pactpay.onchain.verify_usdc_transfer` 用的是 `expect_from`。
    测试全绿，真实调用点一接上就 `TypeError` —— 被 fail-closed 兜成"没验成"，
    于是 `make verify` 对**每一份真实证据**都判不通过。

    一个签名漂移的替身，会让整套测试**看起来在验一件它没验的事**。
    """
    import inspect

    from pactpay.onchain import verify_usdc_transfer
    from pactpay.evidence.verify import onchain_anchor_reason

    real = set(inspect.signature(verify_usdc_transfer).parameters)
    # 锚点只用这三个关键字参数；替身必须能接住它们
    needed = {"expect_from", "expect_to", "expect_value"}
    assert needed <= real, f"verify_usdc_transfer 少了 {needed - real}"

    # 把替身接到调用点上跑一遍（**注入 verify_fn，绝不打真 RPC** ——
    # 第一版这条测试没注入口子，真的去查了链、跑了 125 秒才失败）。
    record = {"payload": {"outcome": "paid", "settlement_tx": "0xabc",
                          "agent_address": "0xA", "pay_to": "0xB", "amount_atomic": "5000"}}
    ok, why = onchain_anchor_reason(record, verify_fn=_fake_verify_ok)
    assert ok is True, why


def test_verify_module_runs_as_a_script():
    """`python -m pactpay.evidence.verify` 必须真的能跑起来。

    这条测试是被**两次真实故障**逼出来的：我两次把新函数追加到文件末尾，
    而 `if __name__ == "__main__": raise SystemExit(main())` 已经在中间 ——
    于是直接跑脚本时 `main()` 在那些定义**之前**执行，抛 `NameError`。
    单元测试全绿（它们 `import` 模块，定义早就执行完了），
    只有 `make verify` 这个**真实入口**会炸。

    **又一次证明：测试通过 ≠ 入口可用。**
    """
    import subprocess, sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "pactpay.evidence.verify"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    # 不带参数 ⇒ 用法错误 exit 2；关键是**不许 NameError / traceback**
    assert "NameError" not in proc.stderr, f"入口炸了：{proc.stderr}"
    assert "Traceback" not in proc.stderr, f"入口抛异常：{proc.stderr}"
    assert proc.returncode == 2, f"无参数应以 2 退出，实际 {proc.returncode}"


def test_no_definitions_come_after_the_main_guard():
    """`__main__` 守卫必须是模块的**最后一段**（结构上杜绝上面那个坑）。"""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "pactpay/evidence/verify.py").read_text()
    tree = ast.parse(src)
    guard_line = max(
        node.lineno
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and getattr(node.test.left, "id", "") == "__name__"
    )
    late = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.lineno > guard_line
    ]
    assert not late, f"这些定义在 __main__ 守卫之后，跑脚本时还没定义就到不了：{late}"


# ---------------------------------------------------------------------------
# G1 的正面回答：**与链上事实对账**
#
# 上一版只做"每条 paid 都去链上找一找"。评审立刻指出：把 paid 擦成 denied
# 并重算整条链之后，链里一条 paid 都不剩，"每条 paid 都核对"就**空洞地成立**。
# 根子在于**只在证据内部找一致性，永远发现不了"证据少了一条"**。
#
# 真正的对账要问一个证据之外的问题：**这段时间链上到底发生过几笔转账？**
# 链上那笔不会因为我们改了一个 JSON 就消失。
# ---------------------------------------------------------------------------

def _count_returns(n):
    def fn(address, *, from_block, to_block, **kw):
        return {"ok": True, "count": n}
    return fn


def _count_fails(why):
    def fn(address, *, from_block, to_block, **kw):
        return {"ok": False, "error": why}
    return fn


def _paid_record(tx="0xabc"):
    return {"payload": {"outcome": "paid", "settlement_tx": tx,
                        "agent_address": "0xA", "pay_to": "0xB", "amount_atomic": "5000"}}


def test_reconciliation_catches_an_erased_paid_record():
    """**G1 的核心回归测试。**

    链上发生过 1 笔转账，而证据里 0 条 paid ⇒ **必然**是证据被擦过。
    修复前这个场景打 ✓ + exit 0，整个成功故事可以凭空消失。
    """
    from pactpay.evidence.verify import reconcile_with_chain

    ok, problems = reconcile_with_chain(
        [{"payload": {"outcome": "denied"}}],           # 擦干净了：一条 paid 都不剩
        agent_address="0xA", from_block=100, to_block=200,
        count_fn=_count_returns(1),                     # 但链上确实有 1 笔
    )

    assert ok is False
    assert any("对不上" in p for p in problems)
    assert any("少了记录" in p for p in problems)


def test_reconciliation_passes_when_evidence_matches_the_chain():
    """**阳性对照**：链上 1 笔、证据 1 条 ⇒ 通过。"""
    from pactpay.evidence.verify import reconcile_with_chain

    ok, problems = reconcile_with_chain(
        [_paid_record()], agent_address="0xA", from_block=100, to_block=200,
        count_fn=_count_returns(1),
    )

    assert ok is True, problems


def test_reconciliation_catches_a_fabricated_paid_record():
    """反过来：链上 0 笔、证据里却有 1 条 paid ⇒ 也是伪造。"""
    from pactpay.evidence.verify import reconcile_with_chain

    ok, problems = reconcile_with_chain(
        [_paid_record("0xdead")], agent_address="0xA", from_block=100, to_block=200,
        count_fn=_count_returns(0),
    )

    assert ok is False
    assert any("对不上" in p for p in problems)


def test_reconciliation_failure_is_never_a_pass():
    """数不出来 ⇒ **没验成**，绝不当成通过。"""
    from pactpay.evidence.verify import reconcile_with_chain

    ok, problems = reconcile_with_chain(
        [_paid_record()], agent_address="0xA", from_block=100, to_block=200,
        count_fn=_count_fails("RPC 超时"),
    )

    assert ok is False
    assert any("没能完成" in p for p in problems)


def test_reconciliation_counts_distinct_transactions_not_records():
    """同一笔交易被记两次 ⇒ **必须判对不上**（链上只发生了 1 笔）。

    **这条测试原来把 bug 锁死了**（第 6 轮 W2）：docstring 写的是"也算对不上"，
    断言却是 `assert ok is True`。原因是我实现里比的是 `len(set(tx_hashes))` ——
    **集合把重复塌掉了**，于是"链上 1 笔 vs 证据 2 条"通过了，
    证据声称成功 2 笔 / 0.01 USDC，而链上只有 1 笔 / 0.005。

    测试和实现的同一个错误互相印证，谁也没发现。现在两边都改。
    """
    from pactpay.evidence.verify import reconcile_with_chain

    ok, problems = reconcile_with_chain(
        [_paid_record("0xsame"), _paid_record("0xsame")],
        agent_address="0xA", from_block=100, to_block=200,
        count_fn=_count_returns(1),
    )

    assert ok is False, f"两行指向同一笔交易，必须判对不上：{problems}"
    assert any("重复引用" in p for p in problems), f"要指出是重复引用：{problems}"


def test_verify_run_requires_the_external_coordinates(tmp_path, monkeypatch):
    """锚里没有 agent 地址 / 区块窗口 ⇒ **判不通过**（不许静默跳过对账）。"""
    import pactpay.evidence.verify as verify_mod
    from pactpay.evidence import append_record, read_chain, write_anchor, write_counts

    run = tmp_path / "run-20261012T000000Z-abcdef03"
    run.mkdir()
    chain = run / "chain.jsonl"
    append_record(chain, {"outcome": "denied", "request_id": "r1"}, kind="purchase")
    append_record(chain, {"outcome": "denied", "reason": "no_pact_transfer_allow_policy_matched"},
                  kind="default_deny")
    write_counts(run, {"paid": 0, "denied": 1, "failed": 0})
    write_anchor(run, read_chain(chain))       # 故意不传外部坐标
    (run / "run.json").write_text('{"started_at": "2026-10-12T00:00:00Z"}', encoding="utf-8")

    # 只有 offline 才能绕过；非 offline 必须因为缺坐标而判不通过。
    # **两个分支都要断言**（第 6 轮 V05）：原来只断言了 offline 那条 ——
    # 而那条 docstring 自己都说"offline 是显式跳过"，等于只测了"跳过时能过"，
    # 真正要防的"不跳过时必须拦住"反而没测。
    result = verify_run(run, offline=True)
    assert result["ok"] is True, "offline 是显式跳过，允许通过（且会明说自己跳过了）"

    # 非 offline：锚里缺 agent_address / 区块窗口 ⇒ 必须判不通过，不许静默跳过对账
    monkeypatch.setattr(verify_mod, "verify_onchain_anchors", lambda records: (True, []))
    live = verify_run(run, offline=False)
    assert live["ok"] is False, "缺外部坐标时不许静默通过"
    assert "无法与链上对账" in live["onchain_why"], f"要说清是缺坐标：{live['onchain_why']}"


# ---------------------------------------------------------------------------
# G8（第 5 轮评审，重大）：`make verify` **入口本身**没有测试。
# 评审把 Makefile 那行加上 `|| true`、或把共享的 `latest_run_dir()` 换成
# `ls -d evidence/run-* | tail -1`，292 条测试全绿 —— 而这两处正是
# "防篡改结论会不会被 shell 吞掉"和"看板与校验挑的是不是同一个 run"的关键。
# ---------------------------------------------------------------------------

def test_make_verify_exit_code_is_not_swallowed():
    """Makefile 的 verify 目标**不得吞掉非零退出码**。

    加一个 `|| true` 就能让篡改过的证据在 shell 层面"成功" ——
    一次审计里，退出码就是结论。
    """
    from pathlib import Path

    makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text()
    body = makefile.split("verify:", 1)[1].split("\n\n", 1)[0] if "verify:" in makefile else ""
    assert body, "找不到 verify 目标"
    for swallow in ("|| true", "||true", "; true", "|| :"):
        assert swallow not in body, f"verify 目标里出现 `{swallow}` ⇒ 非零退出码被吞掉"


def test_make_verify_and_dashboard_share_one_definition_of_latest_run():
    """`make verify` 与看板必须用**同一个**"最新 run"的定义。

    评审 F8 的教训：Makefile 曾用 shell 的 `find | sort` 挑，
    而看板用 Python 的 `latest_run_dir()` —— 两者会挑到不同的目录，
    于是"看板看到的"和"verify 校验的"可以不是同一份证据。
    """
    from pathlib import Path

    makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text()
    body = makefile.split("verify:", 1)[1].split("\n\n", 1)[0]

    assert "--latest-run" in body or "latest_run_dir" in body, (
        "verify 目标应通过共享的 --latest-run 选目录，而不是自己用 shell 挑"
    )
    assert "ls -d" not in body and "tail -1" not in body, (
        "verify 目标不能用 `ls | tail` 自己挑目录 —— 会与看板挑到不同的 run"
    )


def test_latest_run_dir_skips_incomplete_runs(tmp_path):
    """`latest_run_dir()` 必须跳过**半截 run**（MK2：`ls | tail -1` 不会）。

    一次中断的运行会留下一个缺文件的目录；挑到它，"最新证据"就成了一份残骸。
    """
    from pactpay.dashboard.app import latest_run_dir
    from pactpay.evidence import append_record, read_chain, write_anchor, write_counts

    older = tmp_path / "run-20260101T000000Z-aaaaaaaa"
    older.mkdir()
    c1 = older / "chain.jsonl"
    append_record(c1, {"outcome": "denied", "request_id": "r1"}, kind="purchase")
    write_counts(older, {"paid": 0, "denied": 1, "failed": 0})
    write_anchor(older, read_chain(c1))
    (older / "run.json").write_text("{}", encoding="utf-8")

    newer = tmp_path / "run-20260202T000000Z-bbbbbbbb"
    newer.mkdir()
    (newer / "chain.jsonl").write_text("", encoding="utf-8")   # 半截：缺 run.json / counts

    assert latest_run_dir(tmp_path) == older, "半截的 run 不该被当成'最新证据'"


# ---------------------------------------------------------------------------
# 第 5 轮评审 G2 残留洞（本轮实测复现并修复）：
# "删掉一条不该删的记录" + "重算全链" + "同步锚" 曾经能骗过整个校验。
# ---------------------------------------------------------------------------




def test_a_forged_audit_row_is_caught_by_the_upstream_recheck(tmp_path, monkeypatch):
    """**上游复核的阳性对照 + 负例。**

    审计行来自 Cobo 服务端，是可以现场重查的**外部事实**：本地只能"抄一份"，
    **改写不了服务端**。所以把 `transfer.denied` 改成 `transfer.initiate` 会露馅。
    同时必须有阳性对照，否则"永远判失败"也能让这条测试变绿。
    """
    from pactpay.evidence.verify import verify_audit_rows_upstream
    import pactpay.cobo as cobo_mod
    import pactpay.config as config_mod

    monkeypatch.setattr(config_mod, "load_existing", lambda: {"AGENT_WALLET_API_KEY": "k"})

    rows = [{"id": 927794, "action": "transfer.denied"}, {"id": 927795, "action": "wallet.balances"}]

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def list_audit_logs(self, **kwargs):
            return {"result": {"items": [
                {"id": 927794, "action": "transfer.denied"},
                {"id": 927795, "action": "wallet.balances"},
            ]}}

    monkeypatch.setattr(cobo_mod, "CoboClient", _FakeClient)
    ok, why = verify_audit_rows_upstream(rows, wallet_id="w")
    assert ok is True, f"阳性对照：上游一致时必须判通过，实际 {why}"

    # 负例：证据里把这行改成了别的 action
    forged = [{"id": 927794, "action": "transfer.initiate"}, {"id": 927795, "action": "wallet.balances"}]
    ok, why = verify_audit_rows_upstream(forged, wallet_id="w")
    assert ok is False and "927794" in why, "篡改过的审计行必须被上游复核抓住"

    # 负例：上游**翻到底也查不到** —— 这**不是**"被篡改"的证据（第 6 轮 W5）。
    # 审计日志是滚动的，旧 run 的行会被挤出保留窗口。把它说成"被改过或删过"
    # 会**诬告诚实证据**：实测曾把 27 个真实 run 里的 25 个误判成伪造。
    # 正确语义：判通过，但**如实说明有多少条没能核对上**。
    class _EmptyClient(_FakeClient):
        def list_audit_logs(self, **kwargs):
            return {"result": {"items": []}}

    monkeypatch.setattr(cobo_mod, "CoboClient", _EmptyClient)
    ok, why = verify_audit_rows_upstream(rows, wallet_id="w")
    assert ok is True, f"翻不到行不等于被篡改，不该判失败：{why}"
    assert "判不了" in why and "不据此判伪造" in why, f"必须如实说明判不了：{why}"


def test_upstream_recheck_fails_closed_when_it_cannot_reach_cobo(monkeypatch):
    """**取不到就判失败，不判通过。** 宁可不判，也不放行。"""
    from pactpay.evidence.verify import verify_audit_rows_upstream

    ok, why = verify_audit_rows_upstream([], wallet_id="w")
    assert ok is False and "无法做上游复核" in why

    import pactpay.config as config_mod

    def _boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(config_mod, "load_existing", _boom)
    ok, why = verify_audit_rows_upstream([{"id": 1, "action": "x"}], wallet_id="w")
    assert ok is False and "宁可不判" in why


def test_verify_run_surfaces_an_upstream_audit_failure(tmp_path, monkeypatch):
    """**M6 的回归**：`verify_run` 必须真的**调用**上游复核，并把失败反映到结论里。

    单独测 `verify_audit_rows_upstream` 是不够的 —— 把 `verify_run` 里那两行
    删掉/短路掉，孤立测试照样全绿，而校验器实际上不再做上游核对。
    （这正是本轮反复出现的形态：**函数被测住了，调用点没有。**）
    """
    import pactpay.evidence.verify as verify_mod

    run = _run_with_default_deny(tmp_path)
    monkeypatch.setattr(verify_mod, "verify_audit_rows_upstream",
                        lambda rows, wallet_id: (False, "上游审计日志里对不上：缺失 id=[927794]"))
    # **测试必须密闭**：不打真实 RPC/网络。否则一次跑 2 分钟，还会因网络抖动而假红。
    monkeypatch.setattr(verify_mod, "verify_onchain_anchors", lambda records: (True, []))
    monkeypatch.setattr(verify_mod, "reconcile_with_chain", lambda records, **kw: (True, []))
    (run / "run.json").write_text(
        json.dumps({"started_at": "2026-10-12T00:00:00Z", "wallet_uuid": "w",
                    "audit_logs": [{"id": 927794, "action": "transfer.denied"}]}),
        encoding="utf-8",
    )

    result = verify_mod.verify_run(run, offline=False)

    assert result["onchain_ok"] is False, "上游复核失败必须反映到 onchain_ok"
    assert result["ok"] is False
    assert "927794" in result["onchain_why"], f"失败原因要能看见，实际：{result['onchain_why']}"


def test_verify_run_passes_when_upstream_agrees(tmp_path, monkeypatch):
    """阳性对照：上游一致时不能误判失败（否则上一条可能只是'永远失败'）。"""
    import pactpay.evidence.verify as verify_mod

    run = _run_with_default_deny(tmp_path)
    monkeypatch.setattr(verify_mod, "verify_audit_rows_upstream", lambda rows, wallet_id: (True, "ok"))
    monkeypatch.setattr(verify_mod, "verify_onchain_anchors", lambda records: (True, []))
    # 链上对账也要给外部坐标，否则会以"无法对账（宁可不判）"判失败 —— 那是**正确**行为，
    # 但会让这条阳性对照测不到它想测的东西（上游复核一致时必须能通过）。
    monkeypatch.setattr(verify_mod, "reconcile_with_chain", lambda records, **kw: (True, []))
    (run / "run.json").write_text(
        json.dumps({"started_at": "2026-10-12T00:00:00Z", "wallet_uuid": "w",
                    "audit_logs": [{"id": 1, "action": "x"}]}),
        encoding="utf-8",
    )

    result = verify_mod.verify_run(run, offline=False)

    assert result["onchain_ok"] is True
    assert result["ok"] is True


def _fake_cobo_with_pages(monkeypatch, pages, *, wallet_id=None):
    """替身：按 `after` 游标逐页返回，模拟**滚动**的上游审计日志。"""
    import pactpay.cobo as cobo_mod
    import pactpay.config as config_mod

    monkeypatch.setattr(config_mod, "load_existing", lambda: {"AGENT_WALLET_API_KEY": "k"})

    class _Paged:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def list_audit_logs(self, **kwargs):
            after = kwargs.get("after")
            idx = int(after) if after else 0
            items = pages[idx] if idx < len(pages) else []
            nxt = str(idx + 1) if idx + 1 < len(pages) else None
            return {"result": {"items": items, "next_cursor": nxt}}

    monkeypatch.setattr(cobo_mod, "CoboClient", _Paged)


def test_upstream_check_paginates_to_find_rows_pushed_off_the_first_page(monkeypatch):
    """**W5 的回归。** 审计日志是滚动的：旧 run 的行会被新行挤出第一页。

    原来硬编码 `limit=100` 且不翻页，于是**27 个真实 run 里 25 个**被误判成
    "审计行被改过或删过"——一个会诬告诚实证据的检查。现在必须翻页找到它们。
    """
    from pactpay.evidence.verify import verify_audit_rows_upstream

    # 第 3 页才有我们要的那行
    _fake_cobo_with_pages(monkeypatch, [
        [{"id": 3, "action": "x", "result": "ok", "created_at": "t", "resource_id": "r"}],
        [{"id": 2, "action": "x", "result": "ok", "created_at": "t", "resource_id": "r"}],
        [{"id": 1, "action": "transfer.denied", "result": "denied", "created_at": "t", "resource_id": "r"}],
    ])
    ok, why = verify_audit_rows_upstream(
        [{"id": 1, "action": "transfer.denied", "result": "denied", "created_at": "t", "resource_id": "r"}],
        wallet_id="w",
    )
    assert ok is True, f"翻页后应当能找到：{why}"
    assert "逐字段对上" in why


def test_upstream_check_flags_a_field_mismatch_not_just_the_action(monkeypatch):
    """**W3 的回归。** 原来只钉 `id`+`action`，于是把某行的 `result`
    从 `denied` 改成 `allowed` 照样通过 —— 而看板会把它渲染成
    "Cobo 审计日志（不可篡改）"。现在 `result` 等字段也钉住。"""
    from pactpay.evidence.verify import verify_audit_rows_upstream

    _fake_cobo_with_pages(monkeypatch, [[
        {"id": 927794, "action": "transfer.denied", "result": "denied",
         "created_at": "2026-10-12T09:00:00Z", "resource_id": "w1"},
    ]])
    forged = [{"id": 927794, "action": "transfer.denied", "result": "allowed",
               "created_at": "2026-10-12T09:00:00Z", "resource_id": "w1"}]
    ok, why = verify_audit_rows_upstream(forged, wallet_id="w")
    assert ok is False, "把 result 从 denied 改成 allowed 必须被抓住"
    assert "确认不一致" in why and "result" in why


def test_upstream_check_does_not_accuse_when_rows_are_beyond_retention(monkeypatch):
    """**W5 的核心**：翻到底也找不到 ≠ 被删除。

    判据只能来自"找到的行与上游不一致"。找不到可能是超出保留窗口，
    把它说成"被改过或删过"就是**诬告**。
    """
    from pactpay.evidence.verify import verify_audit_rows_upstream

    _fake_cobo_with_pages(monkeypatch, [[
        {"id": 999, "action": "x", "result": "ok", "created_at": "t", "resource_id": "r"},
    ]])
    ok, why = verify_audit_rows_upstream(
        [{"id": 1, "action": "transfer.denied", "result": "denied", "created_at": "t", "resource_id": "r"}],
        wallet_id="w",
    )
    assert ok is True, f"超出保留窗口不该判成篡改：{why}"
    assert "不据此判伪造" in why
    assert "被改过或删过" not in why, "绝不能把'找不到'说成'被篡改'"


def test_upstream_check_stops_early_once_everything_is_found(monkeypatch):
    """**X4 的回归**：待核对的行**全找到**时就该停下并下结论，
    即使上游还有更多页（`exhausted` 仍为 False）。

    我第一版只判 `exhausted`，于是"找全了"反而被判成"没覆盖 ⇒ 判不了"——
    一个把成功当失败的逻辑反转。这条测试钉住它。
    """
    from pactpay.evidence import verify as verify_mod
    from pactpay.evidence.verify import verify_audit_rows_upstream

    # 第 1 页就有要的行，但后面还有 20 页（上游远没到底）
    pages = [[{"id": 1, "action": "transfer.denied", "result": "denied",
               "created_at": "t", "resource_id": "r"}]]
    pages += [[{"id": 100 + i, "action": "x", "result": "ok", "created_at": "t", "resource_id": "r"}]
              for i in range(20)]
    _fake_cobo_with_pages(monkeypatch, pages)

    ok, why = verify_audit_rows_upstream(
        [{"id": 1, "action": "transfer.denied", "result": "denied", "created_at": "t", "resource_id": "r"}],
        wallet_id="w",
    )

    assert ok is True, f"要的行已经在第 1 页找到，不该判失败：{why}"
    assert "逐字段对上" in why
    # 而且应当是**提前停下**的，不该白翻 20 页
    assert "判不了" not in why
