"""运行时配置：链/代币标识，以及 `.env` 里凭证的读写。

**为什么单独成一个模块**：这些是**生产路径**（demo / pact_check / complete_check /
cumulative_check）与探针脚本共用的东西。原先它们住在 `pactpay/spike/t0_probe.py` 里，
于是"正式的命令"反过来 import 一个叫 spike 的探针脚本 —— 依赖方向是反的：
探针本该是可以随时删掉的东西，一旦被生产路径依赖就删不动了（评审 B9）。

现在方向正过来：`config` 与 `pact_spec` 是底层，探针和生产命令都从它们取。
"""

from __future__ import annotations

from pathlib import Path

CHAIN_ID = "TBASE_SETH"      # Base Sepolia（实测确认，见 progress/cobo-raw-evidence.md）
TOKEN_ID = "TBASE_USDC"      # Base Sepolia USDC（注意 token id 不带 SETH）

ENV_PATH = Path(".env")


def load_existing() -> dict[str, str]:
    """读上次跑出来的凭证。

    **为什么必须复用**：每次跑都新建钱包的话，地址会变——而用户要去外部 faucet
    往这个地址打测试币。一旦地址变了，用户领的币就变成孤儿、后续步骤全部落空。
    所以凭证落 `.env`（已 gitignore），有就复用；每次只新建探针 Pact。
    """
    if not ENV_PATH.exists():
        return {}
    found: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        found[key.strip()] = value.strip()
    return found


def save_env(updates: dict[str, str]) -> None:
    """把值写进 `.env`（合并式：不覆盖用户已填的其它键）。"""
    existing = load_existing()
    existing.update(updates)
    lines = [
        "# PactPay Agent 的测试网凭证。**不要提交这个文件**（已在 .gitignore 里）。",
        "# 这些都是 Base Sepolia 测试网的东西，没有任何真实价值。",
        "",
    ]
    lines += [f"{k}={v}" for k, v in sorted(existing.items())]
    ENV_PATH.write_text("\n".join(lines) + "\n")
