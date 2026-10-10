"""运行前置检查：把"环境其实不满足"这件事在**跑之前**说出来。

为什么需要它（T12）：演示会花真钱（测试币）、写真证据。如果钱包余额不够，
演示会跑到一半失败，或者更糟——**产出一份"少了一笔"的证据**，而那份证据看起来
完全正常。所以开跑前先检查，不够就明确报错退出。

单位是这里最大的陷阱：Cobo 余额接口的 `amount` 是**整币十进制字符串**（`"19.96"`），
而 x402 的 `accepts[].amount` 是**原子单位**（`"5000"` = 0.005 USDC）。两套混用会静默出错。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# USDC 是 6 位小数。这个演示只碰 USDC，所以写死；换币种要一起改。
USDC_DECIMALS = 6


class PreconditionError(RuntimeError):
    """运行前的环境检查没通过。**必须在花任何钱之前抛出。**"""


def find_balance(
    balances: list[dict[str, Any]], chain_id: str, token_id: str
) -> dict[str, Any] | None:
    """按 `chain_id` + `token_id` 定位一条余额记录。"""
    for row in balances:
        if not isinstance(row, dict):
            continue
        if row.get("chain_id") == chain_id and row.get("token_id") == token_id:
            return row
    return None


def available_atomic(
    balances: list[dict[str, Any]],
    chain_id: str,
    token_id: str,
    *,
    in_atomic: bool = False,
) -> Decimal | None:
    """可用余额。

    **查不到时返回 `None`（未知），不是 `0`**——"没钱"和"我不知道这个币"是两回事，
    混为一谈会让"链写错了"看起来像"钱包没钱"，排错时被带偏。

    `in_atomic=True` 时换算成原子单位整数（用于和 x402 的 `amount` 直接比大小）。
    """
    row = find_balance(balances, chain_id, token_id)
    if row is None:
        return None
    try:
        amount = Decimal(str(row.get("amount", "0")))
    except InvalidOperation:
        return None
    return amount * (10**USDC_DECIMALS) if in_atomic else amount


def require_balance(
    balances: list[dict[str, Any]],
    chain_id: str,
    token_id: str,
    needed: Decimal,
    *,
    label: str = "本次演示",
) -> None:
    """余额不足以覆盖 `needed`（整币）时抛 `PreconditionError`。

    值不值得单独报错：一份"少了一笔采购"的证据看起来和正常的一模一样，
    事后没人能看出它是残缺的。**宁可跑不起来，也不要跑出一份假证据。**
    """
    available = available_atomic(balances, chain_id, token_id)
    if available is None:
        raise PreconditionError(
            f"查不到 {chain_id}/{token_id} 的余额，无法确认{label}能不能跑完。"
            f"（注意：这和「余额为 0」不是一回事——很可能是链或币的 id 写错了。）"
        )
    if available < needed:
        raise PreconditionError(
            f"{label}需要 {needed} USDC，但钱包只有 {available} USDC，还差 {needed - available}。"
            f"请先领测试币（见 README），否则会跑出一份缺笔的证据。"
        )
