"""Agent 主循环：**模型自己一步步驱动**，而不是"模型出个计划、脚本去执行"。

## 和旧写法的区别（这是本模块存在的理由）

旧写法：

    plan = llm(task)          # 模型只说一次话
    for p in plan.purchases:  # 之后全是写死的 Python
        buy(p)

模型只参与了一次。超限那笔被拒时，它**不在场**——拒绝是脚本处理的。

本模块：

    while True:
        reply = model(messages, tools=TOOL_SCHEMAS)   # 模型看得到全部历史
        if reply 要求调工具:
            result = dispatch(ctx, name, args)        # 真去干
            messages.append(result)                   # **把真实结果喂回去**
        else:
            break

`purchase` 被策略拒绝时，返回的拒绝原样进了 `messages` ——
**模型下一次推理就看得见"我刚才被拒了，码是 X402_POLICY_DENIED，
命中了 matched_pact_transfer_deny_if"**，然后它自己决定怎么办：
再试一次、换个策略、还是用手上的数据交差。

这才是"自主"。而这个自主是**可审计的**：每一次工具调用与返回都进 `transcript`，
落进证据文件，人能看到它当时看到了什么、于是做了什么决定。

## 步数上限

真 agent 必须有上限，否则一个卡住的循环会一直烧钱。
超了上限**报错停下**，不假装完成。
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any, Callable

import httpx

from .llm_planner import DEFAULT_BASE_URL, DEFAULT_KEY_PATHS, DEFAULT_MODEL, TIMEOUT_S, LLMError, _read_api_key
from .tools import SPENDING_TOOLS, TOOL_SCHEMAS, ToolContext, dispatch, to_json

#: 一次会话最多几步。5 个工具、一次采购任务，20 步绰绰有余；
#: 超过就是模型卡住了，应当停下而不是继续烧钱。
MAX_STEPS = 20


def build_system_prompt(task: str, budget_usd: Decimal) -> str:
    """给 Agent 的职责说明。

    **刻意不包含单笔上限**（`PER_TX_CAP`）——那是人类在 Pact 里划的边界，
    Agent 看不见。它只知道总预算。这样当它决定买一个超过单笔上限的接口时，
    那是它**自己算出来该买**，然后撞上墙；而不是我们安排它去撞。
    """
    return f"""你是一个采购 Agent。你的任务是：

{task}

你有一个加密钱包，里面有钱，你可以调用工具真的去买数据接口。

约束：
- 本次任务总预算 {budget_usd} USDC，不要超
- 钱包不是你自己的：它由一份人类签发的授权凭证（Pact）管着。
  **那份凭证里写了什么规则，你事先看不到。** 你只知道自己的预算。
- 花钱只有 `purchase` 这一个途径，它真的会付款、真的会上链

工作方式：
1. 先 `list_catalog` 看清楚能买什么、各多少钱
2. 想清楚完成这个任务需要哪些数据
3. 需要就 `purchase` 买——它成功会返回数据，不被允许会返回一条明确的说明
4. 拿到足够的材料后，调 `finish` 交付

**如果某次 purchase 被拒绝**：那不是网络故障，是那条硬性边界。重试同样的请求
不会有不同结果。请看清拒绝说明，然后决定是换一条路、还是用手上已有的材料交差——
但不要假装买到了你没有的东西。

现在开始。"""


def _chat(
    messages: list[dict[str, Any]],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """调一次模型（带 tools），返回 assistant 消息。"""
    key = api_key or _read_api_key()
    url = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    body = {
        "model": model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL,
        "messages": messages,
        "tools": list(TOOL_SCHEMAS),
        "temperature": 0,
    }
    sender = post or httpx.post
    try:
        response = sender(
            f"{url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as error:
        raise LLMError(f"调用 DeepSeek 失败：{type(error).__name__}: {error}") from error

    if response.status_code != 200:
        raise LLMError(f"DeepSeek 返回 HTTP {response.status_code}：{response.text[:300]}")

    try:
        return response.json()["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise LLMError(f"DeepSeek 响应形状不对：{error}；原文前 300 字：{response.text[:300]}")


class StepLimitExceeded(RuntimeError):
    """Agent 在 `MAX_STEPS` 步内没有交付。

    **报错而不是静默收尾**：一个没交付的会话不该被当成"完成了"。
    """


def run_agent(
    ctx: ToolContext,
    task: str,
    *,
    max_steps: int = MAX_STEPS,
    chat: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """跑一轮 agent 会话，返回 Agent 的交付说明。

    `chat` 是注入口子（测试用）：签名同 `_chat`。默认真调 DeepSeek。
    测试用一个按脚本回话的假模型，一个字节都不出网。
    """
    caller = chat or _chat
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(task, ctx.budget_usd)},
        {"role": "user", "content": "开始。"},
    ]

    for step in range(1, max_steps + 1):
        reply = caller(messages)
        tool_calls = reply.get("tool_calls") or []

        if not tool_calls:
            # 模型没调工具就回话 —— 要么它以为说完了，要么它在偷懒。
            # 把话喂回去并明确告诉它必须用 finish 才算交付。
            content = (reply.get("content") or "").strip()
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": (
                    "你没有调用任何工具。请继续：需要买就调 purchase，"
                    "任务完成就调 finish 说明你交付了什么。"
                ),
            })
            ctx.transcript.append({"step": step, "kind": "no_tool_call", "content": content})
            continue

        messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": tool_calls})

        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
                result = {"error": f"arguments 不是合法 JSON：{raw_args!r}"}
            else:
                if not isinstance(args, dict):
                    args = {}
                    result = {"error": f"arguments 必须是对象，实际 {type(raw_args).__name__}"}
                else:
                    result = dispatch(ctx, name, args)

            ctx.transcript.append({
                "step": step,
                "kind": "tool_call",
                "tool": name,
                "args": args,
                "result": result,
                "spends_money": name in SPENDING_TOOLS,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or f"call_{step}_{name}",
                "content": to_json(result),
            })

            if name == "finish" and ctx.report:
                return ctx.report

    raise StepLimitExceeded(
        f"Agent 在 {max_steps} 步内没有交付。工具调用流水见 transcript（{len(ctx.transcript)} 条）。"
        "宁可报错，也不把一次没完成的会话当成完成了。"
    )
