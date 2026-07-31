# Loop — 可控投递协作平台（开源版 loop.pingkai.cn）

> Central control plane for `loop.hotelbyte.com`.
> 护城河 = **可控投递边界 + 投递诊断**（MessageDelivery 全审计）。普通消息不群发、`@all`/`@online` 受控、私有上下文不扩散，且**每一次投递命中/排除都可追溯**。

> 👥 **非研发同学请直接看 [《内测快速上手（一页纸）》](./quickstart.md)** —— 10 分钟从零跑起来，无需任何前置知识。
> 本文件（README.md）是面向**研发**的架构 / 投递语义 / runtime 桥完整文档。

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
- **Lease 续约（M3）**：`pnpm machine` 在 runtime 执行期间周期性 `POST /api/dispatches/:id/renew` 续约 claim，使长 runtime（claude -p 可能数分钟）不会被 `requeueStaleClaimed` 在 `LOOP_CLAIM_TTL_MS` 后重置给另一台 machine——避免重复 agent 回复。续约是 CAS（仅当前 claimer、仍 claimed）；lease 已丢则停止续约，runtime 自然结束、`complete` 409 丢弃，**不产生重复回复**。
- **实例存活回收（M3，R-008）**：轮询即存活信号——`pollDispatches` 先刷新调用方 machine 的在线 instance 的 `last_seen_at`，再回收 `LOOP_INSTANCE_TTL_MS`（默认 ≥ 2× claim TTL）内无心跳的 instance 为 offline。machine 崩溃后其 instance 最终 offline，其 pending dispatch 由另一台托管该 agent 在线 instance 的 machine 接管（eventual consistency）。纯 SQL 结构化事实（`online=1 AND last_seen_at<cutoff`），不引入行为路由。

## 父子任务（M3 §D.2.3 W7）

`task` 表（`parent_task_id` / `thread_id` / `assignee_id`+`assignee_kind` / `status`）已接线为最小可用：

- `POST /api/workspaces/:wid/tasks` 建任务（可选 `parentTaskId` / `threadId` / `assigneeId`+`assigneeKind`）
- `GET /api/workspaces/:wid/tasks[?threadId=|?parentId=]` 列表；`GET .../tasks/:id/children` 直系子任务
- `PATCH /api/workspaces/:wid/tasks/:id/status` 状态机 `open→in_progress→done|cancelled`（CAS，非法转换 409）

task 是轻量工作项，**与 message-driven dispatch 解耦**（建任务/改状态不产 dispatch、不走投递决策器）。所有访问按 workspace 结构化隔离（`id=? AND workspace_id=?`），跨 workspace 404 不泄漏存在性。

## 安全配置（demo-only seed gate）

`POST /api/seed/pm-scenario` 是 demo 端点（建场景 + 颁 machine token）。鉴权锚定在**服务端配置的 bind host**，而非可伪造的客户端 `Host` 头：

- 服务器默认绑 `127.0.0.1`（`LOOP_BIND_HOST` 未设时）→ seed 端点对本地放行（web 首屏自动 seed 仍工作）。
- 若 `LOOP_BIND_HOST=0.0.0.0`（对外暴露）**必须**同时设 `LOOP_SEED_TOKEN`，否则 seed 端点直接 404（隐藏存在性）；带 token 时走 `timingSafeEqual` 常量时间比较。
- `pnpm seed`（`cli/seed.ts`）直接调 `seedPmScenario`，不经 HTTP，本地 dev 零配置可用、不受 gate 影响。

## 状态

**M1**（中央服务器消息+投递闭环）✅ 已交付。
**M2**（runtime 执行桥：dispatch 生命周期 + machine/instance 注册 + 参考机器客户端）✅ 已交付。
**M3**（本轮切片）✅ 已交付：真实 lease renewal（exec 期间心跳续约，杜绝重复回复）/ instance 存活回收（多机接管）/ `/api/seed` gate / 父子任务接线。
M3+ 待办：桌面 app（复用 hermes-workspace）/ 飞书文档协同（需 transport ADR）/ 深度多机联邦（多 region + R-009 灾备，需 ADR）。
决策依据见 hotel-be 主仓 `docs/architecture/hermes-loop/2026-06-28-hermes-loop-roadmap-investigation.md`（D-024 / D-025）。
