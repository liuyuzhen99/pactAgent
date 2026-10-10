"""把 Agent 能干的事做成**工具**，交给模型自己调用。

## 为什么要有这一层

在这之前，"Agent"其实是两段拼起来的：模型吐一份采购计划 JSON，然后一段
**写死的 Python 循环**去执行。模型只参与了一次，之后的事跟它没关系了——
那不是一个 agent，是一个 planner 加一个脚本。

真正的 agent 应该是：**模型每一步自己决定调哪个工具、看结果、再决定下一步。**
超限那笔被拒之后，**它必须自己看见那个拒绝、自己想办法**——
是换个法子、还是就用手上已有的数据交差。这才是"自主"。

于是本模块把已有的能力包成工具（**薄封装，不重写**）：

| 工具 | 包的是什么 | 对应需求 |
|---|---|---|
| `list_catalog` | 可采购的接口清单（本地权威价目表） | R3 |
| `check_balance` | `CoboClient.list_balances` | 花钱前的自知 |
| `purchase` | **`agent.buy`** —— 真正花钱的那一步 | R4/R5 |
| `spending_summary` | 本次会话已花掉多少（Agent 自己记账） | R3 |
| `finish` | Agent 宣布交付 | R2 |

## 一条不能破的规矩

**工具返回的拒绝，必须原样交给模型看见，一个字都不许美化。**

`purchase` 在被策略拒绝时返回的是平台给的结构化拒绝（码、原因、命中的策略 id）。
**绝不能**把它翻译成"这个接口暂时不可用，请换一个"之类的软话——
那样模型就以为是自己挑错了，而不是撞上了人类划的边界，
演示要证明的东西也就没了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from .agent import PurchaseOutcome, buy
from .cobo import CoboClient
from .llm_planner import API_CATALOG
from .planner import Purchase

#: Agent 能看见的接口清单（资源 → 价格/描述）。价格是**本地权威**，
#: 不问模型、也不问收款方——收款方报价和标价不一致时以本地清单为准并报错。
CATALOG = {item["resource"]: item for item in API_CATALOG}


@dataclass
class ToolContext:
    """一次 agent 会话的全部状态。

    Agent **自己**维护花费账（`spent`），这是它"知道自己在花多少钱"的依据。
    注意它记的是**实际结算**的金额：被拒的那笔不记账——钱没动。
    """

    client: CoboClient
    wallet_uuid: str
    pact_api_key: str
    payee_base: str
    agent_address: str
    budget_usd: Decimal

    #: 本次会话真的花掉的钱（只有 paid 才累加）
    spent: Decimal = Decimal("0")
    #: 全部采购结果（含被拒的），供证据与交付
    outcomes: list[PurchaseOutcome] = field(default_factory=list)
    #: 每次工具调用的流水（供看板展示"Agent 当时看到了什么"）
    transcript: list[dict[str, Any]] = field(default_factory=list)
    #: Agent 自己声明的交付物
    report: str | None = None

    def remaining(self) -> Decimal:
        return self.budget_usd - self.spent

    def paid_outcomes(self) -> list[PurchaseOutcome]:
        return [o for o in self.outcomes if o.outcome == "paid"]

    def denied_outcomes(self) -> list[PurchaseOutcome]:
        return [o for o in self.outcomes if o.outcome == "denied"]


class ToolError(Exception):
    """工具用法错误（不是策略拒绝）。

    与"被策略拒绝"**必须分开**：前者是 Agent 用错了工具，后者是它撞上了边界。
    混成一个错误类型，模型就分不清"我做错了"和"人类不许"。
    """


# ── 工具实现 ────────────────────────────────────────────────────────────


def tool_list_catalog(ctx: ToolContext) -> dict[str, Any]:
    """列出能买的接口和价格。"""
    return {
        "apis": [
            {
                "resource": resource,
                "price_usd": item["price_usd"],
                "description": item["description"],
            }
            for resource, item in CATALOG.items()
        ],
        "budget_usd": str(ctx.budget_usd),
    }


def tool_check_balance(ctx: ToolContext) -> dict[str, Any]:
    """查钱包余额（只读）。"""
    from .config import CHAIN_ID, TOKEN_ID
    from .preconditions import find_balance

    balances = ctx.client.list_balances(ctx.wallet_uuid, chain_id=CHAIN_ID, token_id=TOKEN_ID)
    found = find_balance(balances.get("result") or [], CHAIN_ID, TOKEN_ID) or {}
    return {
        "chain_id": CHAIN_ID,
        "token_id": TOKEN_ID,
        "balance_usdc": str(found.get("amount", "")),
        "spent_this_session_usdc": str(ctx.spent),
        "remaining_budget_usdc": str(ctx.remaining()),
    }


def tool_spending_summary(ctx: ToolContext) -> dict[str, Any]:
    """Agent 自己记账：这次会话花掉多少、还剩多少、买成了几笔。"""
    return {
        "budget_usd": str(ctx.budget_usd),
        "spent_usd": str(ctx.spent),
        "remaining_usd": str(ctx.remaining()),
        "paid_count": len(ctx.paid_outcomes()),
        "denied_count": len(ctx.denied_outcomes()),
        "purchased": [
            {
                "resource": o.resource,
                "amount_usd": str(o.amount_usd),
                "description": o.description.split("（Agent 的理由")[0],
            }
            for o in ctx.paid_outcomes()
        ],
    }


def tool_purchase(ctx: ToolContext, resource: Any, reason: Any = "") -> dict[str, Any]:
    """**花钱的那一步。** 真的走 x402 握手、真的让平台签名、真的上链。

    返回什么就原样给模型看什么：
    - 成功 ⇒ 数据 + 结算交易哈希
    - **被策略拒绝 ⇒ 平台给的结构化拒绝，原样转达**
    - 未结算 ⇒ `failed`，并且**明说钱可能已经动了**

    `reason` 是 Agent 说明"为什么买这个"，会写进证据——人要能看见它当时的想法。
    """
    if not isinstance(resource, str) or resource not in CATALOG:
        raise ToolError(
            f"没有这个接口：{resource!r}。可用的有 {sorted(CATALOG)}。"
            "请先调 list_catalog 看清楚能买什么。"
        )

    price = Decimal(CATALOG[resource]["price_usd"])
    if price > ctx.remaining():
        # 这是**Agent 自己**该守住的东西（预算），不是平台的边界。
        # 平台那条边界是单笔上限，Agent 看不见——所以这里只谈预算。
        raise ToolError(
            f"买不起：{resource} 要 {price} USDC，而本次会话只剩 {ctx.remaining()} USDC。"
            "预算已经不够了。"
        )

    purchase = Purchase(
        resource=resource,
        amount_usd=price,
        description=(
            CATALOG[resource]["description"]
            + (f"（Agent 的理由：{reason}）" if isinstance(reason, str) and reason.strip() else "")
        ),
    )
    outcome = buy(
        ctx.client, ctx.payee_base, purchase,
        wallet_uuid=ctx.wallet_uuid, pact_api_key=ctx.pact_api_key,
    )
    ctx.outcomes.append(outcome)

    if outcome.outcome == "paid":
        ctx.spent += outcome.amount_usd
        return {
            "outcome": "paid",
            "resource": resource,
            "amount_usd": str(outcome.amount_usd),
            "settlement_tx": outcome.settlement_tx,
            "data": outcome.data,
            "remaining_budget_usd": str(ctx.remaining()),
        }

    if outcome.outcome == "denied":
        denial = outcome.denial or {}
        # **原样转达平台的拒绝，一个字不美化。**
        return {
            "outcome": "denied",
            "resource": resource,
            "amount_usd": str(outcome.amount_usd),
            "http_status": outcome.http_status,
            "code": denial.get("code"),
            "reason": denial.get("reason"),
            "policy_reason": denial.get("policy_reason"),
            "policy_id": denial.get("policy_id"),
            "settlement_tx": None,
            "chain_trace": "签名前就被拦下，钱根本没动",
            "note": "这是一条**硬性边界**：不是暂时故障，重试同样的请求会得到同样的结果。",
        }

    return {
        "outcome": "failed",
        "resource": resource,
        "http_status": outcome.http_status,
        "reason": (outcome.denial or {}).get("reason"),
        "warning": "没有结算成功。**钱可能已经动了**，需要人工核对钱包余额。",
    }


def tool_finish(ctx: ToolContext, report: Any) -> dict[str, Any]:
    """Agent 宣布交付。"""
    if not isinstance(report, str) or not report.strip():
        raise ToolError("report 必须是一段非空的交付说明。")
    ctx.report = report.strip()
    return {
        "accepted": True,
        "paid_count": len(ctx.paid_outcomes()),
        "denied_count": len(ctx.denied_outcomes()),
        "spent_usd": str(ctx.spent),
    }


# ── 给模型看的 schema（OpenAI tool-calling 格式）─────────────────────────

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "list_catalog",
            "description": "列出你可以购买的接口及价格。开始规划前先看这个。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_balance",
            "description": "查钱包余额，以及本次会话已经花掉多少、还剩多少预算。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spending_summary",
            "description": "看本次会话的采购清单：买成了哪几个、被拒了几个、还剩多少预算。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "purchase",
            "description": (
                "购买一个接口。这会真的花钱（走 x402 付款并上链结算）。"
                "成功会返回数据；如果这次采购不被允许，会返回一条明确的拒绝说明。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource": {"type": "string", "description": "要买的接口路径，例如 /api/cheap"},
                    "reason": {"type": "string", "description": "为什么需要买它（会写进审计证据）"},
                },
                "required": ["resource"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "任务完成时调用，说明你最终交付了什么、用了哪些数据。",
            "parameters": {
                "type": "object",
                "properties": {"report": {"type": "string", "description": "交付说明"}},
                "required": ["report"],
            },
        },
    },
)

#: 工具名 → 实现。**只读工具与花钱工具一目了然**，便于审计。
TOOL_IMPLS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_catalog": tool_list_catalog,
    "check_balance": tool_check_balance,
    "spending_summary": tool_spending_summary,
    "purchase": tool_purchase,
    "finish": tool_finish,
}

#: 会改变外部世界（花钱）的工具。审计时要能一眼看出来。
SPENDING_TOOLS = frozenset({"purchase"})


def dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行一次工具调用，把结果（或错误）变成可 JSON 化的字典。

    **工具报错也返回给模型**（包成 `{"error": ...}`），不抛出去中断循环——
    Agent 用错工具应当由它自己看到并纠正，而不是让整个会话崩掉。
    唯一会中断的是策略拒绝之外的意外（那属于基础设施问题，应当炸出来）。
    """
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return {"error": f"没有名为 {name!r} 的工具。可用：{sorted(TOOL_IMPLS)}"}

    try:
        return impl(ctx, **args)
    except ToolError as error:
        return {"error": str(error)}
    except TypeError as error:
        return {"error": f"{name} 的参数不对：{error}"}


def to_json(payload: dict[str, Any]) -> str:
    """工具结果序列化成给模型看的文本。中文不转义，好读也好审。"""
    return json.dumps(payload, ensure_ascii=False, default=str)
