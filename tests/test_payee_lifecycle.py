"""A11a：`make demo` 必须**自带拉起并健康检查收款方**，而不是要求用户另开一个终端。

为什么这算一条验收标准（而不是"顺手加的便利功能"）：README 的"快速开始"要能**照着跑通**。
如果演示依赖一个用户手动开的服务，那"一条命令复现"就是假的——用户漏开一个终端，
看到的是一句"连接被拒绝"，而不是这个项目想展示的任何东西。
"""

from __future__ import annotations


from pactpay.payee_lifecycle import ensure_payee, is_payee_up



def test_reports_up_when_health_says_ok(monkeypatch):
    monkeypatch.setattr(
        "pactpay.payee_lifecycle.httpx.get",
        lambda url, timeout=None, **kw: type("R", (), {"status_code": 200, "json": lambda s: {"status": "ok"}})(),
    )

    assert is_payee_up("http://127.0.0.1:8001") is True


def test_reports_down_when_connection_refused(monkeypatch):
    def boom(url, timeout=None, **kw):
        raise ConnectionError("refused")

    monkeypatch.setattr("pactpay.payee_lifecycle.httpx.get", boom)

    assert is_payee_up("http://127.0.0.1:8001") is False


def test_reports_down_when_health_is_not_ok(monkeypatch):
    """/health 回 200 但 `status != "ok"` 也算没起来——不能只看状态码。"""
    monkeypatch.setattr(
        "pactpay.payee_lifecycle.httpx.get",
        lambda url, timeout=None, **kw: type("R", (), {"status_code": 200, "json": lambda s: {"status": "starting"}})(),
    )

    assert is_payee_up("http://127.0.0.1:8001") is False


def test_ensure_payee_reuses_an_already_running_payee(monkeypatch):
    """已经起着就**不要**再起一个（否则端口冲突，演示直接挂）。"""
    monkeypatch.setattr("pactpay.payee_lifecycle.is_payee_up", lambda base, **kw: True)
    started = []
    monkeypatch.setattr("pactpay.payee_lifecycle._spawn", lambda *a, **kw: started.append(1))

    proc = ensure_payee("http://127.0.0.1:8001")

    assert proc is None          # 复用了别人的，没有子进程要收
    assert started == []


def test_ensure_payee_raises_when_the_new_payee_never_becomes_healthy(monkeypatch):
    """起不来就必须**明确报错**，不能带着一个连不上的收款方继续跑。"""
    monkeypatch.setattr("pactpay.payee_lifecycle.is_payee_up", lambda base, **kw: False)
    monkeypatch.setattr("pactpay.payee_lifecycle._spawn", lambda *a, **kw: type("P", (), {"terminate": lambda s: None, "wait": lambda s, timeout=None: 0})())
    monkeypatch.setattr("pactpay.payee_lifecycle.time.sleep", lambda s: None)

    import pytest
    with pytest.raises(RuntimeError, match="收款方"):
        ensure_payee("http://127.0.0.1:8001", wait_seconds=0.01)
