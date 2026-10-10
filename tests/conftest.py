"""全局测试夹具。

## 为什么必须有一个 autouse 的 `PACT_PLANNER=deterministic`

`demo.select_planner()` 的**默认是 `llm`**——真实演示应该由一个自己算的模型来决策。
但测试绝不能在没人注意的时候去连 DeepSeek：那会让套件变慢、变脆、还会花钱，
而且一旦网络抖动，"计划算错了"和"接口不通了"就分不清。

所以这里把整个测试套件的默认大脑钉成确定性的。要测 LLM 路径的用例
**显式**构造一个假的 `client` 传进 `llm_planner(..., client=...)`，
一个字节都不出网。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _deterministic_planner_by_default(monkeypatch):
    """让每个测试默认用确定性大脑，不碰网络。"""
    monkeypatch.setenv("PACT_PLANNER", "deterministic")
    yield
