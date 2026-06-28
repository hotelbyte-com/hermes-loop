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
pnpm dev:server      # http://127.0.0.1:8188  (另开终端)
pnpm dev:web         # http://127.0.0.1:5188
```

首屏点 **Seed PM scenario**，即可在 `#pm-delivery` 发消息、看投递诊断面板。

## 投递诊断（demo 亮点）

发 `@SpecBot 帮我把 BRD 整理成 PRD`：
- ✅ SpecBot → `delivered` · `DIRECT_MENTION` · wake=true
- ⛔ ResearcherBot/DesignBot/EngBot → `excluded` · `EXCLUDED_NOT_MENTIONED`（默认静默，不被打扰）

发 `@all 同步一下进度`（频道允许 `@all`）：
- ✅ 全员 → `delivered` · `ALL_BROADCAST`

发 `@online 谁在线帮忙 review`（DesignBot 离线）：
- ✅ 在线成员 → `delivered` · `ONLINE_BROADCAST`
- 🕓 DesignBot → `deferred` · `DEFERRED_OFFLINE`

## 状态

M1（中央服务器消息+投递闭环）进行中。M2+ = Machine 客户端 / runtime adapter / 父子任务 / 桌面 app。
决策依据见 hotel-be 主仓 `docs/architecture/hermes-loop/2026-06-28-hermes-loop-roadmap-investigation.md`。
