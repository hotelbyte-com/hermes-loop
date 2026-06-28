# Loop — 可控投递协作平台（开源版 loop.pingkai.cn）

> Central control plane for `loop.hotelbyte.com`.
> 护城河 = **可控投递边界 + 投递诊断**（MessageDelivery 全审计）。普通消息不群发、`@all`/`@online` 受控、私有上下文不扩散，且**每一次投递命中/排除都可追溯**。

这是 `hotelbyte-com/hermes-loop` 仓内的 **Loop 产品**目录。仓内其余 Python 代码（`agent/` 等）是 hermes-agent 上游，按 D-021 视为**可选 runtime 之一**，不在本产品核心路径上。

## 三层核心抽象（D-020）

| 层 | 含义 | 迁移性 |
|---|---|---|
| **Soul** | 可迁移的角色资产（SOUL.md + skills + knowledge） | 稳定，跨 workspace 迁移 |
| **Agent** | workspace 内的协作角色（被 `@` 的对象） | 稳定，绑定 workspace |
| **Instance** | Agent 在某 Machine 上的运行实例（runtime + 在线状态） | 运行位置，未来可迁移 |

`Machine` = Instance 宿主，不是身份来源。

## 架构

```
loop/
  server/   中央控制面：API + 投递决策引擎（node:sqlite, Hono）
  web/      协作面 UI：频道/消息/投递诊断面板（React + Vite）
```

- **runtime 外部化（D-021）**：Loop 只负责调度与投递决策，真正执行工作的 runtime（Claude Code / OpenCode / Codex / GLM / hermes-agent）跑在 Machine 本地。
- **中央服务器拓扑（D-012）**：`loop.hotelbyte.com` = 元数据 + 调度 + 指令队列 + 审计；Machine 本地存 Agent 状态全量，控制台只读脱敏视图（D-011）。

## 快速开始

```bash
cd loop
pnpm install
pnpm seed          # 建库 + PM 场景 + 颁发 machine token（写入 .data/machine.json）
pnpm dev:server    # http://127.0.0.1:8188  (另开终端)
pnpm dev:web       # http://127.0.0.1:5188
pnpm machine       # (再开一终端) runtime 桥：轮询 dispatch，把 wake 消息真正跑起来
```

Web 首屏自动 seed `#pm-delivery`。`@SpecBot` 会生成一条 `⏳ 待执行` 的 dispatch；`pnpm machine` 一跑，runtime 认领并回写真实 agent 回复（诊断面板变 `✅ 已回写`）。

## 投递诊断（护城河）

发 `@SpecBot 帮我把 BRD 整理成 PRD`：
- ✅ SpecBot → `delivered` · `DIRECT_MENTION` · wake + `⏳ 待执行`（生成 dispatch，待 runtime 认领）
- ⛔ ResearcherBot/DesignBot/EngBot → `excluded` · `EXCLUDED_NOT_MENTIONED`（默认静默，不被打扰）

发 `@all 同步一下进度`（频道允许 `@all`）：
- ✅ 全员 → `delivered` · `ALL_BROADCAST`

发 `@online 谁在线帮忙 review`（DesignBot 离线）：
- ✅ 在线成员 → `delivered` · `ONLINE_BROADCAST`
- 🕓 DesignBot → `deferred` · `DEFERRED_OFFLINE`

## Runtime 执行桥（M2，D-024）

runtime **外部化**（D-021）：中央服务器只调度投递、**永不执行 runtime**。一条 wake 到 agent 的投递会落成一条 `dispatch`（目标=该 agent），任意托管了该 agent **在线 instance** 的 Machine 都可轮询/认领/完成。`complete` 带 `replyBody` 时，runtime 输出经同一条 `postMessage` 临界路径回写为 agent 消息——回复会**再次进入决策器**，其投递诊断与原始 wake 串联可审计。

```
人 @SpecBot → message + delivery(wake) + dispatch(pending)
                                     ↓ machine 轮询（结构化：该机器有此 agent 的在线 instance）
                               claim (CAS pending→claimed)
                                     ↓ runtime 本地执行（claude-code / opencode / 任意命令 / 内置 stub）
                              complete(replyBody) → agent 回写消息 → 自身 delivery 诊断
```

- **接入真实 runtime**：`pnpm machine --exec claude -p`（payload JSON 走 stdin，stdout 即回复）。默认内置 stub，零依赖可演示桥。
- **离线队列**：直接 `@` 一个离线 agent → dispatch 进入 `pending` 等待；把该 agent 的 instance 置 `online=true`（`POST /api/workspaces/:wid/agents/:aid/instances`）后，下次轮询即被认领执行。
- **Machine 鉴权**：`POST /api/workspaces/:wid/machines` 颁发 opaque token（仅存 sha256）；dispatch 相关端点走 `Authorization: Bearer <token>`。

## 状态

**M1**（中央服务器消息+投递闭环）✅ 已交付。
**M2**（runtime 执行桥：dispatch 生命周期 + machine/instance 注册 + 参考机器客户端）✅ 已交付。
M3+ = 桌面 app（复用 hermes-workspace）/ 父子任务 / 飞书文档协同 / 多机联邦。
决策依据见 hotel-be 主仓 `docs/architecture/hermes-loop/2026-06-28-hermes-loop-roadmap-investigation.md`（D-024）。
