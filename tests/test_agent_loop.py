"""Agent 主循环 + 工具层的测试。

**一个字节都不出网**：所有用例都用假的模型（按脚本回话）和假的 Cobo。
真调 DeepSeek、真花钱的那条路只在 `make demo` 里走。

这里最要紧的一组用例是「**模型看见拒绝之后怎么办**」——
那正是这个 agent 从"plan + 脚本"变成"真 agent"的地方。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from pactpay.agent import PurchaseOutcome
from pactpay.agent_loop import StepLimitExceeded, build_system_prompt, run_agent
from pactpay.tools import ToolContext, dispatch, tool_purchase


# ── 替身 ────────────────────────────────────────────────────────────────


class FakeCobo:
    """假的 Cobo 客户端。只提供工具层真正用到的两个方法。"""

    def __init__(self, balance: str = "20") -> None:
        self.balance = balance
        self.payments: list[dict[str, Any]] = []

    def list_balances(self, wallet_uuid: str, *, chain_id: str, token_id: str) -> dict[str, Any]:
        return {"result": [{
            "chain_id": chain_id, "token_id": token_id,
            "amount": self.balance, "address": "0xagent",
        }]}

    def payment(self, *a: Any, **kw: Any) -> dict[str, Any]:  # pragma: no cover - buy 会打桩
        raise AssertionError("测试里不该走到真实 payment")


def make_ctx(budget: str = "0.1", balance: str = "20") -> ToolContext:
    return ToolContext(
        client=FakeCobo(balance),          # type: ignore[arg-type]
        wallet_uuid="w",
        pact_api_key="k",
        payee_base="http://127.0.0.1:8001",
        agent_address="0xagent",
        budget_usd=Decimal(budget),
    )


def stub_buy(monkeypatch, *, outcome: str, data: dict | None = None,
             denial: dict | None = None, tx: str | None = None) -> None:
    """把 `agent.buy` 换成返回固定结果 —— 不碰网络、不花钱。"""
    import pactpay.tools as tools_module

    def fake_buy(client, payee_base, purchase, *, wallet_uuid, pact_api_key):
        return PurchaseOutcome(
            resource=purchase.resource,
            amount_usd=purchase.amount_usd,
            description=purchase.description,
            outcome=outcome,
            http_status=200 if outcome == "paid" else 422,
            data=data,
            settlement_tx=tx,
            denial=denial,
            request_id="req-test",
        )

    monkeypatch.setattr(tools_module, "buy", fake_buy)


def scripted(*replies: dict[str, Any]):
    """假的模型：按脚本依次回话，每回一条出队一条。"""
    queue = list(replies)
    seen: list[list[dict[str, Any]]] = []

    def chat(messages, **_kw):
        seen.append([dict(m) for m in messages])
        if not queue:
            raise AssertionError("模型被调用次数超出脚本")
        return queue.pop(0)

    chat.seen = seen          # type: ignore[attr-defined]
    chat.remaining = lambda: len(queue)   # type: ignore[attr-defined]
    return chat


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c_{name}", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}
    ]}


# ── 工具本身 ────────────────────────────────────────────────────────────


def test_purchase_reports_denial_verbatim(monkeypatch):
    """**被拒时平台给什么就转达什么，一个字不美化。**

    若把它翻译成"这个接口暂时不可用"，模型就会以为是自己挑错了，
    而不是撞上了人类划的边界 —— 演示要证明的东西也就没了。
    """
    stub_buy(monkeypatch, outcome="denied", denial={
        "code": "X402_POLICY_DENIED",
        "reason": "x402_no_matching_payment",
        "policy_reason": "matched_pact_transfer_deny_if",
        "policy_id": "5d89ab33-7d46-44fc-9d88-21dda8f097e4",
    })
    ctx = make_ctx()

    result = tool_purchase(ctx, "/api/premium", "需要深度结论")

    assert result["outcome"] == "denied"
    assert result["code"] == "X402_POLICY_DENIED"
    assert result["policy_reason"] == "matched_pact_transfer_deny_if"
    assert result["policy_id"] == "5d89ab33-7d46-44fc-9d88-21dda8f097e4"
    assert result["settlement_tx"] is None
    assert "重试同样的请求会得到同样的结果" in result["note"]
    assert ctx.spent == Decimal("0"), "被拒的那笔不能记账 —— 钱根本没动"


def test_a_denied_purchase_is_not_counted_as_spending(monkeypatch):
    stub_buy(monkeypatch, outcome="denied", denial={"code": "X"})
    ctx = make_ctx()

    tool_purchase(ctx, "/api/premium")

    assert ctx.spent == Decimal("0")
    assert ctx.remaining() == Decimal("0.1")
    assert len(ctx.denied_outcomes()) == 1


def test_purchase_records_the_data_and_the_tx(monkeypatch):
    stub_buy(monkeypatch, outcome="paid", data={"records": 42}, tx="0xabc")
    ctx = make_ctx()

    result = tool_purchase(ctx, "/api/cheap", "基础素材")

    assert result["outcome"] == "paid"
    assert result["data"] == {"records": 42}
    assert result["settlement_tx"] == "0xabc"
    assert ctx.spent == Decimal("0.005")
    assert "基础素材" in ctx.outcomes[0].description, "Agent 的理由必须进证据"


def test_buying_something_not_in_the_catalog_is_a_usage_error(monkeypatch):
    """清单外的接口 ⇒ 用法错误（`error`），**不是**策略拒绝。"""
    ctx = make_ctx()

    result = dispatch(ctx, "purchase", {"resource": "/api/free-lunch"})

    assert "error" in result
    assert "list_catalog" in result["error"]


def test_over_budget_is_refused_by_the_agent_itself(monkeypatch):
    """预算是 **Agent 自己**该守的（它知道）；单笔上限是平台守的（它不知道）。"""
    stub_buy(monkeypatch, outcome="paid", data={}, tx="0x1")
    ctx = make_ctx(budget="0.01")

    result = dispatch(ctx, "purchase", {"resource": "/api/premium"})

    assert "error" in result and "只剩" in result["error"]


def test_unknown_tool_returns_an_error_to_the_model():
    ctx = make_ctx()

    result = dispatch(ctx, "wire_money", {})

    assert "error" in result and "没有名为" in result["error"]


# ── 主循环 ──────────────────────────────────────────────────────────────


def test_agent_without_finish_is_an_error_not_a_silent_pass():
    """没调 finish 就结束 ⇒ 报错，不把没完成的会话当成完成了。"""
    ctx = make_ctx()
    # 它一直查目录、就是不交付 —— 两步之后必须报错停下，不能当成完成
    chat = scripted(call("list_catalog"), call("list_catalog"))

    with pytest.raises(StepLimitExceeded):
        run_agent(ctx, "任务", max_steps=2, chat=chat)


def test_model_talking_without_tools_is_told_to_use_them():
    """模型不调工具只回话 ⇒ 把话喂回去并要它用工具，然后它照做。"""
    ctx = make_ctx()
    chat = scripted(
        {"role": "assistant", "content": "我觉得买普通的就够了"},
        call("finish", report="交付"),
    )

    report = run_agent(ctx, "任务", chat=chat)

    assert report == "交付"
    second = chat.seen[1]          # type: ignore[attr-defined]
    assert any("没有调用任何工具" in (m.get("content") or "") for m in second)


def test_the_model_sees_the_real_result_of_its_own_purchase(monkeypatch):
    """**这是整个模块存在的理由。**

    模型买了 -> 被拒 -> 它下一次推理必须**看得见那条拒绝**。
    先前的写法里模型只出一次计划，拒绝是脚本处理的，它根本不在场。
    """
    stub_buy(monkeypatch, outcome="denied", denial={
        "code": "X402_POLICY_DENIED", "policy_reason": "matched_pact_transfer_deny_if",
    })
    ctx = make_ctx()
    chat = scripted(
        call("purchase", resource="/api/premium", reason="要深度结论"),
        call("finish", report="只拿到基础数据，交差"),
    )

    run_agent(ctx, "任务", chat=chat)

    second_turn = chat.seen[1]      # type: ignore[attr-defined]
    tool_messages = [m for m in second_turn if m.get("role") == "tool"]
    assert tool_messages, "工具结果必须作为 tool 消息喂回模型"
    payload = json.loads(tool_messages[-1]["content"])
    assert payload["outcome"] == "denied"
    assert payload["policy_reason"] == "matched_pact_transfer_deny_if"


def test_retrying_the_denied_purchase_is_visible_in_the_transcript(monkeypatch):
    """模型被拒后重试 —— 也是被拒。两次都在流水里，人看得到边界是硬的。"""
    stub_buy(monkeypatch, outcome="denied", denial={"code": "X402_POLICY_DENIED"})
    ctx = make_ctx()
    chat = scripted(
        call("purchase", resource="/api/premium"),
        call("purchase", resource="/api/premium"),
        call("finish", report="交差"),
    )

    run_agent(ctx, "任务", chat=chat)

    denials = [t for t in ctx.transcript
               if t["kind"] == "tool_call" and t["result"].get("outcome") == "denied"]
    assert len(denials) == 2, "两次尝试都要留痕"


def test_transcript_marks_which_calls_spend_money(monkeypatch):
    """审计要能一眼看出哪一步花了钱。"""
    stub_buy(monkeypatch, outcome="paid", data={}, tx="0x1")
    ctx = make_ctx()
    chat = scripted(
        call("list_catalog"),
        call("purchase", resource="/api/cheap"),
        call("finish", report="交付"),
    )

    run_agent(ctx, "任务", chat=chat)

    by_tool = {t["tool"]: t["spends_money"] for t in ctx.transcript if t["kind"] == "tool_call"}
    assert by_tool["purchase"] is True
    assert by_tool["list_catalog"] is False


def test_bad_tool_arguments_do_not_crash_the_loop():
    """工具参数不是合法 JSON ⇒ 把错误喂回模型，而不是抛出去中断。"""
    ctx = make_ctx()
    chat = scripted(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "purchase", "arguments": "{坏掉的 JSON"}}
        ]},
        call("finish", report="交付"),
    )

    report = run_agent(ctx, "任务", chat=chat)

    assert report == "交付"
    assert any("不是合法 JSON" in str(t.get("result", {})) for t in ctx.transcript)


# ── prompt 里不许泄漏边界 ───────────────────────────────────────────────


def test_system_prompt_does_not_leak_the_per_transaction_cap():
    """系统提示里**不能**出现单笔上限。

    告诉它，它就会自己绕开那笔不买，"边界生效"这一幕永远不会发生。
    """
    prompt = build_system_prompt("任务", Decimal("0.1"))

    assert "0.01" not in prompt
    assert "单笔" not in prompt
    assert "上限" not in prompt
    assert "0.1" in prompt
    assert "看不到" in prompt, "要明确告诉它边界规则是它看不见的"
