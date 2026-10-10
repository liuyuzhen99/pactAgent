"""Dashboard（T8）的验收单测。

对应方案 A8（人工验收）：页面上必须能看到①原始任务与总预算 ②采购计划
③每笔结果 ④被拒那笔的原因与命中策略 ⑤审计日志。

这里把"页面要显示什么"做成**可机器核验的视图模型**，人工验收时只需对照页面。
期望值手写字面量。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pactpay.dashboard.app import _render, build_view_model, create_app, latest_run_dir
from pactpay.evidence import append_record

EVIDENCE = {
    "run_id": "877d1e88-0000-4000-8000-000000000000",
    "started_at": "2026-10-12T02:36:28.699773+00:00",
    "task": "帮我买一份数据报告",
    "budget_usd": "0.1",
    "pact_id": "36c39e98-be22-45f0-adc0-19ed5d16e1bb",
    "pact_status": "active",
    "payee": "http://127.0.0.1:8001",
    "wallet_uuid": "f342c5cf-b6ed-44a2-8d33-55b554f8a4d0",
    "agent_address": "0x1f3963c9ebc26cce128ec00a779a948bcc2f7ffa",
    "purchases": [
        {
            "resource": "/api/cheap",
            "amount_usd": "0.005",
            "description": "原始运营数据",
            "outcome": "paid",
            "http_status": 200,
            "data": {"report_id": "cheap-raw-001"},
            "settlement_tx": "0x9ccf917be2d98cc49b0c7c54bbdfc0fa8b594e8db5b3e1cd42c42b5f7684b59a",
            "denial": None,
        },
        {
            "resource": "/api/premium",
            "amount_usd": "0.05",
            "description": "深度分析结论",
            "outcome": "denied",
            "http_status": 422,
            "data": None,
            "settlement_tx": None,
            "denial": {
                "code": "X402_POLICY_DENIED",
                "reason": "x402_no_matching_payment",
                "policy_reason": "matched_pact_transfer_deny_if",
                "policy_id": "7ef70031-ab6d-4d9b-9039-f0f06d8d9925",
                "reject_reason": "policy_denied",
            },
        },
    ],
    "audit_logs": [
        # 读操作噪声——**不该出现在页面的决策列表里**。
        # `created_at` 必须给：真实审计行**一定**带这个字段（服务端 field 是 required）。
        # 之前这三行没给，于是它们先被 `within_run()` 的时间窗滤掉了，
        # 让"动作过滤"变成了一条**没被测到的**防线——删掉过滤器测试照样全绿（评审 B5）。
        {"id": 926962, "action": "wallet.balances", "result": "allowed", "authz_details": {},
         "created_at": "2026-10-12T02:36:30.000000Z"},
        {"id": 926963, "action": "wallet.read", "result": "allowed", "authz_details": {},
         "created_at": "2026-10-12T02:36:30.500000Z"},
        {"id": 926964, "action": "wallet.node_status", "result": "allowed", "authz_details": {},
         "created_at": "2026-10-12T02:36:31.000000Z"},
        # 真实决策
        {
            "id": 926960,
            "action": "transfer.initiate",
            "result": "allowed",
            "authz_details": {"pact_id": "36c39e98-be22-45f0-adc0-19ed5d16e1bb", "stage": "permission_check"},
            "created_at": "2026-10-12T02:36:32.561852Z",
        },
        {
            "id": 926973,
            "action": "transfer.denied",
            "result": "denied",
            "authz_details": {"reason": "no_pact_transfer_allow_policy_matched", "policy_id": None},
            "created_at": "2026-10-12T02:42:43.942053Z",
        },
    ],
}


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    _write_run(tmp_path, EVIDENCE)
    return tmp_path


def test_view_model_shows_task_budget_and_pact():
    """① 原始任务与总预算、② Pact 本身——用户要看的是"我当初怎么定的规矩"。"""
    view = build_view_model(EVIDENCE)

    assert view["task"] == "帮我买一份数据报告"
    assert view["budget_usd"] == "0.1"
    assert view["pact_id"] == "36c39e98-be22-45f0-adc0-19ed5d16e1bb"
    assert view["pact_status"] == "active"


def test_view_model_lists_both_purchases_with_outcomes():
    """③ 每一笔的结果——一笔成功一笔被拒。"""
    view = build_view_model(EVIDENCE)

    # 三种结果都要在：`failed` 是"策略放行了但没结算成功"，只数 paid/denied 会把它静默丢掉
    assert view["summary"] == {"paid": 1, "denied": 1, "failed": 0}
    assert [p["resource"] for p in view["purchases"]] == ["/api/cheap", "/api/premium"]
    assert view["purchases"][0]["settlement_tx"].startswith("0x9ccf917b")
    assert view["purchases"][1]["settlement_tx"] is None


def test_view_model_explains_the_denial_and_which_policy_was_hit():
    """④ 被拒那笔的原因与命中的策略——R9 的核心。"""
    denied = build_view_model(EVIDENCE)["purchases"][1]

    assert denied["denial"]["code"] == "X402_POLICY_DENIED"
    assert denied["denial"]["policy_reason"] == "matched_pact_transfer_deny_if"
    assert denied["denial"]["policy_id"] == "7ef70031-ab6d-4d9b-9039-f0f06d8d9925"


def test_denial_evidence_source_is_labelled_honestly():
    """证据来源必须如实标注：x402 的拒绝**不在** Cobo 审计日志里。

    这条是 R8 的诚实性守门人：Cobo 的审计日志里根本没有这条 x402 拒绝
    （实测等 2/8/20 秒都没有），所以标注必须是 `cobo_payment_response`，
    **不许**标成 `cobo_audit_log`。
    """
    denied = build_view_model(EVIDENCE)["purchases"][1]

    assert denied["denial_evidence_source"] == "cobo_payment_response"
    assert denied["audit_row_id"] is None

    # 而转账路径的拒绝**是**记进审计日志的，能匹配上就必须匹配上
    transfer_denial = {
        "code": "ADDRESS_NOT_WHITELISTED",
        "policy_reason": "no_pact_transfer_allow_policy_matched",
    }
    view = build_view_model({**EVIDENCE, "purchases": [{**EVIDENCE["purchases"][1], "denial": transfer_denial}]})
    assert view["purchases"][0]["denial_evidence_source"] == "cobo_audit_log"
    assert view["purchases"][0]["audit_row_id"] == 926973


def test_audit_list_keeps_decisions_and_drops_read_noise():
    """⑤ 审计日志——只列**决策**，把读操作噪声滤掉。

    实测里 `wallet.balances` / `wallet.read` / `wallet.node_status` 会淹没真正的决策行
    （一次运行里读操作能有 30+ 条），页面上直接铺开就什么都看不出来了。
    """
    view = build_view_model(EVIDENCE)

    actions = [row["action"] for row in view["audit_decisions"]]
    assert actions == ["transfer.initiate", "transfer.denied"]
    # 这三条噪声行**在时间窗内**（有 created_at），所以它们只能靠动作过滤挡掉。
    # 断言必须对此敏感：删掉 `DECISION_ACTION_PREFIXES` 过滤，这里就会红。
    assert "wallet.balances" not in actions
    assert "wallet.read" not in actions
    assert "wallet.node_status" not in actions
    assert view["audit_decisions"][1]["reason"] == "no_pact_transfer_allow_policy_matched"


def test_page_renders_the_whole_story(evidence_dir: Path):
    """/ 必须真的把这些内容渲染进 HTML（人工验收就是看这一页）。"""
    client = TestClient(create_app(evidence_dir=evidence_dir))

    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    for needle in ("帮我买一份数据报告", "0.1", "/api/cheap", "/api/premium",
                   "X402_POLICY_DENIED", "matched_pact_transfer_deny_if", "0x9ccf917b"):
        assert needle in html, f"页面上找不到 {needle!r}"


def test_api_exposes_the_view_model(evidence_dir: Path):
    client = TestClient(create_app(evidence_dir=evidence_dir))

    payload = client.get("/api/evidence").json()

    assert payload["summary"] == {"paid": 1, "denied": 1, "failed": 0}


def test_api_actually_serves_the_decisions_array(evidence_dir: Path):
    """**A8a 的判据就是这个端点**：`curl .../api/evidence` 上必须取得到 `.decisions[i]`。

    评审 B6：原来只断言了 `summary`，把端点改成只回 `{"summary": ...}` 测试照样全绿
    —— 而用例 A8a 恰恰是打在这个端点上的。断言必须落在**用例真正使用的那一层**。
    """
    client = TestClient(create_app(evidence_dir=evidence_dir))

    payload = client.get("/api/evidence").json()

    decisions = payload["decisions"]
    assert len(decisions) == 2
    assert decisions[0]["outcome"] == "paid"
    assert decisions[1]["outcome"] == "denied"
    # A8a 断言的那三样（平台原始码 + 策略依据 + 命中策略 id）必须在端点响应里
    assert decisions[1]["code"] == "X402_POLICY_DENIED"
    assert decisions[1]["policy_reason"] == "matched_pact_transfer_deny_if"
    assert decisions[1]["policy_id"] == "7ef70031-ab6d-4d9b-9039-f0f06d8d9925"


def test_api_serves_every_decision_kind_not_just_purchases(tmp_path: Path):
    """端点上也要能看到默认拒绝那条——它是最容易在"只回 summary"的退化里丢掉的一类。"""
    _write_run(tmp_path, {**EVIDENCE, "default_deny": DEFAULT_DENY, "audit_logs": AUDIT_WITH_DEFAULT_DENY})
    client = TestClient(create_app(evidence_dir=tmp_path))

    payload = client.get("/api/evidence").json()

    kinds = [d["kind"] for d in payload["decisions"]]
    assert kinds == ["purchase", "purchase", "default_deny"]
    assert payload["decisions"][2]["code"] == "ADDRESS_NOT_WHITELISTED"


def test_says_so_plainly_when_there_is_no_evidence_yet(tmp_path: Path):
    """还没跑过演示时，页面要**明说没有数据**，而不是渲染一个空白页让人以为坏了。"""
    client = TestClient(create_app(evidence_dir=tmp_path))

    assert client.get("/api/evidence").status_code == 404
    response = client.get("/")
    assert response.status_code == 200
    assert "还没有证据" in response.text


def test_stale_audit_row_does_not_corroborate_this_runs_denial():
    """Dashboard **必须**把本次运行的开始时间传给匹配器。

    这里放的审计行 reason 与拒绝完全一致，但**时间早于本次运行**——它是另一次
    操作留下的（实测踩到过：14 分钟前另一笔转账的拒绝）。
    如果 build_view_model 忘了传 since，页面就会把这条 x402 拒绝标成
    "在不可篡改审计日志里"，**又一次说出那句假话**。
    """
    stale_row = {
        "id": 926920,
        "action": "transfer.denied",
        "result": "denied",
        "authz_details": {
            "reason": "matched_pact_transfer_deny_if",
            "policy_id": "456b6620-94c4-44f5-97dc-f5f58f283bdf",
        },
        "created_at": "2026-10-12T02:22:11.441726Z",
    }
    evidence = {
        **EVIDENCE,
        "started_at": "2026-10-12T02:36:28.699773+00:00",
        "audit_logs": [stale_row],
    }

    denied = build_view_model(evidence)["purchases"][1]

    assert denied["denial"]["policy_reason"] == "matched_pact_transfer_deny_if"
    assert denied["denial_evidence_source"] == "cobo_payment_response"
    assert denied["audit_row_id"] is None


def _write_run(tmp_path: Path, evidence: dict) -> Path:
    """把一份证据写成 run 目录（与 demo 真实产出的形状一致）。"""
    run = tmp_path / "run-20261012T023628Z-abc"
    run.mkdir(parents=True, exist_ok=True)
    chain = run / "chain.jsonl"
    for purchase in evidence.get("purchases") or []:
        append_record(chain, purchase, kind="purchase")
    if evidence.get("default_deny"):
        append_record(chain, evidence["default_deny"], kind="default_deny")
    (run / "run.json").write_text(json.dumps(evidence, ensure_ascii=False))
    (run / "counts.json").write_text(
        json.dumps(
            {k: sum(1 for p in evidence.get("purchases") or [] if p.get("outcome") == k)
             for k in {p.get("outcome") for p in evidence.get("purchases") or []}}
        )
    )
    return run


def test_audit_decisions_are_limited_to_this_run():
    """审计表只该列**本次运行**产生的决策行。

    实测：Cobo 的审计接口是按 wallet 返回历史的，一次运行能带回十几条**之前**
    运行的 `transfer.initiate`。全列出来的话，页面看起来像"成功了 11 笔"，
    而这一次实际只有 1 笔——整页证据的可信度就没了。
    （和拒绝匹配那个坑同源：**历史行会被误当成本次的事实**。）
    """
    stale = {
        "id": 926921, "action": "transfer.initiate", "result": "allowed",
        "authz_details": {}, "created_at": "2026-10-12T02:22:12.148445Z",
    }
    fresh = {
        "id": 926962, "action": "transfer.initiate", "result": "allowed",
        "authz_details": {}, "created_at": "2026-10-12T02:36:32.561852Z",
    }
    evidence = {
        **EVIDENCE,
        "started_at": "2026-10-12T02:36:28.699773+00:00",
        "audit_logs": [stale, fresh],
    }

    ids = [row["id"] for row in build_view_model(evidence)["audit_decisions"]]

    assert ids == [926962]


# ---- R6 默认拒绝在页面上的呈现 ----
DEFAULT_DENY = {
    "outcome": "denied",
    "code": "ADDRESS_NOT_WHITELISTED",
    "reason": "no_pact_transfer_allow_policy_matched",
    "policy_reason": "no_pact_transfer_allow_policy_matched",
    "policy_id": None,
    "requested_address": "0x1111111111111111111111111111111111111111",
    "settlement_tx": None,
}
AUDIT_WITH_DEFAULT_DENY = [
    {
        "id": 926973,
        "action": "transfer.denied",
        "result": "denied",
        "authz_details": {"reason": "no_pact_transfer_allow_policy_matched", "policy_id": None},
        "created_at": "2026-10-12T02:36:40.000000Z",
    }
]


def test_default_deny_is_shown_and_backed_by_the_audit_log():
    """默认拒绝这一段必须出现，而且**这条拒绝真的在 Cobo 审计日志里**。

    与 x402 那笔不同：走转账路径的拒绝 Cobo 是记账的，所以来源应标成
    `cobo_audit_log` 并给出审计行 id——这就是 R8"拒绝也被记录"唯一立得住的地方。
    """
    evidence = {
        **EVIDENCE,
        "default_deny": DEFAULT_DENY,
        "audit_logs": AUDIT_WITH_DEFAULT_DENY,
    }

    dd = build_view_model(evidence)["default_deny"]

    assert dd["code"] == "ADDRESS_NOT_WHITELISTED"
    assert dd["denial_evidence_source"] == "cobo_audit_log"
    assert dd["audit_row_id"] == 926973


def test_default_deny_is_honest_when_the_audit_row_is_missing():
    """审计日志里没有对应行时，不许硬说"有"。"""
    evidence = {**EVIDENCE, "default_deny": DEFAULT_DENY, "audit_logs": []}

    dd = build_view_model(evidence)["default_deny"]

    assert dd["denial_evidence_source"] == "cobo_payment_response"
    assert dd["audit_row_id"] is None


def test_page_renders_the_default_deny_section(tmp_path: Path):
    evidence = {**EVIDENCE, "default_deny": DEFAULT_DENY, "audit_logs": AUDIT_WITH_DEFAULT_DENY}
    _write_run(tmp_path, evidence)

    html = TestClient(create_app(evidence_dir=tmp_path)).get("/").text

    assert "ADDRESS_NOT_WHITELISTED" in html
    assert "没有任何允许策略匹配" in html
    assert "0x1111111111111111111111111111111111111111" in html


def test_page_says_so_when_there_was_no_default_deny_check(tmp_path: Path):
    _write_run(tmp_path, EVIDENCE)

    html = TestClient(create_app(evidence_dir=tmp_path)).get("/").text

    assert "本次运行没有做默认拒绝检查" in html


def test_audit_rows_expose_their_stage():
    """审计行必须把 `stage` 暴露出来。

    实测（2026-10-12）：`transfer.initiate/allowed` 记录的是**前置权限检查通过**
    （`stage="permission_check"`），而且 Cobo **对被策略拒绝的操作也会写这一行**——
    本次运行里 3 条 `allowed` 只对应 1 笔成功付款。
    不把 stage 显示出来，读者会把审计表读成"成功了 3 笔"，与页面上方的"成功 1 笔"自相矛盾。
    """
    with_stage = {
        **EVIDENCE,
        "started_at": "2026-10-12T02:36:28.699773+00:00",
        "audit_logs": [
            {
                "id": 927052,
                "action": "transfer.initiate",
                "result": "allowed",
                "authz_details": {"stage": "permission_check", "required_permission": "can_transfer"},
                "created_at": "2026-10-12T02:36:40.000000Z",
            }
        ],
    }

    row = build_view_model(with_stage)["audit_decisions"][0]

    assert row["stage"] == "permission_check"
    assert row["required_permission"] == "can_transfer"


# ---- A8a：统一的"每一次策略决策"视图 ----
def test_decisions_is_a_unified_ordered_list_of_every_policy_decision():
    """R8 的措辞是"每一次策略决策（允许/拒绝/待审批）"。

    接口原本把三类东西分开存（`purchases` / `default_deny` / `audit_decisions`），
    **没有一个统一的"决策"视图**——A8a 要按 `.decisions[i].code` 机器校验就取不到。
    这里要求 `decisions` 把每次决策按发生顺序列出来，且**每条都带 code 与 policy_id**。
    """
    view = build_view_model({**EVIDENCE, "default_deny": DEFAULT_DENY})

    decisions = view["decisions"]

    assert [d["outcome"] for d in decisions] == ["paid", "denied", "denied"]
    assert [d["kind"] for d in decisions] == ["purchase", "purchase", "default_deny"]


def test_every_decision_carries_the_code_and_the_policy_that_decided_it():
    """R9 要求页面能说清"为什么被拒、依据哪条策略"——所以每条决策都要带这两样。"""
    view = build_view_model({**EVIDENCE, "default_deny": DEFAULT_DENY})

    paid, denied, dd = view["decisions"]

    assert paid["code"] is None and paid["outcome"] == "paid"
    assert denied["code"] == "X402_POLICY_DENIED"
    assert denied["policy_reason"] == "matched_pact_transfer_deny_if"
    assert denied["policy_id"] == "7ef70031-ab6d-4d9b-9039-f0f06d8d9925"
    assert dd["code"] == "ADDRESS_NOT_WHITELISTED"


def test_decision_index_is_stable_and_zero_based():
    """用例是按下标取用的（`.decisions[1]`），下标语义必须钉住。"""
    view = build_view_model({**EVIDENCE, "default_deny": DEFAULT_DENY})

    assert [d["index"] for d in view["decisions"]] == [0, 1, 2]


def test_decisions_exist_even_without_a_default_deny_check():
    view = build_view_model(EVIDENCE)

    assert len(view["decisions"]) == 2


# =====================================================================================
# 评审第 2 轮 N4/N5：`failed` 下游没接完；最新 run 会挑到中断留下的空目录
# =====================================================================================


def _failed_run() -> dict:
    return {
        "task": "t", "budget_usd": "0.1", "pact_id": "p", "pact_status": "active",
        "summary": {"paid": 0, "denied": 0, "failed": 1},
        "purchases": [{
            "resource": "/api/cheap", "amount_usd": "0.005", "description": "d",
            "outcome": "failed", "http_status": 500,
            "denial": {"code": "PAYMENT_NOT_SETTLED", "reason": "重放返回 HTTP 500"},
            "settlement_tx": None,
        }],
        "audit_logs": [],
    }


def test_failed_is_flagged_apart_from_denied():
    """`failed` 必须能被渲染层识别出来（N4）——它与 `denied` 的结论**相反**。"""
    view = build_view_model(_failed_run())

    assert view["purchases"][0]["failed"] is True
    assert view["summary"]["failed"] == 1


def test_failed_card_does_not_claim_a_policy_rejection():
    """**核心断言**：`failed` 那张卡片不许说成"被策略拒绝、签名前被拦下"。

    `failed` = 策略**放行**了但没结算（钱可能动了，是基础设施故障）；
    `denied` = 策略在签名前挡住（钱没动，是边界生效）。
    混在一起会让看板把**故障读成成绩**——正好把这个项目要证明的事说反。
    """
    html = _render(build_view_model(_failed_run()))
    card = html.split('class="card"')[1].split("</div></div>")[0]

    assert "被策略拒绝" not in card, f"failed 那张卡说成了策略拒绝：{card}"
    assert "签名前就被拦下" not in card
    assert "未结算" in card
    assert "可能已经动了" in card


def test_failed_shows_up_in_the_summary():
    """汇总原先只有"成功/被拒"两栏，一次只含 failed 的运行会显示"被拒 0"
    却又有卡片报错 —— 自相矛盾。"""
    html = _render(build_view_model(_failed_run()))

    assert "未结算 1" in html


def test_a_paid_run_still_renders_as_success():
    """反向：正常成功那笔不能被 failed 分支抢走。"""
    view = build_view_model({
        "task": "t", "purchases": [{
            "resource": "/api/cheap", "amount_usd": "0.005", "description": "d",
            "outcome": "paid", "http_status": 200, "data": {"report_id": "x"},
            "settlement_tx": "0xabc",
        }], "audit_logs": [],
    })
    html = _render(view)

    assert view["purchases"][0]["failed"] is False
    assert "支付成功" in html


def test_latest_run_dir_skips_incomplete_runs(tmp_path):
    """**N5**：中断运行会留下**空目录**，而它按名字排序是"最新"的。

    实测踩到：`make verify` 挑中空目录后报 `chain_ok=true (ok（0 条）) / counts_ok=false`，
    页面也渲染成"什么都没有"——看起来像证据链坏了，其实只是挑错了目录。
    """
    (tmp_path / "run-20260101T000000Z-aaaaaaaa").mkdir()
    (tmp_path / "run-20260101T000000Z-aaaaaaaa" / "run.json").write_text("{}")
    # 更晚的一次运行，但**中途炸了**，只剩一个空目录
    (tmp_path / "run-20260102T000000Z-bbbbbbbb").mkdir()

    assert latest_run_dir(tmp_path).name == "run-20260101T000000Z-aaaaaaaa"


def test_latest_run_dir_returns_none_when_everything_is_incomplete(tmp_path):
    """一次完整运行都没有时返回 `None`，而不是挑一个空目录来渲染。"""
    (tmp_path / "run-20260102T000000Z-bbbbbbbb").mkdir()

    assert latest_run_dir(tmp_path) is None


def _write_failed_run(root: Path) -> Path:
    """在磁盘上造一个**真实形状**的失败运行：`chain.jsonl` + `run.json`。

    **踩过的坑**：`load_run` 的 `purchases` 是从 `chain.jsonl` 读的，
    只写 `run.json` 的话会被覆盖成空列表 —— 我第一版测试就是这么写的，
    端点返回 `failed: 0`，看起来像功能坏了，其实是 fixture 没造对。
    按真实落盘形状造，测的才是真链路。
    """
    from pactpay.evidence import append_record

    run = root / "run-20260101T000000Z-ffffffff"
    run.mkdir()
    record = _failed_run()["purchases"][0]
    append_record(run / "chain.jsonl", record, kind="purchase")
    meta = {k: v for k, v in _failed_run().items() if k != "purchases"}
    (run / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    return run


def test_api_endpoint_exposes_a_failed_purchase(tmp_path):
    """**端点层**也要能看到 `failed`（评审 N4 要求落在这层，不只是 view model）。

    上一轮我只在 `build_view_model` 上测了 `failed` 标记 —— 但用户和验收用例
    读的是 `/api/evidence` 的响应。断言必须打在**真正被消费的那一层**，
    这正是 B6 那次的教训。
    """
    _write_failed_run(tmp_path)

    payload = TestClient(create_app(evidence_dir=tmp_path)).get("/api/evidence").json()

    assert payload["summary"]["failed"] == 1
    assert payload["purchases"][0]["failed"] is True
    assert payload["purchases"][0]["outcome"] == "failed"


def test_page_renders_a_failed_run_from_a_real_directory(tmp_path):
    """整条链路（目录 → 端点 → 页面）走一遍，确认 `failed` 不会被渲染成策略拒绝。"""
    _write_failed_run(tmp_path)

    html = TestClient(create_app(evidence_dir=tmp_path)).get("/").text

    assert "未结算" in html
    assert "可能已经动了" in html
    assert "被策略拒绝" not in html.split('class="card"')[1].split("</div></div>")[0]


def test_verify_run_accepts_a_run_containing_a_failed_purchase(tmp_path):
    """`verify_run` 必须能处理含 `failed` 的运行，且计数与链一致。

    三态里加了 `failed` 之后，`counts.json` 与链里数出来的必须仍然对得上 ——
    否则一次"策略放行但没结算"的运行会被证据校验误判成"计数对不上"。
    """
    from pactpay.evidence import count_outcomes, write_counts
    from pactpay.evidence.verify import verify_run

    run = _write_failed_run(tmp_path)
    counts = count_outcomes([_failed_run()["purchases"][0]])
    write_counts(run, counts)  # 注意：参数是 **run 目录**，它自己拼 counts.json

    result = verify_run(run, offline=True)

    assert result["counts"]["failed"] == 1, "failed 没被数进去"
    assert result["counts_ok"] is True, f"计数对不上：{result}"
    assert result["chain_ok"] is True


def test_verify_run_does_not_falsely_report_a_count_mismatch(tmp_path):
    """**回归测试**：只含一种 outcome 的运行不许被误判成"计数对不上"。

    本仓库自己踩到的真 bug：运行时计数器 `count_outcomes` 总是写全
    `paid/denied/failed` 三个键（缺席的记 0），而 `count_records` 从链里数出来时
    **只含出现过的 outcome**。直接 `==` 比就永远不等。
    demo 那次侥幸通过，只是因为它恰好同时有 paid 与 denied —— 掩盖了这个 bug，
    直到我为了测 `failed` 造了一个"只有 failed"的运行才暴露出来。
    """
    from pactpay.evidence import append_record, count_outcomes, write_counts
    from pactpay.evidence.verify import verify_run

    for only_outcome in ("paid", "denied", "failed"):
        run = tmp_path / f"run-20260101T000000Z-{only_outcome}"
        run.mkdir()
        payload = {"outcome": only_outcome, "amount_usd": "0.005", "request_id": f"req-{only_outcome}"}
        append_record(run / "chain.jsonl", payload, kind="purchase")
        write_counts(run, count_outcomes([payload]))

        result = verify_run(run, offline=True)

        assert result["counts_ok"] is True, (
            f"只含 {only_outcome} 的运行被误判成计数对不上："
            f"运行时={result['recorded_counts']} 链里={result['counts']}"
        )
        assert result["counts"][only_outcome] == 1


def test_an_unknown_outcome_is_never_rendered_as_a_policy_denial(tmp_path):
    """**看板不许替一个它不认识的 result 下结论**（第 4 轮评审 F11）。

    渲染里原来是个 `else:` 兜底：任何没认出来的 outcome 都会被画成
    「⛔ 被策略拒绝 …… 链上交易 无 —— 签名前就被拦下」。
    一个兜底分支**断言了一个它并不知道的结论** —— 如果 agent 哪天多出一种结果
    （`error` / `timeout` / …），看板会把一次基础设施故障说成"边界生效"，
    正好把这个项目要证明的事**说反**。
    """
    from pactpay.dashboard.app import create_app

    run_dir = _write_run(
        tmp_path,
        {
            **EVIDENCE,
            "purchases": [
                {
                    "resource": "/api/cheap",
                    "amount_usd": "0.005",
                    "description": "原始运营数据",
                    "outcome": "timeout",
                    "http_status": 504,
                    "data": None,
                    "settlement_tx": None,
                    "denial": None,
                }
            ],
        },
    )
    page = TestClient(create_app(evidence_dir=run_dir.parent)).get("/").text

    # **只看这张卡片**，不看整页：页脚有一段静态说明里本来就含"被策略拒绝"这个词，
    # 拿整页做断言会误伤（第一版就是这么错的）。
    card_start = page.index("/api/cheap")
    card = page[card_start : page.index("</div></div>", card_start)]

    assert "未知结果" in card, f"未知 outcome 没有被标成'未知'：\n{card}"
    assert "签名前就被拦下" not in card, (
        "未知 outcome 被渲染成了'签名前就被拦下' —— 兜底分支在替它下结论"
    )
    assert "被策略拒绝" not in card, "未知 outcome 被渲染成了'被策略拒绝'"
    assert "支付成功" not in card, "未知 outcome 被渲染成了'支付成功'"
    assert "不做任何判断" in card, "未知 outcome 的卡片没有说明'页面不做判断'"


# ---------------------------------------------------------------------------
# 第 5 轮自查发现的**真实性缺陷**（评审没抓到，我自己跑真实 demo 时抓到的）
#
# 现场：最新产物里 `/api/premium`（x402 路径的拒绝）的卡片上写着
#     证据来源：Cobo 审计日志（不可篡改）   审计行 id 927625
# 但 927625 是一条 `action="transfer.denied"`、`request.amount="0.05"` 的行
# —— 那是 **A5c② 探针**（故意打的那笔超限转账）留下的，**不是**这次 x402 拒绝的。
# demo 自己打印的是"② 审计佐证 ✗（x402 付款的拒绝不进 Cobo 审计日志）"。
#
# 也就是说：**页面给一条根本没有审计行的拒绝，安上了别人家的审计行**，
# 还宣称它"不可篡改"。这正是这个项目要证明的反面。
#
# 根因：匹配只按 `policy_reason` 字符串 + 时间窗。时间窗挡住了**跨运行**的误配，
# 但本次运行里探针**自己**造出的行，reason 完全相同、时间还**晚于**该拒绝，
# 于是照样被认领。
# ---------------------------------------------------------------------------

def test_an_x402_denial_is_never_backed_by_a_transfer_audit_row():
    """**核心**：x402（payment）路径的拒绝，绝不许拿 `transfer.denied` 行当证据。

    实测事实：x402 付款路径的拒绝**不写审计日志**。所以它的证据来源必须是
    `cobo_payment_response`（Cobo 签发的付款响应），而不是"不可篡改审计日志"。
    """
    from pactpay.evidence import denial_evidence_source

    x402_denial = {
        "api": "payment",
        "code": "X402_POLICY_DENIED",
        "policy_reason": "matched_pact_transfer_deny_if",
        "policy_id": "ebb03030-03a9-44e0-b3a8-06939317d24d",
        "details": {"amount": "0.05"},
    }
    # 同一次运行里，探针留下的那条**同 reason** 的审计行
    probe_row = {
        "id": 927625,
        "action": "transfer.denied",
        "result": "denied",
        "authz_details": {"reason": "matched_pact_transfer_deny_if",
                          "policy_id": "ebb03030-03a9-44e0-b3a8-06939317d24d"},
        "request": {"amount": "0.05"},
        "created_at": "2099-01-01T00:00:00Z",   # 甚至比拒绝还晚
    }

    source = denial_evidence_source(x402_denial, [probe_row], since="2020-01-01T00:00:00Z")

    assert source == "cobo_payment_response", (
        f"x402 拒绝被标成了 {source!r} —— 页面会给一条没有审计行的拒绝安上别人家的行"
    )


def test_a_transfer_denial_still_finds_its_own_audit_row():
    """**阳性对照**：转账路径的拒绝**应该**找得到它自己的审计行。

    没有这一条，上面那条可能只是"永远返回 payment_response"。
    """
    from pactpay.evidence import denial_evidence_source

    transfer_denial = {
        "api": "transfer",
        "code": "TRANSFER_LIMIT_EXCEEDED",
        "policy_reason": "matched_pact_transfer_deny_if",
        "policy_id": "ebb03030-03a9-44e0-b3a8-06939317d24d",
        "details": {"amount": "0.05"},
    }
    own_row = {
        "id": 927625,
        "action": "transfer.denied",
        "result": "denied",
        "authz_details": {"reason": "matched_pact_transfer_deny_if"},
        "request": {"amount": "0.05"},
        "created_at": "2026-10-12T08:39:03Z",
    }

    source = denial_evidence_source(transfer_denial, [own_row], since="2026-10-12T08:00:00Z")

    assert source == "cobo_audit_log", (
        "转账路径的拒绝没找到它自己的审计行 —— 这个判据被修得永远不匹配了"
    )


def test_a_non_transfer_audit_action_never_backs_a_transfer_denial():
    """结构性核对：`action` 不是 `transfer.*` 的行，不许拿来佐证转账拒绝。"""
    from pactpay.evidence import denial_evidence_source

    denial = {
        "api": "transfer",
        "policy_reason": "matched_pact_transfer_deny_if",
        "details": {"amount": "0.05"},
    }
    wrong_action = {
        "id": 1,
        "action": "payment.denied",          # 不是 transfer.*
        "result": "denied",
        "authz_details": {"reason": "matched_pact_transfer_deny_if"},
        "request": {"amount": "0.05"},
        "created_at": "2026-10-12T08:39:03Z",
    }

    source = denial_evidence_source(denial, [wrong_action], since="2026-10-12T08:00:00Z")

    assert source == "cobo_payment_response", (
        "一条非 transfer 动作的审计行被用来佐证转账拒绝"
    )


def test_amount_mismatch_prevents_attribution():
    """同一次运行里同 reason 的多笔拒绝，靠**金额**区分。"""
    from pactpay.evidence import denial_evidence_source

    denial = {
        "api": "transfer",
        "policy_reason": "matched_pact_transfer_deny_if",
        "details": {"amount": "0.05"},
    }
    other_amount_row = {
        "id": 2,
        "action": "transfer.denied",
        "result": "denied",
        "authz_details": {"reason": "matched_pact_transfer_deny_if"},
        "request": {"amount": "0.5"},        # 金额对不上
        "created_at": "2026-10-12T08:39:03Z",
    }

    source = denial_evidence_source(denial, [other_amount_row], since="2026-10-12T08:00:00Z")

    assert source == "cobo_payment_response", "金额对不上的审计行被认领了"


def test_the_evidence_record_carries_its_api_path():
    """两条路径造出的拒绝记录必须**自带 `api` 标注**（判据的来源）。

    这是上一条测试的前提：没有 `api` 字段，匹配就只能回去猜 reason 字符串。
    """
    from pactpay.agent import normalize_transfer_denial
    from pactpay.cobo import PolicyDenied

    denied = PolicyDenied(
        status=403,
        code="TRANSFER_LIMIT_EXCEEDED",
        reason="policy_denied",
        details={"amount": "0.05", "policy_id": "p1"},
        suggestion="",
        raw={},
    )

    record = normalize_transfer_denial(denied)

    assert record.get("api") == "transfer", (
        f"转账路径的拒绝记录没标 api（得到 {record.get('api')!r}）—— 审计匹配只能退回猜字符串"
    )
