"""A4：链上复核 —— 证明"付款方的钱真的动了"，而不只是"链上发生过一笔交易"。

计划的 A4 要求：用 RPC 取 receipt，断言 `status == "0x1"`，并**解出 USDC 的 `Transfer`
事件且 `from`/`to`/`value` 都对**。只查 receipt 存在证明不了钱动了。

这里测的是**解码的纯逻辑**（不碰网络）：给一组 logs，能不能正确挑出 USDC 的 Transfer
事件、并正确解出 from/to/value；以及**该被排除的能不能被排除**（别的合约、别的事件类型）。
"""

from __future__ import annotations

import pytest

from pactpay.platform.onchain import TRANSFER_TOPIC, decode_usdc_transfers

USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
FROM = "0x1f3963c9ebc26cce128ec00a779a948bcc2f7ffa"
TO = "0x757868baab9ab42a20d0ed2d8ffcff9c6df82ea7"


def _addr_topic(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


class FakeLog:
    """手写的 log 替身（不是 Mockito）——只有真实用到的三个属性。"""

    def __init__(self, address, topics, data):
        self.address = address
        self.topics = topics
        self.data = data


def _transfer_log(address=USDC, src=FROM, dst=TO, value=5000):
    return FakeLog(address, [TRANSFER_TOPIC, _addr_topic(src), _addr_topic(dst)], hex(value))


def test_decodes_a_usdc_transfer():
    out = decode_usdc_transfers([_transfer_log()], usdc_address=USDC)

    assert len(out) == 1
    assert out[0]["from"].lower() == FROM
    assert out[0]["to"].lower() == TO
    assert out[0]["value"] == 5000


def test_ignores_transfers_from_other_contracts():
    """**关键**：别的合约也会发同名 Transfer 事件，必须按合约地址过滤掉。"""
    other = _transfer_log(address="0x000000000000000000000000000000000000dEaD")

    assert decode_usdc_transfers([other], usdc_address=USDC) == []


def test_ignores_other_event_types_from_the_same_contract():
    approval = FakeLog(USDC, ["0x" + "8c" * 32, _addr_topic(FROM), _addr_topic(TO)], hex(1))

    assert decode_usdc_transfers([approval], usdc_address=USDC) == []


def test_reads_value_from_bytes_data_too():
    """RPC 返回的 `data` 可能是 bytes 而不是 hex 字符串，两种都要能解。"""
    log = FakeLog(USDC, [TRANSFER_TOPIC, _addr_topic(FROM), _addr_topic(TO)], (5000).to_bytes(32, "big"))

    assert decode_usdc_transfers([log], usdc_address=USDC)[0]["value"] == 5000


def test_usdc_address_match_is_case_insensitive():
    """链上地址大小写不固定（EIP-55 校验和），比对必须忽略大小写。"""
    log = _transfer_log(address=USDC.lower())

    assert len(decode_usdc_transfers([log], usdc_address=USDC)) == 1


def test_multiple_transfers_are_all_returned():
    logs = [_transfer_log(value=5000), _transfer_log(value=7)]

    assert [t["value"] for t in decode_usdc_transfers(logs, usdc_address=USDC)] == [5000, 7]


# ---- B3：`verify_usdc_transfer` 的判定逻辑（评审指出它**零测试**、且从不核对 from）----
from pactpay.platform.onchain import check_usdc_transfer  # noqa: E402

FROM_ADDR = "0x1f3963c9ebc26cce128ec00a779a948bcc2f7ffa"
OTHER_ADDR = "0x9999999999999999999999999999999999999999"


def _transfer(src=FROM_ADDR, dst=TO, value=5000):
    return {"from": src, "to": dst, "value": value}


def test_check_passes_when_from_to_and_value_all_match():
    errors = check_usdc_transfer([_transfer()], expect_from=FROM_ADDR, expect_to=TO, expect_value=5000)

    assert errors == []


def test_check_rejects_a_transfer_from_the_wrong_sender():
    """**评审的 M38**：`from` 没被核对时这个变异无人变红。

    钱的来源不对 = 不是这笔付款（可能是**别人**替我们付的，或根本是另一笔交易）。
    """
    errors = check_usdc_transfer([_transfer(src=OTHER_ADDR)], expect_from=FROM_ADDR, expect_to=TO, expect_value=5000)

    assert errors, "付款方不是我们的钱包，却没报错"


def test_check_rejects_the_wrong_recipient():
    """M39：`to` 没被核对。"""
    errors = check_usdc_transfer([_transfer(dst=OTHER_ADDR)], expect_from=FROM_ADDR, expect_to=TO, expect_value=5000)

    assert errors


def test_check_rejects_the_wrong_amount():
    """M40：`value` 没被核对。"""
    errors = check_usdc_transfer([_transfer(value=1)], expect_from=FROM_ADDR, expect_to=TO, expect_value=5000)

    assert errors


def test_check_rejects_an_empty_transfer_list():
    """一笔 Transfer 都没有 = 钱根本没动，不能因为"没有反例"就放行。"""
    errors = check_usdc_transfer([], expect_from=FROM_ADDR, expect_to=TO, expect_value=5000)

    assert errors


def test_check_errors_name_the_field_that_mismatched():
    """错误信息要指得出是哪个字段不对——否则排查时只知道"没过"。"""
    errors = check_usdc_transfer([_transfer(src=OTHER_ADDR)], expect_from=FROM_ADDR, expect_to=TO, expect_value=5000)

    assert any("from" in e.lower() or "付款方" in e for e in errors)


# ---------------------------------------------------------------------------
# E11（评审第 3 轮 #9）：`count_usdc_transfers_from` 的"不抛异常"契约
#
# docstring 写着"失败不抛异常，把原因写进 error"。但 `Web3(...)` 构造、
# `is_connected()`、`block_number` 都在 try 之外 —— 契约没兑现。
# 下面用一个**会炸的假 Web3** 把这条路径逼出来。
# ---------------------------------------------------------------------------

def _install_exploding_web3(monkeypatch, *, where: str):
    """装一个在指定环节爆炸的假 `web3` 模块。

    只替换 `count_usdc_transfers_from` 内部 `from web3 import Web3` 拿到的东西，
    真实网络一行都不碰。
    """
    import sys
    import types

    class ExplodingProvider:
        def __init__(self, *a, **k):
            if where == "provider":
                raise ConnectionError("provider 初始化失败")
            self._head = 48000000

    class ExplodingEth:
        @property
        def block_number(self):
            # **`block_number` 也必须能炸**（评审 E11）：它是"取链头"那一步，
            # 原来在 try 之外，异常会直接冒出去。第一版这里写成普通类属性，
            # 于是把 `head = int(w3.eth.block_number)` 挪出 try 的变异照样全绿。
            if where == "block_number":
                raise ConnectionError("取链头炸了")
            return 48000000

        def get_logs(self, *a, **k):
            if where == "get_logs":
                raise ConnectionError("getLogs 炸了")
            return []

    class ExplodingWeb3:
        HTTPProvider = ExplodingProvider

        def __init__(self, provider):
            if where == "construct":
                raise ConnectionError("Web3 构造失败")
            self.eth = ExplodingEth()

        def is_connected(self):
            if where == "is_connected":
                raise ConnectionError("探活炸了")
            return True

        @staticmethod
        def to_checksum_address(a):
            if where == "checksum":
                raise ValueError("地址不合法")
            return a

    fake = types.ModuleType("web3")
    fake.Web3 = ExplodingWeb3
    monkeypatch.setitem(sys.modules, "web3", fake)


@pytest.mark.parametrize(
    "where",
    ["provider", "construct", "is_connected", "checksum", "block_number", "get_logs"],
)
def test_count_usdc_transfers_never_raises_anywhere(monkeypatch, where):
    """**任何环节炸掉都必须变成 `{ok: False, error: ...}`，不许外抛。**

    它上面套着 `_safe_count` 兜底没错，但**契约写在 docstring 里就该兑现** ——
    靠调用方兜底意味着"换个调用方就会崩"。这条把 5 个环节逐个逼一遍。
    """
    from pactpay.platform.onchain import count_usdc_transfers_from

    _install_exploding_web3(monkeypatch, where=where)

    try:
        result = count_usdc_transfers_from(
            "0x1f3963c9ebc26cce128ec00a779a948bcc2f7ffa", from_block=1, to_block=2
        )
    except Exception as error:  # noqa: BLE001
        raise AssertionError(
            f"{where} 环节的异常冒出来了（{type(error).__name__}: {error}）—— "
            f"违反「失败不抛异常，把原因写进 error」的契约"
        ) from error

    assert result["ok"] is False, f"{where} 环节失败了却报 ok=True"
    assert result["error"], f"{where} 环节失败了却没写 error"


def test_decoding_is_unaffected_by_the_stub(monkeypatch):
    """**阳性对照**：假 Web3 只是让网络层失败，解码逻辑本身不受影响。

    没有这一条，上面那组测试可能只是"函数永远返回 ok=False"。
    """
    from pactpay.platform.onchain import decode_usdc_transfers

    _install_exploding_web3(monkeypatch, where="get_logs")

    decoded = decode_usdc_transfers([_transfer_log()], usdc_address=USDC)

    assert len(decoded) == 1
    assert decoded[0]["value"] == 5000


def test_a_window_entirely_beyond_the_chain_head_is_inconclusive(monkeypatch):
    """**整段窗口都还没出块 ⇒ 判不了，绝不是"数到 0 笔 = 没有痕迹"**。

    本轮自查发现（第 4 轮评审 F3 的同一类残留）：`from_block > to_block`（被链头夹过之后）
    原来直接返回 `{"ok": True, "count": 0}`。可走到这一支说明那些区块**还没出**，
    所以"数到 0 笔"不是"没有转账"，而是"**根本没有数据可数**"。
    把"没测"当成"测了而且没有"，正是本项目反复被抓的那一类假绿灯。
    """
    import pactpay.platform.onchain as onchain

    class _Eth:
        block_number = 100

    class _W3:
        eth = _Eth()

        @staticmethod
        def HTTPProvider(*_a, **_k):  # noqa: N802 - 模仿 web3 的 API
            return object()

        def __init__(self, *_a, **_k):
            pass

        def is_connected(self):
            return True

    monkeypatch.setattr(onchain, "Web3", None, raising=False)
    import web3 as _web3

    monkeypatch.setattr(_web3, "Web3", _W3)

    result = onchain.count_usdc_transfers_from(
        "0x" + "11" * 20, from_block=500, to_block=600, rpc_url="http://x"
    )

    assert result["ok"] is False, (
        f"整段窗口都在链头之后，却给出了 ok=True —— 这是从「没有数据」推出「没有痕迹」：{result}"
    )
    assert result["count"] is None, "判不了的时候 count 必须是 None，不能是 0"
    assert "还没出块" in (result["error"] or "")


# ---- `verify_usdc_transfer` 的 receipt 层：`status != 1` 必须判失败 ----
# 这一段原来**零测试**：`check_usdc_transfer`（纯函数）被测得很全，但
# "先看 receipt 成功没有"这一步在 `verify_usdc_transfer` 里，而它需要 RPC。
# 一个**失败**的交易也可能带着 Transfer 事件日志（例如后续 revert 的合约调用），
# 所以"日志对得上"不能替代"交易成功"。这里用替身把 RPC 打掉。


class _FakeReceipt:
    def __init__(self, status, block_number=48014861, logs=()):
        self.status = status
        self.blockNumber = block_number
        self.logs = list(logs)


class _FakeEth:
    def __init__(self, receipt):
        self._receipt = receipt

    def wait_for_transaction_receipt(self, tx_hash, timeout=None):
        return self._receipt


def _verified_with(monkeypatch, receipt, *, transfers):
    """把 `web3.Web3` **整个**换成替身（`verify_usdc_transfer` 是函数内 import，所以打这里）。"""
    import web3
    import pactpay.platform.onchain as onchain_mod

    class _FakeWeb3:
        def __init__(self, *a, **k):
            self.eth = _FakeEth(receipt)

        @staticmethod
        def HTTPProvider(*a, **k):  # `verify_usdc_transfer` 会先建 provider
            return None

        def is_connected(self):
            return True

    monkeypatch.setattr(web3, "Web3", _FakeWeb3)
    monkeypatch.setattr(onchain_mod, "decode_usdc_transfers", lambda logs, **k: transfers)
    return onchain_mod.verify_usdc_transfer(
        "0xdead", expect_from=FROM_ADDR, expect_to=TO, expect_value=5000
    )


def test_verify_rejects_a_failed_receipt_even_when_the_log_looks_right(monkeypatch):
    """**`status != 1` 必须判失败** —— 日志对得上不能替代交易成功。

    这是一个真实的坑：一个 reverted 的交易**仍然可能**留下 Transfer 事件
    （同一个 tx 里先 transfer 再 revert，或事件来自别的合约调用）。
    只看日志就判"钱付成功了"，会把一笔**没有真正完成**的付款记成成功。
    """
    receipt = _FakeReceipt(status=0)
    result = _verified_with(monkeypatch, receipt, transfers=[_transfer()])

    assert result["ok"] is False, "失败的 receipt 竟然通过了 —— 这就是那个坑"
    assert any("status" in e for e in result["errors"]), f"要指出是 status 的问题：{result['errors']}"


def test_verify_accepts_a_successful_receipt_with_a_matching_transfer(monkeypatch):
    """阳性对照：成功的 receipt + 对得上的日志必须通过（否则上一条可能只是"永远失败"）。"""
    receipt = _FakeReceipt(status=1)
    result = _verified_with(monkeypatch, receipt, transfers=[_transfer()])

    assert result["ok"] is True, f"正常的一笔被判失败：{result['errors']}"
    assert result["block_number"] == 48014861
    assert result["errors"] == []
