.PHONY: help install t0-probe a0e demo agent verify pact dashboard complete-check cumulative-check boundary-check payee test clean

VENV := .venv
PY := $(VENV)/bin/python

help:
	@echo "install   —— 建 venv 并装依赖"
	@echo "t0-probe  —— T0 探针：凭证/钱包/地址 + 超限拒绝（零资金即可跑）"
	@echo "a0e       —— A0e 探针：x402 付款全链路（需先 make payee + 领到测试币）"
	@echo "demo      —— 演示主流程：两笔采购（一笔成功、一笔被拒）+ 证据落盘"
	@echo "agent     —— 让模型自己驱动一次真实采购（工具调用主循环，全程留痕）"
	@echo "pact      —— A1：验证提交的 Pact 被服务端原样存下（逐字段比对）"
	@echo "verify    —— A7a：校验最近一次运行的证据哈希链与计数"
	@echo "dashboard —— 起证据展示页，默认 http://127.0.0.1:8000"
	@echo "complete-check —— T9：验证完成条件触发后 Pact 自动终止、密钥失效"
	@echo "cumulative-check —— A5b：验证累计总预算真的会被拒绝"
	@echo "boundary-check —— R12：边界声明只应出现在 README 里"
	@echo "payee     —— 起收款方（两个付费 API），默认 127.0.0.1:8001"
	@echo "test      —— 跑单测"

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements.txt

# T0：把"能不能真跑"打掉。零资金——策略检查先于余额检查，所以钱包没币照样能验出拒绝码。
t0-probe:
	$(PY) -m pactpay.spike.t0_probe

# A0e：x402 全链路（402 → Cobo 签名提交 → retry_headers 重放 → 链上复核）。
# 需要 agent 钱包里有 Base Sepolia USDC；没有的话探针会明确告诉你"还没领到币"并退出。
a0e:
	$(PY) -m pactpay.spike.a0e_probe

# 收款方。PAY_TO_ADDRESS 从 .env 读（t0-probe 首次运行会写进去）。
payee:
	$(PY) -m pactpay.payee.app

# 演示主流程：任务/预算 → 采购计划 → Pact → 逐笔采购 → 证据落盘。
# 需要先 make payee，且 agent 钱包里有 Base Sepolia USDC。
demo:
	$(PY) -m pactpay.demo

agent: ## 让模型自己驱动一次真实采购（Agent 主循环，工具调用）
	.venv/bin/python -m pactpay.agent_run

# T9：完成条件（tx_count=1）触发后 Pact 自动终止、pact-scoped key 立即失效。
# 需要先 make payee，且钱包里有测试币（本检查会真的花掉一笔）。
complete-check:
	$(PY) -m pactpay.complete_check

# A5b：累计总预算。用独立的第二份 Pact（阈值可达）验同一条代码路径——
# demo Pact 的 0.1 档算术上够不着（单笔 0.01 × 11 笔才到 0.11）。
cumulative-check:
	$(PY) -m pactpay.cumulative_check

# R12：边界声明（不做什么）只允许出现在 README 里。
# 源码里若冒出 DeFi / 多链之类的实现，说明范围蔓延了——这条能机械地挡住。
#
# `BOUNDARY_TARGET` 让阳性对照把"被扫描的树"指向临时目录
# （评审第 4 轮 F12：原先那个阳性对照往**被审查的仓库里**写
# `pactpay/_boundary_probe.py`，跑一次 pytest 就临时改动工作区）。
BOUNDARY_TARGET ?= pactpay
boundary-check:
	@# R12 的机械检查：**只扫实际交付的代码**（`$(BOUNDARY_TARGET)/`）。
	@# 两条必须写明的边界，都是实测踩出来的：
	@#  1) `\b` 词边界：不加的话 `defi` 会匹配到 `defined` / `definition`，
	@#     满仓库的英文注释全被误判成"越界词汇"。
	@#  2) **不扫 tests/**：`tests/test_repo_hygiene.py` 里必须**字面写出**这些词
	@#     才能测"词表真的能命中越界词"——扫它会让本目标恒定失败，
	@#     而"一个永远失败的检查等于没有检查"。R12 约束的是**产品**，不是测试夹片。
	@if grep -rniE "\b(defi|liquidity|multi-?chain|agent market)\b" --include=*.py --include=*.md $(BOUNDARY_TARGET)/; then \
	  echo "✗ 交付代码里出现了越界词汇"; exit 1; \
	else echo "✓ 通过：交付代码（$(BOUNDARY_TARGET)/）里没有越界词汇"; fi

# A1：逐字段比对提交的 Pact 与服务端回读（限额/白名单被改动是最危险的静默失败）。
pact:
	$(PY) -m pactpay.pact_check

# A7a：校验最近一次运行的证据（哈希链是否完好、计数是否对得上、有无幂等重放）。
verify:
	@# 只挑**完整**的运行（有 run.json 的）。demo 会在任何实质工作之前就建好目录，
	@# 它那些"设计上会 raise"的守卫会留下空目录 —— 按名字取最新就会挑中空目录，
	@# 报出 "0 条 / 计数对不上"，看起来像证据链坏了，其实只是挑错了目录（评审 N5）。
	@# 目录的选择必须**和看板用同一个函数**（评审第 4 轮 F8）：
	@# 原先这里用 `[ -f run.json ]` 只判断**文件存在**，而 `is_complete_run`
	@# 要求它**能解析**。于是一个 run.json 被截断的运行会被 `make verify` 挑中
	@# （而且 verify_run 根本不读 run.json，所以它照样打 ✓），
	@# 同时看板挑的是**另一个更旧的**目录 —— 两个入口对"最新一次运行"的理解不一致。
	@latest=$$($(PY) -m pactpay.dashboard.app --latest-run); \
	if [ -z "$$latest" ]; then echo "还没有**完整**的证据，先跑 make demo"; exit 1; fi; \
	echo "校验：$$latest"; \
	$(PY) -m pactpay.evidence.verify "$$latest"

# 证据展示页（读 evidence/ 里最近一次运行的结果）。
dashboard:
	$(PY) -m uvicorn pactpay.dashboard.app:app --host 127.0.0.1 --port 8000

test:
	$(PY) -m pytest -q

clean:
	rm -rf $(VENV) .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
