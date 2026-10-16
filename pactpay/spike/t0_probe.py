"""T0 探针：把"能不能真跑"这件事先打掉。

对应方案里的 A0a–A0d、A0g（部分）。这一段**零资金**即可完成：
实测确认策略引擎的评估顺序是 权限 → 策略 → 余额，所以钱包里没有币，
超限那笔照样会被策略拒掉——这样在用户去 faucet 领币之前就能先证明"硬边界真的生效"。

跑法：
    .venv/bin/python -m pactpay.spike.t0_probe

退出码 0 = 全部断言通过；非 0 = 有断言没过（会打印是哪条）。
证据（含逐字请求体与响应体）写进 progress/t0-evidence.json（progress/ 已 gitignore）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pactpay.platform.cobo import CoboClient, CoboError, PolicyDenied
from pactpay.platform.config import CHAIN_ID, TOKEN_ID, load_existing, save_env

# 探针专用 Pact 的已知上限。取 0.001 是为了让"超限"这件事有一个明确的、可复核的边界。
PROBE_CAP = "0.001"
# 超限金额：0.005 > 0.001
OVER_LIMIT = "0.005"
# 限内金额：0.0001 <= 0.001
WITHIN_LIMIT = "0.0001"
# 探针目标地址（只为占位；when 里不限定目标地址，所以任何地址都行）
PROBE_DST = "0x1111111111111111111111111111111111111111"

EVIDENCE_PATH = Path("progress/t0-evidence.json")


def verify_wallet_access(client: CoboClient, wallet_uuid: str) -> tuple[bool, str]:
    """确认这把 key **真的能读这个钱包**，再把它写进 .env。

    为什么非要有这一步（踩过的坑）：`provision_agent` 返回的凭证，其钱包可见范围
    并不总是"我随后建的那个钱包"。曾经出现过：.env 里的 key 能列钱包、但**看不到**
    自己那次建的钱包（`GET /wallets/{uuid}` 直接 403
    "API key pact authorization is not authorized for this wallet"），
    而余额接口 / 审计接口也一并 403。如果不在这里挡住，症状会拖到"用户已经往那个地址
    打完币"之后才暴露——那时币就成了孤儿。
    """
    try:
        client.request("GET", f"/api/v1/wallets/{wallet_uuid}")
    except CoboError as exc:
        return False, f"读钱包失败 HTTP {exc.status}: {json.dumps(exc.body, ensure_ascii=False)[:200]}"
    try:
        client._get("/api/v1/wallets/balances", {"wallet_uuid": wallet_uuid})
    except CoboError as exc:
        return False, f"读余额失败 HTTP {exc.status}: {json.dumps(exc.body, ensure_ascii=False)[:200]}"
    return True, "读钱包 + 读余额都通过"


class Check:
    """一条断言。全部收集完再统一判定，这样一次能看全所有结果。"""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def that(self, name: str, condition: bool, detail: str = "") -> bool:
        self.results.append((name, bool(condition), detail))
        mark = "PASS" if condition else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
        return bool(condition)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.results)


def build_probe_pact_spec() -> dict:
    """探针 Pact：只放一条 transfer 规则，上限是已知值 PROBE_CAP。

    故意**只放 rolling/per-tx 中的 per-tx 一条**，且 when 不限定目标地址，
    这样"超限被拒"只可能来自这一条规则、不可能是别的机制凑出来的。
    """
    return {
        "execution_plan": "T0 可行性探针：验证策略引擎真的会在签名前拦下超限转账。",
        "policies": [
            {
                "name": "t0-probe-transfer-cap",
                "type": "transfer",
                "rules": {
                    "effect": "allow",
                    "when": {
                        "chain_in": [CHAIN_ID],
                        "token_in": [{"chain_id": CHAIN_ID, "token_id": TOKEN_ID}],
                    },
                    "deny_if": {"amount_gt": PROBE_CAP},
                },
            }
        ],
        "completion_conditions": [{"type": "time_elapsed", "threshold": "3600"}],
    }


def main() -> int:
    check = Check()
    evidence: dict = {}

    with CoboClient() as client:
        # ---- A0a：匿名拿凭证 -> 建钱包 -> 建地址 ----
        print("\n== A0a：凭证 / 钱包 / 地址 ==")
        saved = load_existing()
        reuse_key = os.environ.get("AGENT_WALLET_API_KEY") or saved.get("AGENT_WALLET_API_KEY", "")
        reuse_wallet = os.environ.get("AGENT_WALLET_WALLET_ID") or saved.get("AGENT_WALLET_WALLET_ID", "")
        reuse_addr = os.environ.get("AGENT_WALLET_ADDRESS") or saved.get("AGENT_WALLET_ADDRESS", "")

        api_key = agent_id = wallet_uuid = address = ""

        if reuse_key and reuse_wallet and reuse_addr:
            client.api_key = reuse_key
            ok, why = verify_wallet_access(client, reuse_wallet)
            if ok:
                api_key, wallet_uuid, address = reuse_key, reuse_wallet, reuse_addr
                print(f"  复用 .env 里已有的钱包（地址不变），wallet_uuid={wallet_uuid}")
                check.that("A0a.0 复用已有钱包凭证（地址不会变）", True, f"address={address}")
            else:
                print(f"  已有凭证读不到该钱包（{why}）")
                print("  ⇒ 这份凭证已失效/作用域不对，**重新开户**（旧地址请勿再打币）")
                reuse_key = ""

        if not api_key:
            try:
                prov = client.provision_agent(name="pactpay-t0-probe")
            except CoboError as exc:
                print(f"  provision 失败：{exc}")
                return 1
            result = prov.get("result", {})
            api_key = result.get("api_key", "")
            agent_id = result.get("agent_id", "")
            check.that("A0a.1 匿名 provision 返回 active 凭证", result.get("status") == "active", f"agent_id={agent_id}")
            check.that("A0a.2 api_key 非空", bool(api_key))

            client.api_key = api_key  # 后续调用都带这把 key

            try:
                wallet_resp = client.create_wallet(name="pactpay-t0-wallet")
            except CoboError as exc:
                print(f"  建钱包失败：{exc}")
                return 1
            wallet = wallet_resp.get("result", {})
            # 字段名是 uuid（不是 wallet_uuid / id），照 WalletRead schema
            wallet_uuid = wallet.get("uuid", "")
            print(f"  钱包响应：{json.dumps(wallet, ensure_ascii=False)[:300]}")
            check.that("A0a.3 Custodial 钱包创建成功且为 active", wallet.get("status") == "active", f"uuid={wallet_uuid}")
            check.that("A0a.4 wallet_uuid 非空", bool(wallet_uuid))

            addr_resp = client.create_address(wallet_uuid, CHAIN_ID)
            addr = addr_resp.get("result", {})
            address = addr.get("address", "")
            check.that("A0a.5 Base Sepolia 地址创建成功", address.startswith("0x"), f"address={address}")

            # **先确认这把 key 真能读这个钱包，再落盘**——否则用户往一个读不到的地址打币。
            ok, why = verify_wallet_access(client, wallet_uuid)
            check.that("A0a.5b 该 key 能读这个钱包（读钱包 + 读余额都通）", ok, why)
            if not ok:
                print("\n  ⇒ 凭证与钱包不匹配，**不落盘**（否则会把币打到取不回的地址）。")
                return 1

            if api_key and wallet_uuid and address:
                save_env(
                    {
                        "AGENT_WALLET_API_URL": client.base_url,
                        "AGENT_WALLET_API_KEY": api_key,
                        "AGENT_WALLET_WALLET_ID": wallet_uuid,
                        "AGENT_WALLET_ADDRESS": address,
                    }
                )

        # ---- 商户钱包：收款方要用**另一个**地址 ----
        # 为什么要单独一个：如果收款地址就是 agent 自己的钱包，那就是"自己付给自己"，
        # 余额不会变——而 A5c 要断言"被拒那笔余额不变"，前提是"成功那笔余额会变"。
        # 所以必须有一个真正的第三方收款地址。
        #
        # 实测踩到的坑：用 agent 那把 key 去给**新钱包**建地址会被拒：
        #   HTTP 403 {"error": "API key pact authorization is not authorized for this wallet"}
        # ⇒ API key 的授权范围绑在它自己那套钱包上。所以商户必须**另起一个 agent**
        #    （这也更符合真实模型：卖家是另一个主体，有自己的身份）。
        print("\n== 商户（收款方）钱包 ==")
        merchant_uuid = os.environ.get("MERCHANT_WALLET_ID") or saved.get("MERCHANT_WALLET_ID", "")
        merchant_addr = os.environ.get("PAY_TO_ADDRESS") or saved.get("PAY_TO_ADDRESS", "")
        if merchant_uuid and merchant_addr:
            print(f"  复用已有商户地址：{merchant_addr}")
            check.that("A0a.6 商户收款地址已就绪（且与 agent 钱包不同）", merchant_addr != address, f"{merchant_addr}")
        else:
            merchant_prov = client.provision_agent(name="pactpay-merchant").get("result", {})
            merchant_key = merchant_prov.get("api_key", "")
            check.that("A0a.6 商户另起一个 agent（key 的授权范围绑在自己的钱包上）", bool(merchant_key))
            client.api_key = merchant_key
            mw = client.create_wallet(name="pactpay-merchant")["result"]
            merchant_uuid = mw["uuid"]
            merchant_addr = client.create_address(merchant_uuid, CHAIN_ID)["result"]["address"]
            print(f"  新建商户钱包：{merchant_uuid} / {merchant_addr}")
            check.that(
                "A0a.7 商户收款地址与 agent 钱包不同",
                merchant_addr.startswith("0x") and merchant_addr != address,
                f"{merchant_addr}",
            )
            save_env(
                {
                    "MERCHANT_WALLET_ID": merchant_uuid,
                    "MERCHANT_API_KEY": merchant_key,
                    "PAY_TO_ADDRESS": merchant_addr,
                }
            )
            client.api_key = api_key  # 切回 agent 的 key，后面的探针要用它

        # ---- A0d：faucet 里到底有什么 ----
        print("\n== A0d：faucet 可用代币 ==")
        faucet = client.list_faucet_tokens()
        chains = faucet.get("result", []) or []
        faucet_ids = sorted(
            tok for entry in chains for tok in (entry.get("tokens") or {}).keys()
        )
        print(f"  faucet token ids = {faucet_ids}")
        check.that(
            "A0d.1 Cobo faucet 确认不供 Base Sepolia（记录在案，改用外部 faucet）",
            not any("BASE" in t.upper() for t in faucet_ids),
            f"实际提供：{faucet_ids}",
        )

        # ---- 确认 Base Sepolia USDC 的 token_id ----
        print("\n== Base Sepolia USDC token_id 核验 ==")
        # 用 search 端点（文档说拿它返回的 token_id 去转账/查余额）
        hits = ((client.search_tokens("USDC") or {}).get("result") or [])
        if isinstance(hits, dict):
            hits = hits.get("data") or []
        found = next(
            (h for h in hits if isinstance(h, dict) and h.get("token_id") == TOKEN_ID),
            None,
        )
        check.that(f"token_id={TOKEN_ID} 存在", found is not None, f"USDC 命中 {len(hits)} 条")
        if found:
            print(f"  chain_id={found.get('chain_id')} symbol={found.get('symbol')} addr={found.get('token_address')}")
            check.that("它确实是 Base Sepolia 上的 USDC", found.get("chain_id") == CHAIN_ID, str(found.get("symbol")))
        evidence["token"] = found

        # ---- 探针 Pact ----
        print("\n== 建探针 Pact（独立于 demo，专用）==")
        pact_resp = client.submit_pact(
            wallet_id=wallet_uuid,
            intent="T0 可行性探针：确认策略引擎会在签名前拦下超限转账",
            name="pactpay-t0-probe-cap",
            spec=build_probe_pact_spec(),
        )
        pact = pact_resp.get("result", {})
        pact_id = pact.get("pact_id", "")
        check.that("A0b.0 探针 Pact 提交后自动 active（agent-first 自动审批）", pact.get("status") == "active", f"pact_id={pact_id}")

        detail = client.get_pact(pact_id).get("result", {})
        pact_key = detail.get("api_key", "")
        check.that("A0b.1 取到 pact-scoped key（不是根 key）", bool(pact_key), f"key_prefix={str(pact_key)[:12]}…")

        # ---- A0b：必然超限的那一笔必须被拒 ----
        print("\n== A0b：超限探针（零资金也应被策略拒掉）==")
        denied: PolicyDenied | None = None
        try:
            client.transfer(
                wallet_uuid,
                {
                    "chain_id": CHAIN_ID,
                    "token_id": TOKEN_ID,
                    "src_addr": address,
                    "dst_addr": PROBE_DST,
                    "amount": OVER_LIMIT,
                },
                api_key=pact_key,
            )
        except PolicyDenied as exc:
            denied = exc

        if denied is None:
            check.that("A0b.2 超限转账被拒", False, "没有拿到 403——R-1 未通过，按方案应停下找用户")
        else:
            print(f"  逐字响应：{json.dumps(denied.raw, ensure_ascii=False)}")
            check.that("A0b.2 超限转账被拒（403）", denied.status == 403, f"status={denied.status}")
            check.that(
                "A0b.3 拒绝码是 TRANSFER_LIMIT_EXCEEDED",
                denied.code == "TRANSFER_LIMIT_EXCEEDED",
                f"code={denied.code} reason={denied.reason}",
            )
            check.that("A0b.4 details 里带 policy_id（R5 要的'触发了哪条策略'）", bool(denied.details.get("policy_id")), str(denied.details))

        # ---- A0c：口径与字段集合实测 ----
        print("\n== A0c：details 的完整 key 集合 ==")
        if denied is not None:
            keys = sorted(denied.details.keys())
            print(f"  details keys = {keys}")
            check.that(
                "A0c.1 已如实记录 limit_value/requested_amount 是否存在",
                True,
                f"存在={[k for k in ('limit_value', 'requested_amount') if k in denied.details]}",
            )

        # ---- 限内那笔：应过策略、改报余额不足（证明拒绝来自策略而非余额）----
        print("\n== 对照：限内那笔应过策略、改报余额不足 ==")
        within = client.transfer(
            wallet_uuid,
            {
                "chain_id": CHAIN_ID,
                "token_id": TOKEN_ID,
                "src_addr": address,
                "dst_addr": PROBE_DST,
                "amount": WITHIN_LIMIT,
            },
            api_key=pact_key,
            expect_error=True,
        )
        w_code = ((within or {}).get("error") or {}).get("code", "")
        print(f"  限内那笔 -> {json.dumps(within, ensure_ascii=False)[:300]}")
        check.that(
            "A0c.2 限内那笔不是被策略拒的（说明顺序是 策略 -> 余额）",
            w_code != "TRANSFER_LIMIT_EXCEEDED",
            f"code={w_code}",
        )

        # ---- 审计日志 ----
        # **实测到的可见性规则**（这条很反直觉，必须按它查）：
        #   `list_audit_logs` 默认只返回**当前鉴权主体自己做的**动作。
        #   pact-scoped key 发起的 transfer，根 key **不带过滤**是查不到的
        #   （实测：根 key 不带过滤 1 条、`result=denied` **0 条**；用 pact key 才 1 条）。
        #   加上 `wallet_id=` 过滤后，根 key 能看到整个钱包的完整轨迹（实测 3 条，含 transfer.denied）。
        # 所以：**必须带 wallet_id 查**；若该 key 缺 can_view（会 403 required_permission=can_view），
        # 退回用 pact key 查它自己那条。
        print("\n== 审计日志 ==")
        rows: list[dict] = []
        audit_scope = ""
        try:
            audit = client.list_audit_logs(wallet_id=wallet_uuid, result="denied", limit=5)
            rows = ((audit or {}).get("result") or {}).get("items") or []
            audit_scope = "根 key + wallet_id"
        except CoboError as exc:
            print(f"  根 key 带 wallet_id 查被拒（{exc.status}），退回 pact key")
        if not rows:
            # pact key 能看到自己发起的那条
            saved_key, client.api_key = client.api_key, pact_key
            try:
                audit = client.list_audit_logs(result="denied", limit=5)
                rows = ((audit or {}).get("result") or {}).get("items") or []
                audit_scope = "pact key"
            finally:
                client.api_key = saved_key

        check.that("A0c.3 审计日志里能查到 denied 记录", len(rows) > 0, f"条数={len(rows)}（作用域：{audit_scope}）")
        if rows:
            print(f"  首条：{json.dumps(rows[0], ensure_ascii=False)[:500]}")

        # A5a 依赖的"三方一致"：拒绝响应的 policy_id 要能在审计行里找到同一个值。
        # 这里是 T0 就要验掉的，不能拖到 T5 才发现对不上。
        if denied is not None and rows:
            want = denied.details.get("policy_id")
            got = [
                (r.get("authz_details") or {}).get("policy_id")
                for r in rows
                if isinstance(r, dict)
            ]
            check.that(
                "A0c.4 审计行的 authz_details.policy_id 与拒绝响应的 policy_id 一致（A5a 的三方交叉）",
                want in got,
                f"拒绝侧={want} 审计侧={got}",
            )
            actions = sorted({r.get("action") for r in rows if isinstance(r, dict)})
            print(f"  审计 action 取值 = {actions}（A5c ② 要按它过滤）")
            evidence["audit_actions"] = actions

        evidence.update(
            {
                "agent_id": agent_id,
                "api_key": api_key,
                "wallet_uuid": wallet_uuid,
                "address": address,
                "probe_pact_id": pact_id,
                "probe_cap": PROBE_CAP,
                "denial": denied.raw if denied else None,
                "denial_details_keys": sorted(denied.details.keys()) if denied else None,
                "within_limit_response": within,
                "audit_sample": rows[:3],
                "calls": [c.as_evidence() for c in client.calls],
            }
        )

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))

    print("\n" + "=" * 64)
    if check.ok:
        print(f"T0 零资金部分：全部 {len(check.results)} 条断言通过 ✅")
    else:
        failed = [n for n, ok, _ in check.results if not ok]
        print(f"T0 零资金部分：{len(failed)} 条断言未通过 ❌")
        for name in failed:
            print(f"  - {name}")
    print(f"证据已写入 {EVIDENCE_PATH}")
    print(f"钱包地址（下一步去外部 faucet 领 Base Sepolia USDC 要用）：{evidence.get('address')}")
    print("=" * 64)
    return 0 if check.ok else 1


if __name__ == "__main__":
    sys.exit(main())
