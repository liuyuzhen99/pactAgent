"""让 `make demo` 一条命令跑完：自带拉起并健康检查收款方（A11a）。

为什么这不是"顺手加的便利功能"，而是一条验收标准：README 的"快速开始"要能照着跑通。
如果演示依赖用户**手动另开一个终端**起收款方，那"一条命令复现"就是假的——
用户漏开一个终端，看到的是一句"连接被拒绝"，而不是这个项目想展示的任何东西。

两条边界：
- **已经起着就复用**，不再起第二个（否则端口冲突，演示直接挂）。
- **起不来就明确报错**，绝不带着一个连不上的收款方继续跑——
  那会产出一份"每笔都失败"的证据，看起来像策略引擎坏了。
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

import httpx

HEALTH_TIMEOUT_S = 2.0
DEFAULT_WAIT_S = 20.0


def is_payee_up(base_url: str, *, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    """收款方是否已经健康。

    **不只看状态码**：`/health` 回 200 但 `status != "ok"` 也算没起来——
    只看状态码会把"正在启动"误判成"已就绪"。
    """
    try:
        response = httpx.get(base_url.rstrip("/") + "/health", timeout=timeout)
    except Exception:  # noqa: BLE001 - 连不上就是没起来，具体异常类型不重要
        return False
    if response.status_code != 200:
        return False
    try:
        return (response.json() or {}).get("status") == "ok"
    except Exception:  # noqa: BLE001 - 回的不是 JSON 也说明不是我们要的服务
        return False


def _spawn(base_url: str) -> subprocess.Popen[Any]:
    """起一个收款方子进程。

    端口从 base_url 里取，保证"检查的地址"和"起的服务"是同一个——
    否则会出现"检查 8001、起在 8002"这种永远等不到的情况。
    """
    port = base_url.rsplit(":", 1)[-1].rstrip("/")
    return subprocess.Popen(  # noqa: S603 - 参数固定，没有用户输入拼接
        [sys.executable, "-m", "pactpay.payee.app"],
        env={**__import__("os").environ, "PAYEE_PORT": port},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ensure_payee(base_url: str, *, wait_seconds: float = DEFAULT_WAIT_S) -> subprocess.Popen[Any] | None:
    """确保收款方可用。

    返回**需要本进程负责收尾**的子进程；若复用了已在跑的，返回 `None`
    （别人的进程不归我们管，收尾时不能去动它）。
    """
    if is_payee_up(base_url):
        return None

    proc = _spawn(base_url)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if is_payee_up(base_url):
            return proc
        time.sleep(0.25)

    proc.terminate()
    raise RuntimeError(
        f"收款方在 {wait_seconds:.0f} 秒内没起来（{base_url}/health 一直不是 ok）。"
        f"请先手动跑 `make payee` 看它报什么错，再跑 `make demo`。"
    )
