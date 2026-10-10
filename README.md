# PactPay Agent

**在人类写死的预算和权限边界内，让 AI Agent 自主完成链上采购——超限时在签名前被硬拦下，全过程可验证、可审计。**

这是一个演示项目，不是产品。它只做一件事，但把这件事做完整：
把「规则 → 决策 → 支付 → 证据」这条链路真实跑通，而不是画一张架构图。

全程使用 **Base Sepolia 测试网**，不涉及真实资金。

---

## 它证明了什么

跑一次 `make demo`，你会看到：

1. **Agent 有自主性** —— 没有人逐笔点"确认付款"，Agent 自己决定买什么、自己发起支付、自己完成交易。
2. **边界是硬性的** —— 超限的那笔在**签名之前**就被策略引擎拒绝，链上**没有任何痕迹**。
   不是"付了再退回来"，是钱根本没动。
3. **过程可验证** —— 成功的支付有链上交易哈希（可在区块浏览器查，且 `make verify`
   会拿它去链上**对账**：擦掉一条成功记录会让链上笔数与证据条数对不上），
   被拒的请求有机器可读的拒绝码和触发的策略 id。
   **注意两件事**：① 审计日志只覆盖转账路径 —— 实测 x402 付款的拒绝**不进** Cobo 审计日志，
   所以每条拒绝在页面上都标注了**证据来源**（见下方"两条拒绝路径"），
   而不是笼统地说"都在审计日志里"；② **这套证据本身不是防篡改的**——
   证据是本地文件，能写它的人就能改内容再重算哈希。它靠**和文件夹外面的东西对账**
   站住脚：`paid` 记录对链上转账、尝试次数对 Cobo 审计日志、规则集对 Cobo 上的 pact 对象
   （边界详见下方"关于'不可篡改'这四个字"）。

---

## 快速开始

### 前置条件

- Python 3.12+
- 一个 Cobo Agentic Wallet 的 agent 凭证（第一次跑 `make t0-probe` 会自动申请，**不需要注册账号、不需要手机 App**）
- **一个 DeepSeek API key**（Agent 的"大脑"是模型，不是写死的规则）。
  默认从 `DEEPSEEK_API_KEY` 环境变量读，没有就读 `~/Downloads/.deepseek-api-key`。
  想跑离线版本：`PACT_PLANNER=deterministic make demo`（用写死的两条计划）

### 步骤

```bash
# 1. 建环境、装依赖
make install

# 2. 申请凭证 / 建钱包 / 建地址（零资金即可跑，这一步会写出 .env）
make t0-probe

# 3. 给钱包领测试币
#    这个探针会打印你的收款地址，以及 Cobo 自带 faucet 支持哪些链。
#    Cobo 的 faucet 不含 Base Sepolia，所以需要从外部 faucet 领 Base Sepolia USDC。
#    领到之后确认余额：
make t0-probe          # 再跑一次，会显示当前余额

# 4. 跑演示（会**自动**把收款方作为子进程起在 127.0.0.1:8001，跑完即收）
make demo
#    想单独起收款方调接口的话：make payee（另开一个终端）

# 5. 看证据页（默认 http://127.0.0.1:8000）
make dashboard
```

### 想单独验某一项

```bash
make test             # 全部单测
make a0e              # x402 付款全链路探针（含链上复核）
make pact             # R1：Pact 提交后回读一致，且 status 真的是 active
make cumulative-check # R2：累计花费上限真的会拒绝（不是只写在 spec 里）
make boundary-check   # R12：交付代码里不出现越界词汇（复杂 DeFi / 多链 / Agent 市场）
                      #      注：R6「默认拒绝」由 make demo 里的默认拒绝检查覆盖
make complete-check   # R10：三种完成条件（tx_count/time_elapsed/amount_spent）都实测触发
make verify           # R8：校验证据文件里的链式哈希与计数
```

---

## 演示里发生了什么

```
采购任务：「帮我买一份数据报告」  总预算：0.1 USDC

Agent 生成的采购计划：
  1. 原始运营数据   /api/cheap     0.005 USDC
  2. 深度分析结论   /api/premium   0.05  USDC

Pact 里写死的规则：
  · 单笔上限 0.01 USDC
  · 只能付给指定商户地址（白名单）
  · 24 小时后自动失效

逐笔执行：
  ✅ /api/cheap    0.005 USDC  → 允许 → 支付成功 → 拿到数据 → 链上有交易哈希
  ⛔ /api/premium  0.05  USDC  → 拒绝 → 超单笔上限 → 链上无痕迹
  ⛔ 付给白名单外地址 0.001    → 拒绝 → 未被任何策略覆盖 → 链上无痕迹
```

第三条是**默认拒绝**（fail-closed）：任何操作必须被某条策略明确覆盖才能执行，
否则自动拒绝。这是"Agent 在物理上绕不过规则"的依据。

---

## 证据在哪

每次 `make demo` 会在 `evidence/` 下写**一个目录** `run-<时间戳>-<随机后缀>/`，
里面是 `chain.jsonl`（逐条追加、带链式哈希）、`counts.json`（独立计数器）和
`run.json`（本次运行的元信息）。`make dashboard` 取**最近一次完整运行**
（有 `run.json` 的，跳过中途失败留下的空目录）渲染成页面（http://127.0.0.1:8000）。
页面上能看到：

- 原始任务与总预算
- Pact 的 id 与状态
- 每一笔采购的金额、结果、链上交易哈希
- 被拒那笔的拒绝码、触发的策略 id、以及**证据来源**
- Cobo 的审计日志（只列策略决策）

### 两个必须如实说明的事实

这个项目在做的过程中实测出两件与"想当然"不同的事，代码和页面都按事实处理，没有粉饰：

**① 两条支付路径的拒绝码不同。**
超限的 x402 付款返回 `X402_POLICY_DENIED`（具体原因在 `details.reject_details` 里），
而普通转账返回 `ADDRESS_NOT_WHITELISTED` / `TRANSFER_LIMIT_EXCEEDED`。
代码把两条路径归一化成同一组字段，但页面**同时展示平台原始码与策略依据**，不做取舍：
平台返回什么码就显示什么码（**不改写平台的真实错误码**），旁边再给出
归一化后的 `policy_reason` 与命中的 `policy_id`。这样既能对上平台的原始响应，
也能一眼看出是哪条规则拦的。

**② Cobo 的审计日志记录转账拒绝，但不记录 x402 付款的拒绝。**
所以页面上每条拒绝都会标注**证据来源**：

| 场景 | 来源 |
|---|---|
| x402 付款被拒 | `Cobo 付款响应`（该路径审计日志不记录此拒绝） |
| 转账被拒（默认拒绝） | `Cobo 审计日志` + 审计行 id |

> **关于"不可篡改"这四个字（第 6 轮评审后再次收窄）**
>
> 直说：**这套证据不是防篡改的。** 证据是一堆本地文件；**谁能写这些文件，谁就能改内容、
> 然后重算所有哈希让文件重新自洽**。哈希链只能证明"文件没被改坏（传输完整、没被误删）"，
> 它**不能**证明"内容没被人有意改过"。
>
> 那它靠什么站得住？靠**拿本地记录去和文件夹外面的东西对账**：
>
> | 证据里的声明 | 拿什么对账（在证据包之外） | 对不对得上 |
> |---|---|---|
> | 这笔付款成功了、金额、收款方 | **Base Sepolia 链上那笔 USDC 转账**（`eth_getLogs` + 交易回执） | 对不上 ⇒ 校验不通过 |
> | 一共发生过几次尝试 | **Cobo 服务端的审计日志**（现场重查，逐字段比对） | 对不上 ⇒ 校验不通过 |
> | 这次运行的规则集是什么 | Cobo 上的 **pact 对象**（服务端持有） | 对不上 ⇒ 校验不通过 |
>
> 所以：
> - **`paid` 记录**有链上锚点。把成功擦成被拒，链上那笔转账就成了对不上的账。
> - **`denied` 记录**没有链上痕迹。它的依据是 Cobo 审计行 —— 那是服务端写的，
>   本地改不了，但**审计日志是滚动的**：滚过去之后，校验只能给"无法确认"，
>   不会给"通过"。
> - **`x402` 路径的拒绝没有外部证据**（实测：Cobo 不为它写审计行）。
>   页面会如实标注来源是"付款响应"而不是审计日志，**我们不会把它说成审计日志**。
>
> **一句话边界**：校验器能挡住"改了本地文件却没同步外部事实"，
> **挡不住"能改本地文件、又恰好能让外部事实也配合"的人**。
> 对一个跑在测试网上的 demo，这个边界够用；我们不假装它更强。
>
> `make verify` 会把链上对账和上游复核**分别打出来**，通过时也明说它证明了什么。
>
演示因此刻意**保留了那条转账拒绝**——只有它能让"拒绝也被记进审计日志"这句话
真正立得住。**我们不会把付款响应说成审计日志。**

---

## 这个项目不做什么

明确边界：这是一个最小可行演示，**不涉及**——

- 复杂的 DeFi 交易（借贷、流动性提供等）
- 多链资金调度
- 完整的 Agent 市场，或 Agent 之间的互相雇佣
- 生产级的密钥管理或企业级合规
- **真实资金**（全程 Base Sepolia 测试网）

---

## 目录结构

```
pactpay/
  config.py            链/代币标识 + .env 凭证读写（底层，探针与生产命令共用）
  pact_spec.py         PactSpec 构造函数（策略 + 完成条件）
  cobo.py              Cobo Agentic Wallet 的薄封装（REST）
  planner.py           Agent 的采购计划接口（Purchase/Plan + 确定性实现，签名固定、实现可换）
  llm_planner.py       **Agent 的大脑**：DeepSeek 自己决定买哪几个接口（prompt 里不给单笔上限）
  tools.py             Agent 能调用的工具（查目录/查余额/购买/记账/交付）—— 花钱只有 purchase 一个口
  agent_loop.py        **Agent 主循环**：模型每一步自己调工具、看结果、再决定下一步
  agent_run.py         `make agent` 的入口：跑一轮自主采购，把工具流水写进证据
  agent.py             x402 采购执行；拒绝归一化（denied / failed 分开）
  preconditions.py     运行前置检查（余额不足时明确报错，不许跑出残缺证据）
  pact_check.py        Pact 提交与回读一致 + status 生效检查（make pact）
  block_evidence.py    签名前拦截的三重证据（结构化拒绝/审计佐证/无链上痕迹）
  onchain.py           Base Sepolia RPC 复核：解 USDC Transfer 事件、核对 from/to/value
  cumulative_check.py  累计上限拒绝检查（make cumulative-check）
  complete_check.py    完成条件（tx_count/time_elapsed/amount_spent）检查（make complete-check）
  demo.py              演示主流程（make demo）
  payee/app.py         收款方：两个付费 API（make payee）
  payee_lifecycle.py   一条命令里自起自停收款方
  dashboard/app.py     证据展示页（make dashboard）
  evidence/            证据落盘、计数与校验（make verify）
  spike/
    t0_probe.py        T0 探针：凭证/钱包/地址 + 超限拒绝（零资金可跑）
    a0e_probe.py       A0e 探针：x402 付款全链路 + 链上复核
tests/                 单测：含策略判据、AST 结构守卫、损坏容忍、变异自检清单
evidence/              每次运行的证据（不进 git）
progress/              开发进度账本（不进 git）
```

---

## 依赖

```
# 注意：**不用** cobo-agentic-wallet SDK —— 本项目直接走 REST（httpx），
# 一行都没 import 它。详见 requirements.txt 里的说明。
x402[fastapi,evm]==2.25.0      # x402 协议（收款方中间件 + EVM 机制）
```

只有 `x402` **钉死版本**——它在快速迭代期，不钉版本等于把演示建在流沙上。
`httpx` / `pytest` / `python-dotenv` 用下限约束，它们没有同类的破坏性演进风险。
