"""用 LLM 当 Agent 的"大脑"：让模型自己决定买哪几个接口。

这是 `planner.py` 里那个可替换 `Planner` 接口的一个真实实现（R13）。
它和 `default_planner` 的**职责完全一样**——给任务和预算，返回要买哪几笔——
但决策来自模型，而不是写死的两条。

## 这个模块最要紧的一条设计

**planner 只知道"预算"，不知道"单笔上限"。**

`llm_planner(task, budget_usd=0.1)` 拿到的是任务总预算。它看到 `/api/premium`
卖 0.05、总预算 0.1，于是**合理地**判断"要出深度结论就得买它"——
从预算角度看这个决定完全站得住（0.005 + 0.05 = 0.055 < 0.1）。

而人类在 Pact 里写的**单笔上限 0.01** 是模型看不见的。于是：

    Agent 想买 → 平台在签名前拒绝 → Agent 拿到结构化拒绝理由

这才是这个演示真正要证明的那一幕：**被拒的不是一个按剧本走的程序，
而是一个自己算出"这笔该买"的 Agent，撞上了人类划的硬边界。**

反过来说：如果把单笔上限告诉模型，它就会自己绕开那笔不买，
"边界生效"这一幕永远不会发生——演示也就失去了意义。所以**不要**把它写进 prompt。

## 模块边界

- 不碰钱、不碰钱包、不做签名：那是 `agent.py` + `cobo.py` 的事
- 不判断"能不能买"：那是 Pact 策略层的事。这里只负责"想买什么"
- 只读 `API_CATALOG` 里列出的接口；模型编出来的路径一律拒绝
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import httpx

from .planner import Purchase

#: DeepSeek 的 chat completions 端点。可用 `DEEPSEEK_BASE_URL` 覆盖。
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

#: 密钥默认从这些位置找（`DEEPSEEK_API_KEY` 环境变量优先）。
DEFAULT_KEY_PATHS = (
    Path.home() / "Downloads" / ".deepseek-api-key",
    Path.home() / ".deepseek-api-key",
)

#: 请求超时。规划是一次短对话，不需要长连接。
TIMEOUT_S = 60.0


class LLMError(RuntimeError):
    """模型调用或输出解析失败。

    **故意不吞掉**：规划不出来就必须报错停下来，绝不能"模型坏了就退回两条硬编码"——
    那样演示会在没人察觉的情况下退化成确定性版本，而报告里还写着"由 LLM 决策"。
    """


#: Agent 可采购的接口清单。**这是 Agent 能看见的全部世界。**
#: 价格是整币十进制（与 x402 的 accepts[].price 一致），不是原子单位。
API_CATALOG: tuple[dict[str, str], ...] = (
    {
        "resource": "/api/cheap",
        "price_usd": "0.005",
        "description": "原始运营数据（records=42 的原始报表）",
    },
    {
        "resource": "/api/premium",
        "price_usd": "0.05",
        "description": "深度分析结论（7 条洞察，带结论性判断）",
    },
)

_CATALOG_BY_RESOURCE = {item["resource"]: item for item in API_CATALOG}


def catalog_for_prompt() -> str:
    """把接口清单渲染成 prompt 里的一段。价格明确写出来，好让模型能算预算。"""
    lines = [f"- {item['resource']}  价格 {item['price_usd']} USDC  —— {item['description']}"
             for item in API_CATALOG]
    return "\n".join(lines)


def build_prompt(task: str, budget_usd: Decimal) -> str:
    """构造规划 prompt。

    **刻意不包含单笔上限。** 见模块 docstring：让模型自己算出"该买 premium"，
    它才会真的去尝试，边界被触发的那一幕才是真的。
    """
    return f"""你是一个采购 Agent。你的任务：

{task}

你可以购买的接口：
{catalog_for_prompt()}

你的总预算是 {budget_usd} USDC。

请挑出完成这个任务需要购买的接口，按购买顺序排列。
只输出 JSON，不要任何解释文字、不要 markdown 代码块。格式：

{{"purchases": [{{"resource": "/api/xxx", "reason": "为什么需要它"}}]}}

要求：
- 只能从上面列出的接口里挑，resource 必须原样照抄
- 总花费不得超过 {budget_usd} USDC
- 至少挑一个
- 想要更好的结果，就买能力更强的接口——只要预算允许
"""


def parse_plan_json(raw: str, budget_usd: Decimal) -> tuple[Purchase, ...]:
    """把模型的输出解析成采购计划。**每一条都严格校验**，任何不合规都报错。

    净容忍：模型有时会用 markdown 代码块包住 JSON，这种情况扒掉围栏再解析。
    其余一律不猜——不猜路径、不猜价格、不猜金额。
    """
    text = raw.strip()
    if text.startswith("```"):
        # 扒掉 ```json ... ``` 围栏
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise LLMError(f"模型输出不是合法 JSON：{error}；原文前 300 字：{raw[:300]!r}") from error

    if not isinstance(payload, dict):
        raise LLMError(f"模型输出顶层不是对象，而是 {type(payload).__name__}")

    items = payload.get("purchases")
    if not isinstance(items, list) or not items:
        raise LLMError(f"模型没有给出非空的 purchases 列表：{payload!r}")

    purchases: list[Purchase] = []
    total = Decimal("0")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise LLMError(f"第 {index} 条不是对象：{item!r}")
        resource = item.get("resource")
        if not isinstance(resource, str) or resource not in _CATALOG_BY_RESOURCE:
            raise LLMError(
                f"第 {index} 条挑了清单外的接口 {resource!r}；"
                f"清单里只有 {sorted(_CATALOG_BY_RESOURCE)}"
            )
        catalog_item = _CATALOG_BY_RESOURCE[resource]
        # **价格由我们这边查表决定，不采信模型报的数**——否则模型可以自称某接口很便宜。
        price = Decimal(catalog_item["price_usd"])
        reason = item.get("reason")
        description = catalog_item["description"]
        if isinstance(reason, str) and reason.strip():
            description = f"{description}（Agent 的理由：{reason.strip()}）"
        purchases.append(Purchase(resource=resource, amount_usd=price, description=description))
        total += price

    if total > budget_usd:
        raise LLMError(
            f"模型的计划超预算：合计 {total} > 预算 {budget_usd}。"
            "planner 有责任在自己这一层就守住预算，不能指望后面有人拦。"
        )

    return tuple(purchases)


#: 任务要交付的东西。**这不是演示需要，是任务本身的验收条件**——
#: 一份"数据分析报告"如果只有原始数据没有结论（或反过来），任务就没完成。
#: Agent 必须自己核对计划是否覆盖了它们，这是它作为 Agent 的本分。
DELIVERABLES: tuple[tuple[str, str], ...] = (
    ("原始运营数据", "/api/cheap"),
    ("深度分析结论", "/api/premium"),
)


def missing_deliverables(
    purchases: tuple[Purchase, ...], deliverables: tuple[tuple[str, str], ...] = DELIVERABLES
) -> tuple[str, ...]:
    """计划里没覆盖到的交付要点。空元组表示计划是完整的。"""
    picked = {p.resource for p in purchases}
    return tuple(label for label, resource in deliverables if resource not in picked)


def build_replan_prompt(task: str, budget_usd: Decimal, missing: tuple[str, ...]) -> str:
    """带缺口反馈的重规划 prompt。

    反馈只说**任务本身缺了什么**，不提任何与边界/上限有关的事——
    否则就成了在引导模型避开那笔超限采购，演示就假了。
    """
    gaps = "、".join(missing)
    return (
        build_prompt(task, budget_usd)
        + f"\n\n【上一次尝试不合格】你的计划缺少这些必需内容：{gaps}。"
        "请重新给出一份完整的计划（仍然只输出 JSON）。\n"
    )


def _read_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    for path in DEFAULT_KEY_PATHS:
        try:
            candidate = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if candidate:
            return candidate
    raise LLMError(
        "找不到 DeepSeek 密钥：设置 DEEPSEEK_API_KEY，"
        f"或放到 {' / '.join(str(p) for p in DEFAULT_KEY_PATHS)}"
    )


def call_deepseek(
    prompt: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    post: Callable[..., Any] | None = None,
) -> str:
    """调一次 DeepSeek，返回助手消息的文本内容。

    `post` 是注入口子（测试用），签名同 `httpx.post`。
    """
    key = api_key or _read_api_key()
    url = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    body = {
        "model": model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        # 要求只输出 JSON，省掉一轮"再帮我转成 JSON"的折腾
        "response_format": {"type": "json_object"},
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
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise LLMError(f"DeepSeek 响应形状不对：{error}；原文前 300 字：{response.text[:300]}") from error


def llm_planner(
    task: str,
    budget_usd: Decimal,
    *,
    client: Callable[[str], str] | None = None,
) -> tuple[Purchase, ...]:
    """LLM 决策：让模型自己挑要买哪几个接口。

    `client` 是注入口子（测试用）：给一个 prompt、返回模型文本。默认走 DeepSeek。
    签名与 `default_planner` 一致，所以 `make_plan(task, budget, llm_planner)` 直接可用。
    """
    def ask(text: str) -> str:
        return client(text) if client is not None else call_deepseek(text)

    purchases = parse_plan_json(ask(build_prompt(task, budget_usd)), budget_usd)

    # **Agent 自己要核对计划是否满足任务**，这是它作为 Agent 的本分。
    # 一次规划就可能漏掉要点（实测：模型某次只挑了 premium，理由是"它比原始数据
    # 更符合任务目标"——推理没错，但交不出完整报告）。漏了就带着缺口重规划一次。
    missing = missing_deliverables(purchases)
    if missing:
        purchases = parse_plan_json(
            ask(build_replan_prompt(task, budget_usd, missing)), budget_usd
        )
        still = missing_deliverables(purchases)
        if still:
            raise LLMError(
                f"重规划后仍然缺少必需内容：{'、'.join(still)}。"
                "宁可报错停下，也不要交一份不完整的报告。"
            )

    return purchases
