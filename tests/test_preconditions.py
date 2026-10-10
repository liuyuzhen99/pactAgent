"""T12 前置检查：余额不够时必须**明确报错**，不许静默跑出一份缺笔的假证据。

期望值来自 2026-10-12 实测：`GET /api/v1/wallets/balances` 返回的
`amount` 是**整币十进制字符串**（如 `"19.96"`），而 x402 的 `accepts[].amount`
是**原子单位**（如 `"5000"` = 0.005 USDC @6 位小数）。两套单位混用极易静默写错。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pactpay.preconditions import PreconditionError, available_atomic, require_balance

REAL_BALANCES = [
    {
        "chain_id": "TBASE_SETH",
        "token_id": "TBASE_USDC",
        "address": "0x1f3963c9ebc26cce128ec00a779a948bcc2f7ffa",
        "amount": "19.96",
        "total": "19.96",
        "pending": "0",
        "locked": "0",
    }
]


def test_reads_the_balance_by_chain_and_token():
    """按 `chain_id` + `token_id` 定位余额，而不是按 symbol 猜。"""
    assert available_atomic(REAL_BALANCES, "TBASE_SETH", "TBASE_USDC") == Decimal("19.96")


def test_converts_whole_coin_to_atomic_units():
    """`"19.96"` USDC = 19_960_000 原子单位（6 位小数）。

    换算要单独成函数并断言：一旦把"整币"当"原子单位"用（或反过来），
    限额判断会静默错掉，而演示看起来一切正常。
    """
    assert available_atomic(REAL_BALANCES, "TBASE_SETH", "TBASE_USDC", in_atomic=True) == 19_960_000


def test_missing_chain_or_token_is_reported_as_unknown_not_as_zero():
    """查不到该链/币的余额时返回 None（未知），**不是** 0。

    两者含义完全不同：0 是"没钱"，None 是"我不知道"。混为一谈会让
    "配置写错了链"看起来像"钱包没钱"，排错时被带偏。
    """
    assert available_atomic(REAL_BALANCES, "BASE_ETH", "BASE_USDC") is None


def test_require_balance_passes_when_there_is_enough():
    require_balance(REAL_BALANCES, "TBASE_SETH", "TBASE_USDC", Decimal("0.055"))


def test_require_balance_raises_loudly_when_short():
    """余额不足要抛错并说清缺多少——不许静默继续。"""
    with pytest.raises(PreconditionError) as excinfo:
        require_balance(REAL_BALANCES, "TBASE_SETH", "TBASE_USDC", Decimal("25"))

    message = str(excinfo.value)
    assert "19.96" in message and "25" in message


def test_require_balance_raises_when_the_token_is_unknown():
    """压根查不到这个币的余额时也必须报错，而不是当成"够用"。"""
    with pytest.raises(PreconditionError, match="查不到"):
        require_balance(REAL_BALANCES, "TBASE_SETH", "TBASE_UNKNOWN", Decimal("0.001"))
