"""A1：提交的 Pact 与**服务端回读**的是否逐字段一致（R1）。

**为什么要比对**：提交接口返回 200 只说明它收下了，不说明它**按我们写的那样存下来了**。
如果服务端把某条字段"归一化"掉（比如把单笔上限 0.01 调大、或丢掉白名单的一项），
agent 的授权范围就与人类设定的**不一样**了——而这一点**在演示里看不出来**：
两笔采购照样一过一拒，页面一片正常。**这是最危险的一类静默失败。**

**为什么是"子集比对"而不是严格相等**：服务端会**补充**字段（实测策略对象里多了
`priority` / `is_active`），要求严格相等会假失败。真正要验的是
"**我们提交的每个字段都原样回读**"。反过来说，服务端**多塞**一条策略也必须报出来
——多出来的授权同样是范围被改了。
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from pactpay.cobo import CoboClient
from pactpay.demo import build_demo_pact_spec
from pactpay.config import load_existing


def _diff(path: str, submitted: Any, readback: Any, out: list[str]) -> None:
    """把"提交的"与"回读的"对齐比较，把差异写进 `out`。

    规则：**提交里有的，回读里必须一模一样**；回读里多出来的字段允许（服务端补充）。
    """
    if isinstance(submitted, dict):
        if not isinstance(readback, dict):
            out.append(f"{path}: 期望对象，回读是 {type(readback).__name__}")
            return
        for key, value in submitted.items():
            if key not in readback:
                out.append(f"{path}.{key}: 提交里有，回读里**丢失**")
                continue
            _diff(f"{path}.{key}", value, readback[key], out)
        return
    if isinstance(submitted, list):
        if not isinstance(readback, list):
            out.append(f"{path}: 期望数组，回读是 {type(readback).__name__}")
            return
        if len(submitted) != len(readback):
            out.append(f"{path}: 条数不同（提交 {len(submitted)}，回读 {len(readback)}）")
            return
        for index, (s_item, r_item) in enumerate(zip(submitted, readback)):
            _diff(f"{path}[{index}]", s_item, r_item, out)
        return
    if submitted != readback:
        out.append(f"{path}: 提交 {submitted!r}，回读 {readback!r}")


def compare_spec(submitted: dict[str, Any], readback: dict[str, Any]) -> dict[str, Any]:
    """比对提交的 PactSpec 与服务端回读的 spec。返回 `{ok, differences}`。"""
    differences: list[str] = []
    _diff("spec", submitted, readback or {}, differences)

    # 反向：服务端**多塞**的策略也要报（多出来的授权同样是范围被改了）
    s_policies = submitted.get("policies") or []
    r_policies = (readback or {}).get("policies") or []
    s_names = [p.get("name") for p in s_policies]
    for policy in r_policies:
        if policy.get("name") not in s_names:
            differences.append(f"spec.policies: 回读里**多出**一条未提交的策略 {policy.get('name')!r}")

    return {"ok": not differences, "differences": differences}


def pact_key_is_scoped(pact_key: str | None, root_key: str | None) -> bool:
    """A1 的第三个断言：执行必须用 **pact-scoped key**，不是根 key。

    用根 key 执行会让策略挂在"默认 Pact"上，而不是我们刚提交的这份——
    那样**限额与白名单根本不是我们设的那套**，而演示看起来完全正常。
    """
    return bool(pact_key) and pact_key != root_key


def main() -> int:
    env = load_existing()
    api_key = env.get("AGENT_WALLET_API_KEY", "")
    wallet_uuid = env.get("AGENT_WALLET_WALLET_ID", "")
    pay_to = env.get("PAY_TO_ADDRESS", "")
    if not (api_key and wallet_uuid and pay_to):
        print("缺少凭证：先跑 `make t0-probe`", file=sys.stderr)
        return 2

    submitted = build_demo_pact_spec(pay_to)
    failed = False
    with CoboClient(api_key=api_key) as client:
        pact = client.submit_pact(
            wallet_id=wallet_uuid,
            intent="A1：验证提交的规则被原样存下来",
            name=f"pact-readback-{uuid.uuid4().hex[:8]}",
            spec=submitted,
        )["result"]
        detail = client.get_pact(pact["pact_id"])["result"]

    verdict = judge_readback(submitted, detail, api_key)
    pact_key = detail.get("api_key")

    print(f"1) 提交 Pact {pact['pact_id']}  status={detail.get('status')}")
    active = detail.get("status") == ACTIVE_STATUS
    print(f"   生效检查：{'✓ status 是 active，规则真的生效了' if active else '✗ status 不是 active —— 规则没生效'}")
    print(f"2) 逐字段比对：{'✓ 一致' if not verdict['differences'] else '✗ 有差异'}")
    for difference in verdict["differences"]:
        print(f"   ✗ {difference}")
    scoped = pact_key_is_scoped(pact_key, api_key)
    print(f"3) pact-scoped key：{'✓ 存在且与根 key 不同' if scoped else '✗ 缺失或与根 key 相同'}")

    verdict_a1 = judge_a1(active=active, readback_ok=verdict["ok"], scoped=scoped)
    for reason in verdict_a1["reasons"]:
        print(f"   ✗ {reason}")
    failed |= not verdict_a1["ok"]

    print()
    print("提交的策略与完成条件（供人核对）：")
    print(json.dumps(submitted, ensure_ascii=False, indent=2))
    print("\n" + ("✗ A1 未通过" if failed else "✓ A1 成立：Pact 已生效，规则被原样存下，且执行用的是 pact-scoped key"))
    return 1 if failed else 0


#: Pact 只有在 `active` 时规则才真正生效。其余状态都意味着"规则不在生效中"：
#: `pending_approval` 还没批、`rejected` 被否、`completed`/`expired`/`revoked`/`withdrawn` 都已结束。
ACTIVE_STATUS = "active"


def judge_a1(*, active: bool, readback_ok: bool, scoped: bool) -> dict:
    """A1 的**结论聚合**（纯函数，便于脱网单测）。

    **为什么把它抽出来**（第 4 轮自查变异）：原先这三条判据在 `main()` 里各写一句
    `failed |= ...`，而 `active` 那一句**根本不存在** —— 它被算出来、被打印成 ✗，
    却不影响结论。变异 `active = True` 时 281 条测试全绿。

    这不是"少写一行"的问题：Pact 不在 `active` 时，限额/白名单**没有生效**，
    于是后面每一句"被拒"都可能只是预算到期，而不是边界起作用 ——
    整个 A1 结论都不成立。**打印一个 ✗ 而退出码是 0，等于没检查。**

    抽成纯函数的另一个原因：上一轮我修这类洞时写的是"读源码找某一行"的**结构断言**
    （`assert "failed |= not active" in src`）。那种测试只钉住**写法**，不钉住**行为**——
    把 `active` 的定义改成 `True`，那一行还在，测试照样绿。这里改成**调函数看返回值**。
    """
    reasons: list[str] = []
    if not active:
        reasons.append(
            f"Pact 不在 {ACTIVE_STATUS} —— 限额/白名单没有生效，本次结论不成立"
        )
    if not readback_ok:
        reasons.append("提交的 spec 与服务端回读不一致（详见上方逐字段差异）")
    if not scoped:
        reasons.append("pact-scoped key 缺失或与根 key 相同——执行用的不是被约束的那把钥匙")
    return {"ok": not reasons, "reasons": reasons}


def judge_readback(submitted: dict, detail: dict, root_api_key: str) -> dict:
    """A1 的判定（纯函数，便于脱网单测）。

    三项都要过：
    1. **`status == "active"`** —— 规则**真的生效了**（B4，第 7 步评审阻断项）。
       只比对 spec 是不够的：spec 可以一模一样地挂在一个 `pending_approval` 的 Pact 上，
       那样"规则存下来了"成立、"规则在生效"不成立 —— 演示会一切正常但边界根本没装上。
       真实触发条件：agent 一旦被配对，Pact 就不再自动 ACTIVE，而是等人在 App 里审批。
    2. **spec 逐字段一致** —— 提交的规则被原样存下来。
    3. **执行用的是 pact-scoped key**，不是根 key。
    """
    failures: list[str] = []

    status = detail.get("status")
    if status != ACTIVE_STATUS:
        failures.append(f"status 不是 {ACTIVE_STATUS!r} 而是 {status!r} —— 规则没有生效")

    verdict = compare_spec(submitted, detail.get("spec") or {})
    failures.extend(verdict["differences"])

    if not pact_key_is_scoped(detail.get("api_key"), root_api_key):
        failures.append("pact-scoped key 缺失或与根 key 相同")

    return {"ok": not failures, "failures": failures, "differences": verdict["differences"]}


if __name__ == "__main__":
    raise SystemExit(main())
