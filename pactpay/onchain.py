"""链上复核：证明"付款方的钱真的动了"，而不只是"链上发生过一笔交易"。

**为什么需要它**（`plan` 的 A4）：只查 receipt 存在只能证明链上发生过一笔交易，
证明不了这笔交易**是我们要的那笔付款**。所以要把 receipt 里的 USDC `Transfer`
事件解出来，逐字段核对 `from` / `to` / `value`。

**为什么按合约地址过滤**：任何合约都能发一个签名相同的 `Transfer(address,address,uint256)`
事件。不按 `usdc_address` 过滤，别的代币的转账会被误当成 USDC 付款（**静默错认**）。

解码逻辑做成**纯函数**（`decode_usdc_transfers`）以便脱网单测；RPC 调用在外面那层。
"""

from __future__ import annotations

from typing import Any

# keccak256("Transfer(address,address,uint256)") —— ERC-20 标准事件签名，链上常量
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

BASE_SEPOLIA_RPC = "https://sepolia.base.org"
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


def _hexstr(value: Any) -> str:
    """把 `HexBytes` / bytes / str 统一成小写 `0x…` 字符串。"""
    if hasattr(value, "hex"):
        value = value.hex()
    text = str(value)
    return text if text.startswith("0x") else "0x" + text


def _topic_address(topic: Any) -> str:
    """事件 topic 里的地址是左补零到 32 字节的，取低 20 字节。"""
    raw = _hexstr(topic).removeprefix("0x")
    return "0x" + raw[-40:]


def decode_usdc_transfers(logs: list[Any], *, usdc_address: str) -> list[dict[str, Any]]:
    """从收据的 logs 里解出指定 USDC 合约发出的 `Transfer` 事件。

    纯函数：不碰网络，便于单测。返回 `[{from, to, value}, …]`（按出现顺序）。
    """
    out: list[dict[str, Any]] = []
    target = usdc_address.lower()
    for log in logs:
        if _hexstr(log.address).lower() != target:
            continue
        topics = list(log.topics)
        if not topics or _hexstr(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        # `data` 可能是 bytes（RPC 有时给 bytes）也可能是 "0x…" 字符串。
        # 两者都按**十六进制**解析——bytes 的 `.hex()` 不带 `0x` 前缀，若按十进制解析会
        # 静默算出一个错值（本文件的单测正是抓住了这个 bug）。
        data = log.data
        raw = data.hex() if hasattr(data, "hex") else str(data)
        out.append(
            {
                "from": _topic_address(topics[1]),
                "to": _topic_address(topics[2]),
                "value": int(raw.removeprefix("0x").removeprefix("0X"), 16),
            }
        )
    return out


def check_usdc_transfer(
    transfers: list[dict[str, Any]],
    *,
    expect_from: str,
    expect_to: str,
    expect_value: int,
) -> list[str]:
    """纯函数：核对"这笔付款"的三个要素，返回错误列表（空 = 通过）。

    **三个都要核**（B3，第 7 步评审阻断项）：
    - `from`：钱的**来源**必须是我们的钱包 —— 不核它，"别人替我们付的"也会被当成我们的付款；
    - `to`：收款方必须是约定的那个；
    - `value`：金额必须**恰好**相等（x402 `exact` 方案不允许多付或少付）。

    单独立成纯函数是为了**能脱网单测** —— 之前 `verify_usdc_transfer` 一个测试都没有，
    评审的三个变异（不核 `from` / 不核 `to` / 不核 `value`）全部无人变红。
    """
    errors: list[str] = []
    if not transfers:
        return [f"这笔交易里没有 {expect_to} 的 USDC Transfer —— 钱没有动"]

    def _match(t: dict[str, Any]) -> bool:
        return (
            str(t.get("from", "")).lower() == expect_from.lower()
            and str(t.get("to", "")).lower() == expect_to.lower()
            and t.get("value") == expect_value
        )

    if not any(_match(t) for t in transfers):
        seen = [{ "from": t.get("from"), "to": t.get("to"), "value": t.get("value")} for t in transfers]
        errors.append(
            f"没有一笔 Transfer 同时满足 付款方(from)={expect_from}、"
            f"收款方(to)={expect_to}、金额(value)={expect_value}；实际={seen}"
        )
    return errors


def verify_usdc_transfer(
    tx_hash: str,
    *,
    expect_from: str,
    expect_to: str,
    expect_value: int,
    rpc_url: str = BASE_SEPOLIA_RPC,
    usdc_address: str = USDC_BASE_SEPOLIA,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """用 RPC 复核一笔 USDC 转账：receipt 成功 + `Transfer` 事件的 `to`/`value` 都对。

    返回 `{ok, block_number, transfers, errors}`。**失败不抛异常**，把原因写进 `errors`
    ——调用方（demo / 探针）要把它当成一条可展示的证据，而不是崩掉整个流程。
    """
    from web3 import Web3

    errors: list[str] = []
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        return {"ok": False, "block_number": None, "transfers": [], "errors": [f"RPC 连不上 {rpc_url}"]}

    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 —— 超时/未上链都要作为证据返回，不让流程崩
        return {"ok": False, "block_number": None, "transfers": [], "errors": [f"等 receipt 失败：{exc}"]}

    if receipt.status != 1:
        errors.append(f"交易未成功：receipt.status={receipt.status}")

    transfers = decode_usdc_transfers(list(receipt.logs), usdc_address=usdc_address)
    errors.extend(
        check_usdc_transfer(
            transfers, expect_from=expect_from, expect_to=expect_to, expect_value=expect_value
        )
    )

    return {
        "ok": not errors,
        "block_number": receipt.blockNumber,
        "transfers": transfers,
        "errors": errors,
    }


def count_usdc_transfers_from(
    address: str,
    *,
    from_block: int,
    to_block: int,
    rpc_url: str = BASE_SEPOLIA_RPC,
    usdc_address: str = USDC_BASE_SEPOLIA,
) -> dict[str, Any]:
    """数某个地址在**区块区间**内发出的 USDC `Transfer` 笔数（`eth_getLogs`）。

    返回 `{ok, count, error}`；失败不抛异常，把原因写进 `error`。

    **为什么需要它**（N2）：原来的"无链上痕迹"判据是**紧邻两次钱包快照的差值**，
    而 Cobo 的余额/Pact 计数接口是**滞后**的 —— 实测 17 次真实运行里有 16 次，
    **确已链上结算**的付款同样报"余额未变"、`tx_count` 更是 17/17 全为 `0→0`。
    也就是说那个度量**分不清"钱没动"和"钱动了"**，它的"没变化"证明不了任何事。

    链上日志没有滞后问题：转账一旦进块就立刻可查。所以用它当判据，
    并且**必须配阳性对照**（见 `block_evidence.check_no_onchain_transfer`）。
    """
    from web3 import Web3

    # **构造与探活也在 try 里。** 评审第 3 轮 #9：原来只有 `eth_getLogs` 被包住，
    # `Web3(...)` 构造与 `is_connected()` 都可能抛（provider 初始化、DNS、超时），
    # 而 docstring 明写"失败不抛异常，把原因写进 error" —— 契约要么兑现，要么别写。
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        connected = w3.is_connected()
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "count": None, "error": f"RPC 初始化失败：{type(error).__name__}: {error}"}
    if not connected:
        return {"ok": False, "count": None, "error": f"RPC 连不上 {rpc_url}"}

    # **必须夹到当前链头**：`eth_getLogs` 对超出 head 的区间直接报
    # `block range extends beyond current head block`。夹一下即可。
    #
    # 注意：阳性对照窗口是**恰好一个区块**（`(settled_block, settled_block)`，
    # 见 `demo.py`），不是旧注释里写的"结算区块 ±5" —— ±5 会在目标窗口只差几个区块时
    # 与之相交，评审第 3 轮实跑抓到过（`(48012155,48012165)` 撞 `(48012161,48012162)`）。
    try:
        head = int(w3.eth.block_number)
    except Exception as error:  # noqa: BLE001 - 与下面同一契约：失败写进 error，不外抛
        return {"ok": False, "count": None, "error": f"取链头失败：{type(error).__name__}: {error}"}
    to_block = min(to_block, head)
    if from_block > to_block:
        # **绝不能在这里返回 `ok=True, count=0`**（本轮自查发现）。
        # 走到这一支说明整个窗口都还在链头**之后**——那些区块**还没出**，
        # 所以"数到 0 笔"不是"没有转账"，而是"**根本没有数据可数**"。
        # 返回 ok=True 就是把"没测"当成"测了而且没有"，正是本项目反复被抓的那一类。
        return {
            "ok": False,
            "count": None,
            "error": (
                f"判定窗口 [{from_block}, {to_block}] 整个在链头 {head} 之后（还没出块）"
                "—— 没有数据可数，不能据此说「没有链上痕迹」"
            ),
        }

    try:
        topic_from = "0x" + address.lower().removeprefix("0x").rjust(64, "0")
        logs = w3.eth.get_logs(
            {
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": Web3.to_checksum_address(usdc_address),
                "topics": [TRANSFER_TOPIC, topic_from],
            }
        )
    except Exception as exc:  # noqa: BLE001 —— 查询失败要作为证据返回，不让流程崩
        return {"ok": False, "count": None, "error": f"eth_getLogs 失败：{exc}"}

    return {"ok": True, "count": len(logs), "error": None}


def current_block_number(*, rpc_url: str = BASE_SEPOLIA_RPC) -> int | None:
    """当前区块高度（拿不到就返回 `None`，由调用方判为"判不了"）。"""
    from web3 import Web3

    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if not w3.is_connected():
            return None
        return int(w3.eth.block_number)
    except Exception:  # noqa: BLE001 —— 拿不到高度不能让整个演示崩，交给"判不了"分支
        return None
