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
make revoke-check   # 人类随时能收权：撤销后同一个 key 立刻什么都干不了（不花钱）
make events         # 实时告警通道：SSE 推送 + 轮询审计日志两条路都试，如实报结论
```

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

## 凭据是怎么放的（谁拿着什么）

这一节把"**这个 Agent 到底握着什么凭据**"写清楚——因为这决定了它被攻破时
最坏能发生什么。**先说结论：这里没有私钥，Python 代码也从不读私钥。**

### 三样东西，放在三个地方

| 东西 | 在哪 | 谁能读到 |
|---|---|---|
| **API key**（`AGENT_WALLET_API_KEY`） | 项目目录的 `.env`（已 gitignore） | **Agent 进程**。它是 bearer 凭据 |
| **MPC 密钥分片** | `~/.cobo-agentic-wallet/profiles/<profile>/tss-node/db/secrets.db` | **本机 TSS 节点进程**，不是 Agent |
| **分片库的口令** | 同目录下的 `.password`（明文文件） | 能读那个目录的人 |

钱包是 **MPC 2-of-2**：本机 TSS 节点持一片，Cobo 持另一片，**两片都在才能签名**。
本机常驻的签名进程长这样（`ps` 实况）：

```
cobo-tss-node start --caw --prod \
  --key-file .../tss-node/.password \
  --db       .../tss-node/db/secrets.db
```

Python 那边**一行都不碰私钥**——`grep -riE "private_key|keystore|mnemonic"` 在
`pactpay/` 下**零结果**。Agent 只会拿着 API key 发 HTTP。

### 那 Agent 被攻破会怎样

攻击者拿到 `.env` 里的 API key，能做的事**被两层夹住**：

1. **API key 是 pact-scoped 的，不是根 key**。它只能在这个 Pact 允许的范围内动钱
   （单笔 ≤ 0.01、收款方白名单内、24h 内）。这一点在本项目里是**实测**的：
   用根 key 走付款路径会被拒（`INSUFFICIENT_PERMISSION`），必须换上
   `get_pact(pact_id)["result"]["api_key"]` 才通。
2. **24 小时到期**。过期后这个 key 什么都干不了。

**所以这个项目的安全模型不是"藏好私钥"，是"就算凭据泄露，它能造成的损失也被
平台侧的策略封顶了"。** 这正是它想演示的那件事。

### 一处实测与文档不符（记在案）

官方文档 `developer/audit.md` 有一节 "Real-time events (SSE)"，写的是：

```
GET {API_URL}/api/v1/events/stream
Headers: X-API-Key: OWNER_API_KEY, Accept: text/event-stream
```

**实测这个路由在当前部署上不存在**：

| 请求 | 结果 |
|---|---|
| 带 agent key | **404** |
| **不带任何 key** | **404**（⇒ 不是权限问题，是路由没有） |
| 带尾斜杠 / `/api/v1/events` / `/audit-logs/stream` | 全部 404 |
| **对照**：`GET /api/v1/audit-logs?limit=1` | **200** ✓ |

所以"平台推事件给我们"这条**实时告警**通道目前用不了。
`make events` 会把这件事如实打出来，然后**退回轮询审计日志**（那条路是通的，
实测 2 秒内就能看到新的拒绝行）。

**但轮询比推送弱**：有延迟，而且进程挂了就没人看着。它是"近实时"，不是"实时"。
两者共同的关键性质是**都不是 Agent 自己能造的** —— 这一条比 push/poll 的区别更重要。

### 2-of-2 挡住了什么、挡不住什么

| 情况 | 2-of-2 管用吗 |
|---|---|
| 只偷到本机的密钥分片 | ✅ **管用**——还差 Cobo 那片，签不出名 |
| 只偷到 Cobo 那边的分片 | ✅ **管用**——同上，反过来 |
| **整个本机被读**（分片 + 旁边的明文口令） | ❌ **不管用**——两样在同一目录里 |
| **只偷到 API key** | ⚠️ 靠上面的两层夹住，**不是靠 2-of-2** |

**如实说明**：分片库的口令 `.password` 是明文文件，就放在 `secrets.db` 旁边。
所以"本机被完全读取"这一档，MPC 保护不了——**这一点我们不做没有的承诺**。

要真正加固这一档，得把 TSS 节点挪到独立 OS 用户或独立主机上。那是**运维层面**的事，
而"生产级密钥管理"在本项目的**非目标清单**里（见下一节）。

### 撤销：人类随时能收权

权限不是只能等到期——平台提供了撤销：

```bash
make revoke-check    # 撤销 Pact → 立刻重试同一笔 → 确认被拒
```

这是"边界是硬性的"最直接的证明：**撤销之后，同一个 API key、同一个钱包、
同一个请求，立刻什么都干不了**，而且这发生在**平台侧**，不是 Agent 自己让着自己。

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
pactpay/                    按职责分子包，目录名就是职责
  platform/                 平台与链的底层：不掺业务判断
    config.py                 链/代币标识 + .env 凭证读写（标识只定义这一次）
    cobo.py                   Cobo Agentic Wallet 的薄封装（REST，手写）
    onchain.py                Base Sepolia RPC 读链：解 USDC Transfer、核对 from/to/value

  policy/                   人类划的边界
    spec.py                   PactSpec 构造（策略 + 完成条件）
    check.py                  Pact 提交与回读一致 + status 生效检查（make pact）

  agent/                    Agent 自己：决策 + 执行
    loop.py                   **主循环**：模型每一步自己调工具、看结果、再决定下一步
    tools.py                  模型能调的 5 个工具 —— 花钱只有 purchase 一个口
    planner.py                采购计划接口（签名固定、实现可换）
    llm_planner.py            **大脑**：DeepSeek 决定买哪几个（prompt 里不给单笔上限）
    run.py                    make agent 入口：跑一轮自主采购，工具流水写进证据
    pay.py                    x402 采购执行 + 重试（复用同一 request_id）
    preconditions.py          花钱前的余额检查（不许跑出残缺证据）

  evidence/                 留下能核对的东西
    __init__.py               哈希链 / 计数 / 锚 / 记录构造
    verify.py                 make verify：链 + 锚 + 链上对账 + 上游复核
    block.py                  签名前拦截的三重证据（结构化拒绝/审计佐证/无链上痕迹）

  governance/               人类收权的通道（都在平台侧，Agent 绕不过）
    revoke.py                 make revoke-check：撤销后同一 key 立刻失效
    events.py                 make events：实时告警通道检查（SSE 不可用则退回轮询）

  checks/                   独立探针式检查（各建一份临时 Pact）
    complete.py               完成条件自动终止（make complete-check）
    cumulative.py             24h 累计上限被触发（make cumulative-check）

  demo/                     给人看的那一层
    run.py                    make demo 主流程
    payee_lifecycle.py        一条命令里自起自停收款方
    demo/payee/app.py         收款方：两个付费 API（make payee）
    demo/dashboard/app.py     证据展示页（make dashboard）

  spike/                    零资金可行性探针
    t0_probe.py               凭证/钱包/地址 + 超限拒绝
    a0e_probe.py              x402 付款全链路

tests/                      单测：策略判据、AST 结构守卫、损坏容忍、变异自检清单
evidence/                   每次运行的证据（不进 git）
progress/                   开发进度账本（不进 git）
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
