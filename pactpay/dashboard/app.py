"""证据展示页（T8）。

需求 R9：打开一个页面就能看懂 —— Agent 做了什么、为什么成功、为什么被拒、依据哪条规则。

设计上只做一件事：把 `evidence/` 里最近一次运行的证据**如实**渲染出来，不加工、不美化。
两处刻意的诚实：

1. **每条拒绝都标注证据来源**。实测 x402 付款的拒绝**不在** Cobo 审计日志里，
   只有转账路径的拒绝才在。所以页面上会写清这条拒绝是"审计日志里有"还是
   "只有付款响应"——**绝不把付款响应说成不可篡改审计**。
2. **审计列表只留决策行**。一次运行里读操作（`wallet.balances` 等）能有 30+ 条，
   直接铺开会把真正的允许/拒绝决策淹没。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from pactpay.evidence import (
    EVIDENCE_DIR,
    ChainCorruptError,
    count_outcomes,
    load_run,
    denial_evidence_source,
    match_audit_denial,
    within_run,
)

# 哪些动作算"策略决策"。其余（wallet.read / wallet.balances / wallet.node_status /
# user_transaction.list …）都是查询类噪声，不上页面。
DECISION_ACTION_PREFIXES = ("transfer.", "contract_call.", "payment.", "pact.", "delegation.")

SOURCE_LABELS = {
    "cobo_audit_log": "Cobo 审计日志（不可篡改）",
    "cobo_payment_response": "Cobo 付款响应（该路径审计日志不记录此拒绝）",
}


def is_complete_run(run_dir: Path) -> bool:
    """这个运行目录是不是**跑完整了**。

    判据是 `run.json` 在**且能解析**。只看目录存在是不够的：
    `demo` 会在**任何实质工作之前**就建好目录，而它的 A4/A5c/failed 守卫
    一旦 `raise`（那是**设计行为**）就会留下一个空目录。

    **"能解析"这一半是评审第 3 轮 #5 补的**：`write_run_meta` 用的是非原子的
    `Path.write_text`，进程在写一半时被杀就会留下**截断的 JSON**。
    原来只判 `is_file()`，于是这种半截文件被当成"完整运行"选中，
    `make verify` 打出「✓ 证据校验通过」而看板 HTTP 500 —— 一边说没事，一边炸了。
    """
    run_json = Path(run_dir) / "run.json"
    if not run_json.is_file():
        return False
    try:
        json.loads(run_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    return True


def latest_run_dir(evidence_dir: Path = EVIDENCE_DIR) -> Path | None:
    """最近一次**完整**运行的证据目录；没有则 None。

    **为什么必须过滤完整性**（评审第 2 轮 N5）：原先只按目录名取最新，
    而中断的运行会留下空目录。实测踩到：`make verify` 挑中一个空目录后报
    `chain_ok=true (ok（0 条）) / counts_ok=false`，exit 2 —— 页面也渲染成"什么都没有"，
    看起来像证据链坏了，其实只是挑错了目录。
    """
    dirs = sorted(d for d in Path(evidence_dir).glob("run-*") if d.is_dir() and is_complete_run(d))
    return dirs[-1] if dirs else None


def build_view_model(evidence: dict[str, Any]) -> dict[str, Any]:
    """把原始证据整理成页面好渲染的形状。**不改变任何事实性字段**。"""
    audit_logs = evidence.get("audit_logs") or []

    started_at = evidence.get("started_at")
    purchases = []
    for p in evidence.get("purchases") or []:
        denial = p.get("denial")
        row: dict[str, Any] = {
            "resource": p.get("resource"),
            "amount_usd": p.get("amount_usd"),
            "description": p.get("description"),
            "outcome": p.get("outcome"),
            "http_status": p.get("http_status"),
            "data": p.get("data"),
            "settlement_tx": p.get("settlement_tx"),
            "denial": denial,
            "denial_evidence_source": None,
            "audit_row_id": None,
            # `failed` 必须能被渲染层分辨出来：它与 `denied` 的**结论相反**
            # （见渲染处的说明）。只把 outcome 原样带下去，渲染层就得自己猜。
            "failed": p.get("outcome") == "failed",
            # **只有真的是 `denied` 才带这个标记**（第 4 轮评审 F11）。
            # 渲染层原来用 `else:` 兜底当成"被策略拒绝"——于是任何未知 outcome
            # 都会被画成"签名前就被拦下"。现在渲染层按这个**显式**标记分支，
            # 认不出来就如实说"不认识"，不替它下结论。
            "denied": p.get("outcome") == "denied",
        }
        if denial:
            # 必须带 since=本次运行开始时间：否则会拿历史运行里的同 reason 拒绝行冒充
            matched = match_audit_denial(denial, audit_logs, since=started_at)
            row["denial_evidence_source"] = denial_evidence_source(denial, audit_logs, since=started_at)
            row["audit_row_id"] = matched.get("id") if matched else None
        purchases.append(row)

    decisions = []
    for log in audit_logs:
        action = str(log.get("action") or "")
        if not action.startswith(DECISION_ACTION_PREFIXES):
            continue
        # 只看本次运行产生的行：审计接口按 wallet 返回历史，不过滤会把历次运行
        # 的记录一起列出来（实测一次能带回十几条），页面就显得"成功了很多笔"。
        if not within_run(log, started_at):
            continue
        authz = log.get("authz_details") or {}
        decisions.append(
            {
                "id": log.get("id"),
                "action": action,
                "result": log.get("result"),
                "reason": authz.get("reason"),
                "policy_id": authz.get("policy_id"),
                # stage 必须暴露：`transfer.initiate/allowed` 只是**前置权限检查通过**，
                # Cobo 对"随后被策略拒绝"的操作也会写这一行。不显示 stage，
                # 读者会把审计表读成"成功了 N 笔"，与上方的实际笔数自相矛盾。
                "stage": authz.get("stage"),
                "required_permission": authz.get("required_permission"),
                "created_at": log.get("created_at"),
            }
        )

    default_deny = evidence.get("default_deny")
    if default_deny:
        matched = match_audit_denial(default_deny, audit_logs, since=started_at)
        default_deny = {
            **default_deny,
            "denial_evidence_source": denial_evidence_source(
                default_deny, audit_logs, since=started_at
            ),
            "audit_row_id": matched.get("id") if matched else None,
        }

    # R8 的措辞是"每一次策略决策（允许/拒绝/待审批）"。上面那三个键是**按来源**分的
    # （采购 / 默认拒绝 / 审计行），没有一个**按发生顺序**的统一视图；A8a 要按
    # `.decisions[i].code` 机器校验就取不到。这里补一个：每次决策一条，按顺序编号，
    # 每条都带 `code` 与 `policy_id`（R9 要求页面能说清"为什么被拒、依据哪条策略"）。
    def _decision_row(index: int, kind: str, source: dict[str, Any], denial: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": index,
            "kind": kind,
            "label": source.get("description") or source.get("label"),
            "resource": source.get("resource"),
            "amount_usd": source.get("amount_usd"),
            "outcome": source.get("outcome"),
            "http_status": source.get("http_status"),
            "code": denial.get("code"),
            "reason": denial.get("reason"),
            "policy_reason": denial.get("policy_reason"),
            "policy_id": denial.get("policy_id"),
            "settlement_tx": source.get("settlement_tx"),
        }

    decisions_view: list[dict[str, Any]] = [
        _decision_row(i, "purchase", p, p.get("denial") or {}) for i, p in enumerate(purchases)
    ]
    if default_deny:
        decisions_view.append(_decision_row(len(decisions_view), "default_deny", default_deny, default_deny))

    return {
        "run_id": evidence.get("run_id"),
        "started_at": evidence.get("started_at"),
        "task": evidence.get("task"),
        "budget_usd": evidence.get("budget_usd"),
        "pact_id": evidence.get("pact_id"),
        "pact_status": evidence.get("pact_status"),
        "agent_address": evidence.get("agent_address"),
        "payee": evidence.get("payee"),
        "summary": count_outcomes(purchases),
        "decisions": decisions_view,
        "purchases": purchases,
        "default_deny": default_deny,
        "audit_decisions": decisions,
    }


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _render(view: dict[str, Any]) -> str:
    # `failed` 必须在汇总里**单独可见**。原先只显示"成功/被拒"两栏，
    # 一次只含 failed 的运行会渲染成"被拒 0"却又有卡片写着被拒 —— 自相矛盾。
    failed_count = view["summary"].get("failed", 0)
    failed_summary = (
        f' · <span class="warn">未结算 {failed_count}</span>' if failed_count else ""
    )
    rows = []
    for p in view["purchases"]:
        if p["outcome"] == "paid":
            detail = (
                f'<div class="ok">✅ 支付成功</div>'
                f'<div class="kv">链上交易 <code>{_e(p["settlement_tx"])}</code></div>'
                f'<div class="kv">拿到数据 <code>{_e(json.dumps(p["data"], ensure_ascii=False))}</code></div>'
            )
        elif p.get("failed"):
            # `failed` = 策略**放行**了但没结算成功。钱**可能已经动了**。
            # 绝不能和 `denied` 一起渲染成"被策略拒绝、签名前拦下"——那是**说反了**：
            # 前者是基础设施故障，后者才是边界生效。混在一起会让看板把故障读成成绩。
            d = p["denial"] or {}
            detail = (
                f'<div class="warn">⚠️ 未结算（HTTP {_e(p["http_status"])}）—— 不是策略拒绝</div>'
                f'<div class="kv">原因 <code>{_e(d.get("code"))}</code>'
                f'：{_e(d.get("reason"))}</div>'
                f'<div class="kv warn">钱<b>可能已经动了</b> —— 策略放行了，是结算环节出的问题，'
                f'需人工核对钱包余额</div>'
                f'<div class="kv">结算哈希 <code>{_e(p.get("settlement_tx")) or "无"}</code></div>'
            )
        elif p.get("denied"):
            d = p["denial"] or {}
            source = SOURCE_LABELS.get(p["denial_evidence_source"], p["denial_evidence_source"])
            audit_line = (
                f'<div class="kv">审计行 id <code>{_e(p["audit_row_id"])}</code></div>'
                if p["audit_row_id"]
                else '<div class="kv warn">该路径 Cobo 审计日志不记录此拒绝</div>'
            )
            detail = (
                f'<div class="no">⛔ 被策略拒绝（HTTP {_e(p["http_status"])}）</div>'
                f'<div class="kv">拒绝码 <code>{_e(d.get("code"))}</code></div>'
                f'<div class="kv">触发原因 <code>{_e(d.get("policy_reason"))}</code>'
                f'（<code>{_e(d.get("reject_reason"))}</code>）</div>'
                f'<div class="kv">命中策略 <code>{_e(d.get("policy_id"))}</code></div>'
                f'<div class="kv">链上交易 <b>无</b> —— 签名前就被拦下</div>'
                f'<div class="kv">证据来源：{_e(source)}</div>'
                f"{audit_line}"
            )
        else:
            # **未知结果不许猜**（第 4 轮评审 F11）。
            # 这里原来是 `else:`，于是**任何**没认出来的 outcome 都会被渲染成
            # 「⛔ 被策略拒绝 …… 链上交易 无 —— 签名前就被拦下」——
            # 一个兜底分支**断言了一个它并不知道的结论**。
            # 如果哪天 agent 多出一种结果（比如 `error`、`timeout`），
            # 看板会把一次基础设施故障说成"边界生效"，正好把这个项目要证明的事说反。
            # 现在如实说"不认识"，并把它显眼地标出来。
            detail = (
                f'<div class="warn">⚠️ 未知结果 <code>{_e(p.get("outcome"))}</code>'
                f'（HTTP {_e(p.get("http_status"))}）—— 本页不认识这种结果，'
                f'不做任何判断</div>'
                f'<div class="kv warn">不要把它读成"被拒绝"或"成功"：'
                f'页面不知道它意味着什么，需要人工查看原始证据</div>'
                f'<div class="kv">结算哈希 '
                f'<code>{_e(p.get("settlement_tx")) or "无"}</code></div>'
            )
        rows.append(
            f'<div class="card"><div class="head">{_e(p["resource"])} · {_e(p["amount_usd"])} USDC'
            f' · {_e(p["description"])}</div>{detail}</div>'
        )

    dd = view.get("default_deny")
    if dd:
        src = SOURCE_LABELS.get(dd.get("denial_evidence_source"), dd.get("denial_evidence_source"))
        audit_line = (
            f'<div class="kv">审计行 id <code>{_e(dd["audit_row_id"])}</code>'
            f' —— 这条拒绝 <b>Cobo 记进了审计日志</b></div>'
            if dd.get("audit_row_id")
            else '<div class="kv warn">该路径 Cobo 审计日志不记录此拒绝</div>'
        )
        default_deny_block = (
            f'<div class="card"><div class="head">付给白名单外地址 '
            f'<code>{_e(dd.get("requested_address"))}</code>'
            f'</div>'
            f'<div class="no">⛔ 被默认拒绝</div>'
            f'<div class="kv">拒绝码 <code>{_e(dd.get("code"))}</code></div>'
            f'<div class="kv">触发原因 <code>{_e(dd.get("policy_reason"))}</code>'
            f' —— 字面意思：<b>没有任何允许策略匹配</b>（fail-closed 生效）</div>'
            f'<div class="kv">命中策略 <code>{_e(dd.get("policy_id"))}</code>'
            f'（为 null 是对的：本来就没有哪条策略被命中）</div>'
            f'<div class="kv">链上交易 <b>无</b> —— 签名前就被拦下</div>'
            f'<div class="kv">证据来源：{_e(src)}</div>'
            f"{audit_line}</div>"
        )
    else:
        default_deny_block = '<div class="card">本次运行没有做默认拒绝检查。</div>'

    audit_rows = "".join(
        f'<tr><td>{_e(r["id"])}</td><td><code>{_e(r["action"])}</code></td>'
        f'<td class="{"ok" if r["result"] == "allowed" else "no"}">{_e(r["result"])}</td>'
        f'<td><code>{_e(r["reason"])}</code></td><td><code>{_e(r["policy_id"])}</code></td>'
        f'<td>{_e(r["stage"])}</td><td>{_e(r["created_at"])}</td></tr>'
        for r in view["audit_decisions"]
    )

    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>PactPay 证据链</title>
<style>
 body{{font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:28px;
      background:#0f1115;color:#e6e9ef}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:26px 0 10px;color:#9aa4b2}}
 .sub{{color:#9aa4b2;font-size:12px;margin-bottom:18px}}
 .card{{background:#171a21;border:1px solid #242a35;border-radius:8px;padding:14px;margin:10px 0}}
 .head{{font-weight:600;margin-bottom:8px}}
 .kv{{color:#c3cad6;font-size:13px;margin:3px 0;word-break:break-all}}
 .ok{{color:#4ade80;font-weight:600}} .no{{color:#f87171;font-weight:600}}
 .warn{{color:#fbbf24}}
 code{{background:#0b0d11;padding:1px 5px;border-radius:4px;font-size:12px}}
 table{{border-collapse:collapse;width:100%;font-size:12px}}
 th,td{{border-bottom:1px solid #242a35;padding:6px 8px;text-align:left}}
 th{{color:#9aa4b2;font-weight:600}}
 .summary{{display:flex;gap:22px;margin:14px 0}}
 .summary div{{background:#171a21;border:1px solid #242a35;border-radius:8px;padding:10px 16px}}
 .empty{{color:#9aa4b2;padding:40px;text-align:center}}
</style></head><body>
<h1>PactPay 证据链</h1>
<div class="sub">run {_e(view["run_id"])} · {_e(view["started_at"])}</div>

<div class="summary">
  <div>任务<br><b>{_e(view["task"])}</b></div>
  <div>总预算<br><b>{_e(view["budget_usd"])} USDC</b></div>
  <div>Pact<br><code>{_e(view["pact_id"])}</code><br><b>{_e(view["pact_status"])}</b></div>
  <div>结果<br><span class="ok">成功 {view["summary"]["paid"]}</span> ·
       <span class="no">被拒 {view["summary"]["denied"]}</span>{failed_summary}</div>
</div>

<h2>每一笔采购</h2>
{"".join(rows)}

<h2>默认拒绝检查（R6：没被任何策略覆盖的操作必须自动被拒）</h2>
{default_deny_block}

<h2>审计日志（仅策略决策，已滤除读操作噪声）</h2>
<div class="sub">注意：<code>transfer.initiate / allowed</code> 记录的是<b>前置权限检查通过</b>（阶段 <code>permission_check</code>），Cobo 对<b>随后被策略拒绝</b>的操作也会写这一行——所以它的条数<b>大于</b>实际成功的笔数。真正的结果看上面的卡片。</div>
<table><tr><th>id</th><th>动作</th><th>结果</th><th>原因</th><th>策略 id</th><th>阶段</th><th>时间</th></tr>
{audit_rows}</table>
</body></html>"""


EMPTY_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>PactPay 证据链</title></head><body style="font-family:sans-serif;background:#0f1115;color:#e6e9ef">
<div class="empty" style="padding:60px;text-align:center">
<h1>还没有证据</h1>
<p>先跑一次演示：<code>make demo</code></p>
</div></body></html>"""


def _render_broken(reason: str) -> str:
    """证据读不动时渲染的页面。

    **刻意不显示任何"通过/失败"结论** —— 读不动就是读不动，
    不许退化成"看起来像没有证据"或"看起来像全绿"。
    """
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>PactPay 证据（读不动）</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; margin: 3rem auto; max-width: 44rem; line-height: 1.65; }}
 .box {{ border: 2px solid #b45309; background: #fffbeb; padding: 1.2rem 1.4rem; border-radius: 10px; }}
 code {{ background: #f3f4f6; padding: .15rem .4rem; border-radius: 4px; word-break: break-all; }}
</style></head><body>
<h1>证据读不动</h1>
<div class="box">
  <p><strong>最新一次运行的证据文件不完整，无法展示。</strong></p>
  <p><code>{html.escape(reason)}</code></p>
  <p>最可能的原因：进程在写 <code>run.json</code> 时被杀（写入不是原子的），
     或 <code>chain.jsonl</code> 的最后一行被截断。</p>
  <p><strong>这不代表"边界生效"，也不代表"边界失效"——它代表这次运行的证据不可用。</strong>
     请重跑 <code>make demo</code>。</p>
</div>
</body></html>"""


def create_app(evidence_dir: Path = EVIDENCE_DIR) -> FastAPI:
    app = FastAPI(title="PactPay dashboard", version="1.0.0")

    def _current() -> tuple[dict[str, Any] | None, str | None]:
        """返回 `(证据, 读不动的原因)`。

        **看板不许 500**（评审第 3 轮 #5）：一份被截断的证据文件应该被**如实展示成
        "读不动"**，而不是让页面炸掉。500 页面既没说清发生了什么，
        也让人以为是服务挂了 —— 而真正的事实是"这份证据不完整"。
        """
        run_dir = latest_run_dir(evidence_dir)
        if run_dir is None:
            return None, None
        try:
            return load_run(run_dir), None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ChainCorruptError) as error:
            return None, f"{run_dir.name}：{type(error).__name__}: {error}"

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        evidence, broken = _current()
        if broken:
            return _render_broken(broken)
        return _render(build_view_model(evidence)) if evidence else EMPTY_PAGE

    @app.get("/api/evidence")
    async def api_evidence() -> dict[str, Any]:
        evidence, broken = _current()
        if broken:
            raise HTTPException(status_code=422, detail=f"证据读不动：{broken}")
        if evidence is None:
            raise HTTPException(status_code=404, detail="还没有证据，先跑 `make demo`")
        return build_view_model(evidence)

    return app


app = create_app()


def _latest_run_cli() -> int:
    """`python -m pactpay.dashboard.app --latest-run`：打印最近一次**完整**运行的目录。

    存在的理由（评审第 4 轮 F8）：`make verify` 与看板必须对"最新一次运行"
    有**同一个**理解。原先 Makefile 自己写了一句 `[ -f run.json ]`，
    只看文件在不在、不看它能不能解析 —— 而 `is_complete_run` 要求能解析。
    两个入口因此会挑到**不同的目录**：一个报 ✓、另一个渲染另一份证据。

    **判据只有一份**：这里直接调 `latest_run_dir()`，也就是看板用的那个函数。
    """
    run_dir = latest_run_dir()
    if run_dir is None:
        return 1
    print(str(run_dir).rstrip("/") + "/")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    import sys

    if "--latest-run" in sys.argv:
        raise SystemExit(_latest_run_cli())
    raise SystemExit("用法：python -m pactpay.dashboard.app --latest-run（或用 make dashboard 起服务）")
