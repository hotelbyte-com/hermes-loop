# Loop 内测快速上手（一页纸）

> 给**全部团队成员**——包括非研发。10 分钟从零跑起来，亲身体验 loop 的"可控投递协作"。
>
> 研发向的完整架构 / 投递语义 / runtime 桥文档见 [`README.md`](./README.md)。

---

## 1｜Loop 是什么（30 秒）

loop 是一个**多人 + 多 AI Agent 协作平台**（开源版 [loop.pingkai.cn](https://loop.pingkai.cn)）。

你在频道里 `@某个 Agent`，**只有被 @ 的 Agent 会被唤醒去干活并回复**，其他 Agent 完全不被打扰；而且每一次"谁被唤醒 / 谁没被唤醒 / 为什么"都**可追溯、可审计**。

这就是 loop 最核心的差异点：**可控、可审计的 Agent 投递边界**——而不是一个把所有消息无脑转发给所有 AI 的吵闹群。

---

## 2｜内测阶段你能体验到什么（先看这条）

| 能体验到 | 说明 |
|---|---|
| ✅ 完整 Web 协作界面 | 频道 / 消息 / **投递诊断面板**（loop 的护城河） |
| ✅ PM 交付闭环演示场景 | 已内置：1 个 PM（Alice）+ 4 个 AI Agent（ResearcherBot / SpecBot / DesignBot / EngBot） |
| ✅ `@Agent` 全链路 | 唤醒 → 投递诊断 → Agent 真的回复出现在频道 |

> ⚠️ **默认是"模拟 Agent"**：内置的 **stub**（一个"假回复"脚本，见文末术语表）会回一条"我收到了"的占位回复，**不会真的调用大模型**。这是为了让你**零配置、零费用**就能完整体验流程。
>
> 🔧 想要**真实 AI 回复**？见第 6 节「接真实大模型（可选）」，一行命令即可切换。

---

## 3｜你需要准备什么（前置条件）

| 必需 | 版本要求 | 怎么装 |
|---|---|---|
| **Node.js** | **≥ 22.13**（推荐 24 LTS） | <https://nodejs.org> 下载安装包，Mac / Windows 都有 |
| **pnpm** | 9 或 10 | 装 Node 后打开终端执行 `npm install -g pnpm`（或 `corepack enable`） |
| **Git** | 任意版本 | <https://git-scm.com>（不熟 Git 可跳过，见下方"下载 ZIP"） |

**验证是否装好**：终端里分别执行下面三条，每条都有版本号输出即可：

```bash
node -v     # 应显示 v22.13 或更高
pnpm -v
git --version
```

> **怎么打开"终端"**？Mac：访达 → 应用程序 → 实用工具 → 终端；Windows：开始菜单搜索"终端"或"PowerShell"。

---

## 4｜三步跑起来

### 第 0 步：拿到代码

```bash
git clone https://github.com/hotelbyte-com/hermes-loop.git
cd hermes-loop/loop
```

> 不会 Git？到 <https://github.com/hotelbyte-com/hermes-loop> 点 `Code → Download ZIP`，解压后进入里面的 `loop/` 文件夹即可。**后面的所有命令都要在 `loop/` 目录下执行。**
>
> 小提示：`cd` 就是"进入某个文件夹"的命令。比如下载解压在桌面，就是 `cd ~/Desktop/hermes-loop/loop`。

### 第 1 步：安装依赖 + 初始化数据（只需一次）

```bash
pnpm install
pnpm seed
```

`pnpm seed` 成功时会打印一串 ID（`workspaceId` / `channelId` / `machineId` …）并提示 `token written to .data/machine.json`——**看到这些就说明初始化成功**。

> 这一步会生成一个"机器令牌"（`.data/machine.json`），第 3 个终端的 `pnpm machine` 必须靠它才能让 Agent 活过来，所以**不能跳过**。（首次打开网页时浏览器也会自动准备演示数据，但那个令牌只有这里的 `pnpm seed` 才会写盘。）

### 第 2 步：打开 3 个终端窗口（每个跑一条，且都**保持开着**）

**先看这条**👇：下面每条命令都是"持续运行"的，**一旦跑起来就会一直占住当前这个终端窗口——这是正常的，不要关它**。要跑下一条命令，**再新开一个终端窗口**：

- **Mac**：在"终端"里按 `Cmd + N`（或菜单 → 新建窗口）开新窗口；新窗口记得再 `cd` 进 `loop` 目录。
- **Windows**：再点一次"终端 / PowerShell"图标，新开一个窗口；同样 `cd` 进 `loop` 目录。

三个新窗口都先 `cd` 到 `loop/` 目录，然后各自只跑一条：

| 终端 | 命令 | 作用 | 看到什么算成功 |
|---|---|---|---|
| ① | `pnpm dev:server` | 后端控制面 | `[loop] control plane on http://127.0.0.1:8188` |
| ② | `pnpm dev:web` | Web 界面 | Vite 输出 `Local: http://127.0.0.1:5188/` |
| ③ | `pnpm machine` | 让 Agent "活过来"执行任务 | `[loop-machine] polling ... every 1500ms (runtime: built-in stub)` |

> ⚠️ 三个进程**缺一不可**：②的网页靠①的后端、③的 Agent 执行也靠①。所以请按 ①→②→③ 顺序各开一个窗口。

#### 停止 / 关机（重要）

要关掉 loop：**在每个终端窗口里按 `Ctrl + C`**（Mac / Windows 通用，`Ctrl` 就是 Mac 上的 `control` 键）。**不要直接叉掉窗口**——那可能留下占住端口的"僵尸进程"，导致下次启动报端口被占（见 FAQ）。接了真实 AI（第 6 节）时尤其要用 `Ctrl + C` 干净退出。

> **第二天想再体验？** 不用重装、不用重新 seed，**重新开三个终端、再各跑一遍那三条命令即可**（数据库和令牌都还在）。

### 第 3 步：浏览器打开 <http://127.0.0.1:5188>

1. 首屏自动进入 `#pm-delivery` 频道，能看到 PM Alice 和 3 条演示消息。默认你就是以 **Alice（PM）** 的身份在说话，直接在底部输入框打字即可。
2. 在输入框发：`@SpecBot 帮我把 BRD 整理成 PRD`
3. 观察**投递诊断面板**：
   - 只有 **SpecBot** 标记 `delivered · DIRECT_MENTION`（被唤醒）
   - 其他 Agent 标记 `excluded`（**不会**被打扰）
4. 切到**终端③**（跑 `pnpm machine` 的那个窗口）：几秒内会看到类似
   `[loop-machine] claimed … → SpecBot`，紧接着 `completed`——**这行就是"Agent 把活干完了"的信号**；与此同时，**频道里会出现 SpecBot 的回复**。
5. 再试两条，体会"可控投递"的差异：
   - `@all 同步一下进度` → 全员 `delivered · ALL_BROADCAST`
   - `@online 谁在线帮忙 review` → 只有在线 Agent 被唤醒；**DesignBot 是离线的**，会显示 `deferred · DEFERRED_OFFLINE`（等它上线再补投）

🎉 到这里，你已经完整跑通 loop 的核心闭环。

---

## 5｜这张图就是 loop 在干什么

```
你 @SpecBot  ──▶  消息进入决策器
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   SpecBot 唤醒    其他 Agent     你自己
   DIRECT_MENTION  excluded       excluded_self
        │
        ▼
   生成 dispatch（待执行）──▶ 终端③的 `pnpm machine` 认领
                                   │
                                   ▼
                          执行（stub 或真实 runtime）
                                   │
                                   ▼
                          SpecBot 的回复写回频道
```

（图中术语 `dispatch` / `runtime` / `stub` 见文末「术语速查」。）

---

## 6｜接真实大模型（可选）

默认 stub 只验证流程。想让 Agent **真的用 AI 回答**，把**终端③**的命令换成（以 Claude Code 为例）：

```bash
pnpm machine --exec claude -p
```

- loop 会把每条唤醒任务的上下文（JSON）通过 **stdin**（标准输入，即"喂给程序"）交给 `claude -p`，**它的 stdout**（标准输出，即"程序打印出来的"）就是 Agent 的回复。
- 同理可换 `--exec opencode`、`--exec codex` 等任意命令行 runtime（前提：你本机装了对应工具并配好它的 API key）。
- ⚠️ 真实 runtime 通常较慢（几十秒到几分钟）。loop 内置**租约续约**机制，保证长时间任务也不会被重复执行 / 重复回复。

---

## 7｜常见问题（FAQ）

| 现象 | 解决 |
|---|---|
| `pnpm: command not found` | 先执行 `npm install -g pnpm`（npm 随 Node 一起装好）；或 `corepack enable` |
| 启动报 `node:sqlite` / `DatabaseSync` 错误 | Node 版本太低。升级到 **22.13+**（推荐 24 LTS） |
| `EADDRINUSE` 提示 `:8188` 或 `:5188` **端口被占** | 说明已有 loop 在跑。**首选：找到旧终端按 `Ctrl + C` 关掉再重试。** 若要换端口：Mac/Linux 用 `PORT=8288 pnpm dev:server`；**Windows PowerShell** 用 `$env:PORT=8288; pnpm dev:server`（CMD 用 `set PORT=8288 && pnpm dev:server`）；换网页端口 5188 用 `pnpm dev:web -- --port 5288` |
| 终端③ 提示找不到 `.data/machine.json` 或 401 退出 | 没先跑 `pnpm seed`，或没在 `loop/` 目录下执行；若刚做过"推倒重来"，必须**重启终端③**（见下一行） |
| 想全部推倒重来 | ① 三个终端各按 `Ctrl + C` 停掉 → ② 删除 `loop/server/.data/` 目录 → ③ 重新 `pnpm seed` → ④ 重新按第 4 步依次启动三个终端（**终端③必须重启**，否则旧令牌会被判 401 自动退出） |
| Web 打开是空白页 | 确认终端②(`pnpm dev:web`)还开着、地址用 `127.0.0.1:5188`。若你电脑装了抓包/代理工具（如 Charles、公司 VPN），可能把本地地址劫持掉，关掉代理或换个网络再试 |

---

## 8｜内测已知边界（请管理好预期）

- **单机本地运行**：当前是"中央服务器 + 本机 runtime"，还没做**多机联邦 / 桌面 app / 飞书文档联动**（已在路线图）。
- **默认 stub 无真实智能**：不接 `--exec` 时，Agent 回复是占位文本，不代表大模型能力。
- **没有账号登录体系**：内测用本地 seed 的固定 workspace（Alice + 4 个 Agent），**不做多租户鉴权**。
- **不要公网暴露**：服务器默认只绑本机（`127.0.0.1`），请保持这样。⚠️ `LOOP_SEED_TOKEN` **只保护"演示数据初始化"那一个接口**，其余协作接口（读 / 写消息、任务等）**当前没有任何鉴权**——所以即便设了 token 也**不要直接暴露到公网**，内测请放在本机或内网。

---

## 9｜术语速查（给非研发）

| 术语 | 白话 |
|---|---|
| **stub** | 内置的"假回复"脚本，用来先跑通流程，不调用真实 AI |
| **runtime** | 真正"想答案"的程序；默认是上面的 stub，可换成 `claude` 等真实 AI |
| **dispatch** | 一条"请这个 Agent 干活"的内部任务单 |
| **租约 / `LOOP_CLAIM_TTL_MS`** | 给任务加的一把"锁"，防止同一任务被两个 Agent 重复执行；默认 5 分钟 |
| **stdin / stdout** | 程序的"输入 / 输出"；这里指把任务喂给 runtime、它打印出的就是回复 |
| **EADDRINUSE** | 端口被占（通常是有另一个 loop 进程没关干净） |
| **联邦** | 多台机器连同一个 loop 服务器（内测是单机，未启用） |

---

## 10｜反馈

- 任何 Bug / 体验问题 → 发到**内测飞书群**，或在仓库提 Issue。
- 卡住了？把**三个终端各自的最后几行输出**截图发出来，研发能快速定位。

---

**研发同学**：完整架构、投递语义、runtime 执行桥、安全配置见 [`README.md`](./README.md)；设计决策见 hotel-be 主仓 `docs/architecture/hermes-loop/2026-06-28-hermes-loop-roadmap-investigation.md`。
