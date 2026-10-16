"""`make revoke-check` 与 `make events` 的验收测试。

**这两个模块的价值全在"判据会不会说谎"上**：

- `revoke_check` 必须在"撤销前后拒绝理由**相同**"时报失败 ——
  因为那说明还是策略在拒，**证明不了撤销生效**。这是最容易写成恒真 ✅ 的地方。
- `event_watch` 必须分清"通道不可用"和"没有事件" ——
  把 404 读成"平台没这个事件"就是**说反了**（平台是文档里有、路由没有）。
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from pactpay.governance import events as event_watch, revoke as revoke_check
from pactpay.platform.cobo import CoboError, PolicyDenied


class FakePactClient:
    """手写替身：模拟"撤销前策略拒 / 撤销后 key 失效"这个状态机。"""

    def __init__(self, *, revoke_actually_works: bool = True, revoke_status: str = "revoked") -> None:
        self.revoked = False
        self.revoke_actually_works = revoke_actually_works
        self.revoke_status = revoke_status
        self.transfers: list[str] = []
        self.revoke_calls: list[str] = []

    def submit_pact(self, wallet_id, spec, *, intent, name):  # noqa: ANN001
        return {"result": {"pact_id": "p-1"}}

    def get_pact(self, pact_id):  # noqa: ANN001
        return {"result": {"api_key": "k-1", "status": self.revoke_status if self.revoked else "active"}}

    def revoke_pact(self, pact_id):  # noqa: ANN001
        self.revoke_calls.append(pact_id)
        self.revoked = True
        return {"success": True}

    def transfer(self, wallet_uuid, body, *, api_key=None):  # noqa: ANN001
        self.transfers.append(api_key)
        if self.revoked and self.revoke_actually_works:
            # 撤销后：key 本身失效（实测返回 401 IDENTITY_INVALID_API_KEY）
            raise CoboError(401, {"error": {"code": "IDENTITY_INVALID_API_KEY"}})
        raise PolicyDenied(403, "TRANSFER_LIMIT_EXCEEDED", "transfer_limit_exceeded")


CREDS = {
    "AGENT_WALLET_API_KEY": "k", "AGENT_WALLET_WALLET_ID": "w",
    "AGENT_WALLET_ADDRESS": "0xA", "PAY_TO_ADDRESS": "0xB",
}


def test_revoke_check_passes_when_the_reason_changes():
    """撤销生效：拒绝理由从「策略」变成「key 失效」。"""
    code = revoke_check._run(FakePactClient(), CREDS, sleep=lambda _s: None)
    assert code == 0


def test_revoke_check_fails_when_the_reason_is_unchanged():
    """**核心否证**：撤销后理由一模一样 ⇒ 还是策略在拒 ⇒ 证明不了撤销生效。

    这正是恒真 ✅ 的入口：只要判据写成"两次都被拒就算过"，
    一个**完全没生效**的撤销也能拿到 ✓。
    """
    client = FakePactClient(revoke_actually_works=False)
    code = revoke_check._run(client, CREDS, sleep=lambda _s: None)
    assert code == 1, "理由没变却判通过 —— 那这个检查什么都没证明"
    assert client.revoke_calls == ["p-1"], "但撤销本身确实发出去过"


def test_revoke_check_fails_when_the_platform_does_not_confirm_revocation():
    """撤销请求发了，但状态没变成 revoked ⇒ 不能算过。"""
    client = FakePactClient(revoke_status="active")
    code = revoke_check._run(client, CREDS, sleep=lambda _s: None)
    assert code == 1


def test_revoke_check_aborts_if_the_over_limit_transfer_is_allowed():
    """超限转账竟然放行 ⇒ 边界根本没生效，前提不成立，必须中止而不是继续判。"""

    class Allowing(FakePactClient):
        def transfer(self, wallet_uuid, body, *, api_key=None):  # noqa: ANN001
            return {"success": True}

    code = revoke_check._run(Allowing(), CREDS, sleep=lambda _s: None)
    assert code == 1


def test_revoke_check_uses_a_deliberately_over_limit_amount():
    """金额必须**必然超限** —— 否则撤销万一没生效，就真的把钱转出去了。

    这是零成本设计的根据：全程只用会被策略拒的金额。
    """
    assert Decimal(revoke_check.OVER_LIMIT) > Decimal("0.01"), (
        f"OVER_LIMIT={revoke_check.OVER_LIMIT} 必须大于 Pact 单笔上限 0.01，"
        "否则撤销失败时会真的转出钱"
    )


# ---------------------------------------------------------------------------
# 事件通道：分清「通道不可用」与「没有事件」
# ---------------------------------------------------------------------------


def test_probe_sse_reports_404_as_unavailable(monkeypatch):
    """404 ⇒ 通道不可用，且理由里要说清**不是权限问题**（不带 key 也 404）。"""
    def fake_stream(method, url, headers=None, timeout=None):  # noqa: ANN001
        return httpx.Response(404, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Ctx(fake_stream(*a, **k)))
    ok, why = event_watch.probe_sse(type("C", (), {"base_url": "http://x", "api_key": "k"})())
    assert ok is False
    assert "404" in why


class _Ctx:
    def __init__(self, response):  # noqa: ANN001
        self._r = response

    def __enter__(self):
        return self._r

    def __exit__(self, *exc):  # noqa: ANN001
        return False


def test_event_watch_says_unavailable_rather_than_no_events(monkeypatch, capsys):
    """**核心否证**：SSE 404 时绝不能说"平台没有这个事件"。

    平台是**文档里有、路由没有** —— 说成"没有这个事件类型"就把结论说反了。
    正确结论是"通道不可用，退回轮询"，而且要真的退回去、真的看到那条拒绝。
    """
    class Client(FakePactClient):
        base_url = "http://x"
        api_key = "k"

        def list_audit_logs(self, **kw):  # noqa: ANN003
            return {"result": {"items": [{"id": 1, "action": "transfer.denied",
                                          "result": "denied",
                                          "created_at": "2099-01-01T00:00:00Z"}]}}

    monkeypatch.setattr(event_watch, "load_existing", lambda: dict(CREDS))
    monkeypatch.setattr(event_watch, "probe_sse", lambda c: (False, "HTTP 404 —— 端点不存在"))
    monkeypatch.setattr(event_watch, "_trigger_violation", lambda c, cr: "denied（替身）")
    monkeypatch.setattr(event_watch, "POLL_INTERVAL_S", 0.01)

    code = event_watch.run(client=Client())

    out = capsys.readouterr().out
    assert "平台没提供这个通道" in out
    assert "轮询" in out, "必须真的退回轮询"
    assert code == 0, "轮询应当看到那条拒绝"


def test_polling_does_not_claim_success_when_nothing_is_seen(monkeypatch, capsys):
    """轮询窗口内什么都没看到 ⇒ 如实报失败，不假装收到了。"""
    class Client(FakePactClient):
        def list_audit_logs(self, **kw):  # noqa: ANN003
            return {"result": {"items": []}}

    monkeypatch.setattr(event_watch, "load_existing", lambda: dict(CREDS))
    monkeypatch.setattr(event_watch, "_trigger_violation", lambda c, cr: "denied（替身）")
    monkeypatch.setattr(event_watch, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(event_watch, "POLL_TIMEOUT_S", 0.05)

    code = event_watch._watch_by_polling(Client(), CREDS)

    out = capsys.readouterr().out
    assert code == 1
    assert "没有在审计日志里看到" in out
