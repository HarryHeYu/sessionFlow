# 路线图：Continuity Engine（跨 Agent 接续层）

> Voyager compiles scattered agent histories into the context the next agent actually needs.
>
> Voyager 把散落在各 Agent 里的历史，编译成下一个 Agent 真正需要的上下文。

**状态：** 规划（本文不包含实现）。
**英文版：** [ROADMAP.md](ROADMAP.md)

Voyager 现在是机器上所有 AI 编码 Agent 的统一**索引**。
下一层是 **Continuity / Context Orchestration**：不只是「以前聊过什么」，
而是「把多个 Agent、多个 Session 的工作状态重新组织起来，让另一个 Agent 接着干」。

```
Search past work, merge context across sessions, and continue seamlessly in any agent.
搜索过去的工作，跨会话合并上下文，在任意 Agent 里无缝接续。
```

这比继续加 Adapter、TUI 或搜索更值得做。索引是地基，编译器才是壁垒。

---

## 定位

| 现在 | 下一阶段 |
|---|---|
| 本机所有 AI 编码 Agent 的可搜索索引 | 所有 AI 编码 Agent 之上的接续层 |
| 以 Session 为中心（`handoff <id>`，`continue` = 最新一条） | 以任务为中心（由多个 Session 组成的 `WorkThread`） |
| 对话摘要（截断 + 拼接） | 上下文合成（排序、去重、冲突消解、预算） |
| 「无缝迁移 Session」（实际做不到） | 无缝的**工作接续**（work continuation） |

跨 Agent 无法搬运 system prompt、隐藏 tool state、cached reasoning、
provider 运行时状态。Voyager 不会声称能做到 session migration。
用户侧可以非常无缝——一条命令：

```
voyager switch codex
```

找到当前 thread，抽取状态，检查 git，写出 Continuation Bundle，
启动 Codex，让它读 bundle 接着干。

---

## 已经有的地基（不要推倒重来）

| 层 | 现状 |
|---|---|
| Adapter | 8 家 provider → 归一化 `Session` / `Event`（`voyager/model.py`） |
| Index | SQLite + FTS5 trigram（`voyager/store.py`），幂等扫描 |
| 单 Session handoff V1 | `voyager/handoff.py`：一个会话 → Markdown 上下文包 → 拉起 `claude` / `codex` / `grok` |
| Continue V1 | 最新会话；能原生 resume 就 resume，否则 handoff |
| MCP | `brief` / `search` / `list` / `show` / `handoff` |
| 约束 | provider 文件只读；无网络；无遥测；core 零依赖 |

Handoff V1 已经抽出：原始请求、后续指令、最后一条助手消息、碰过的文件、
命令、错误、压缩时间线；用**文件路径**注入（D8），不把全文塞进 argv。

缺口：它是 **session-centric**。真实工作往往是：

```
Voyager 项目
 └── 提升 adapter 可靠性
      ├─ Codex A   「分析 adapter」
      ├─ Claude B  「修 DSH adapter」
      ├─ Codex C   「测 CI」
      └─ Grok D    「下一步架构」
```

Voyager 现在看到四条 Session。下一版应该开始看到**一个 WorkThread**。

---

## 原则

1. **工作接续，不是 Session 搬迁。** 同平台原生 resume 仍然优先（D7）。
   跨平台是「新 Session + bundle」，不是伪造同一个 Session。
2. **面向下一步目标的抽取，不是倒历史。** bundle 服务于 *接下来要干什么*。
   `--goal "fix CI"` 应留下 pytest / Actions / 失败，丢掉 UI 讨论。
3. **Pointer over prose。** 能从 git / 文件系统重读的，就引用路径、commit、
   测试名，不要把正文复制进上下文。吸收成熟 handoff skill 的做法
   （如 [jumpifequal/handoff-skill](https://github.com/jumpifequal/handoff-skill)、
   Matt Pocock 的 `handoff`）。
4. **合成，不是拼接。** A 提出方案 X，B 发现 X 有问题改成 Y，C 已经把 Y
   实现了——直接 concat 会让下一个 Agent 再看见已废弃的 X。bundle 必须记录
   `X → Y（已被替代）` 并带出处。
5. **每条断言都有 provenance。** Decision / state / failure 标明来源
   session id，能验证的再挂上 commit 或通过的测试。
6. **核心编译器必须确定性、离线。** Continuity V1 只对已归一化的索引做
   抽取和排序。不调用 LLM，不联网（FAQ + CONTRIBUTING）。
   *读 bundle 的那个目标 Agent* 才是推理者。LLM 精炼以后可以做成可选 extra，
   永不进入 core 依赖。
7. **核心只有一份，前端可以有多个。** CLI / MCP / Skill / UI 都调同一套
   编译器。不要分别做 Claude / Codex / Cursor 插件各写一遍 merge。
8. **UI 放最后。** 侧边栏只是展示。真正形成壁垒的是下面这条 pipeline。

---

## 编译管线

```
原始历史（多 Session、多 Provider）
      ↓  1. select     按 repo / 时间 / 文件 / 分支 / 目标筛选
      ↓  2. extract    带指针的事实（不是作文）
      ↓  3. dedup      同一文件、同一命令、同一错误
      ↓  4. overlay    时间顺序冲突：后者为准，前者标 superseded
      ↓  5. pointers   能重读的改成路径 / commit / 测试名
      ↓  6. budget     Full / Balanced / Compact（或 N tokens）
      ↓  7. bundle     Continuation Bundle（Markdown，D8）
      ↓  8. launch     原生 resume  XOR  新 Session +「读这个文件」
```

用户层面叫 **Context Budget**，不要叫 compression。

---

## 对象模型：Session → Thread → Continuation

```
Project (repo_root)
 └── WorkThread          # 新增的逻辑对象
      ├─ goal, status, branch
      ├─ Session (codex A)
      ├─ Session (claude B)
      └─ Continuation Bundle   # 编译视图，不是另一份历史
           └── Event 仍在 session 表里
```

`WorkThread` 是加法。Session / Event 形状不变。
建议表结构（Phase 2；merge 的第一刀还不需要）：

```sql
CREATE TABLE threads (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    repo_root     TEXT,
    git_branch    TEXT,
    goal          TEXT,
    status        TEXT,          -- active | parked | done
    created_at    REAL,
    updated_at    REAL,
    metadata_json TEXT
);
CREATE TABLE thread_sessions (
    thread_id  TEXT NOT NULL,
    sid        TEXT NOT NULL,
    added_at   REAL,
    PRIMARY KEY (thread_id, sid)
);
```

`continue --repo` 的自动聚类信号（不用 LLM）：

| 信号 | 权重 |
|---|---|
| 同一 `repo_root` | 必需（否则 cwd 兜底） |
| 时间窗（默认 48h） | 高 |
| 同一 `git_branch` | 高 |
| 碰过的文件有交集 | 高 |
| 标题 / 最近用户目标的 FTS 词重叠 | 中 |
| 同一 provider | 低（跨 Agent 才是重点） |

显式的 `voyager merge A B C` 永远压过自动聚类。

---

## Continuation Bundle（目标结构）

用结构化包替换现在扁平的 handoff Markdown。
启发式 V1 从索引 + git 填每一节，**不编故事**。

```markdown
# Continuation Bundle

## Goal
…  （--goal；否则取所选会话里最早一条实质性用户请求）

## Current verified state
- branch / HEAD / 脏文件   （bundle 生成时现场跑 git，不用会话里的旧快照）
- 最近助手结论，新会话优先

## Completed work
- …  （写过的文件、exit 0 的测试、能检测到的 commit）

## Open tasks
- …  （未完成的用户要求、失败命令、最后一次「接下来」）

## Decisions
- 采用 Y。sources: claude#abc, codex#def  verified: commit 485bdc8
  superseded: X → Y   （A 提出 X，B 否定）

## Known failures
- X 失败，因为 …   sources: codex#def

## Relevant artifacts  （只放指针）
- voyager/adapters/dsh.py
- tests/test_dsh.py

## Current repo state
- branch, commit, dirty files  （生成时再检查一遍）

## Evidence
- 纳入的 sessions、provider、时间范围
- confidence: extracted | inferred | unverified
```

**confidence / provenance 才是和普通 summarizer 的差距。**
编译器撑不住的断言标 `unverified` 或直接省略。

---

## Context Budget

```
voyager continue --budget auto
voyager continue --budget 12k
voyager handoff <id> --to claude --budget compact
```

| 档位 | 大约体积 | 内容 |
|---|---|---|
| `compact` | ~4k tokens | 目标、当前状态、下一步、关键决策、产物指针 |
| `balanced` | ~20k | + 文件、错误、近期相关对话 |
| `full` | ~100k | + 选中的原始证据、更长的时间线 |
| `Nk` / 整数 | 那么多 token | 按优先级填到上限 |
| `auto` | 看目标 Agent | 大上下文 CLI → balanced/full；未知 → balanced |

core 里用 `chars/4` 估 token，不引入 tokenizer。
目标相关的排序发生在截断**之前**：CI 目标留下 pytest / Actions / 失败；
UI 目标就把它们丢掉。

---

## CLI / MCP / Skill（共用同一套 core）

四个入口，一份编译器。分阶段上，不是第一个 PR 全做。

```
# 显式合并
voyager merge <s1> <s2> <s3> [--goal "..."] [--budget balanced] [-o FILE]

# 从若干会话继续，或自动挑 thread
voyager continue --from sA,sB,sC --to claude --goal "finish adapter tests"
voyager continue --repo voyager --to codex --budget 12k

# 一条命令换 Agent 接着干
voyager switch codex

# 更晚
voyager thread list|show|attach|close
```

MCP（对同一组函数的薄封装）：

| 工具 | 作用 |
|---|---|
| `voyager_handoff` | 保留；加上 `goal` / `budget` / 多 id |
| `voyager_merge` | 新增 |
| `voyager_continue` | 新增（认识 thread） |
| `voyager_switch` | 新增 |
| `voyager_thread` | WorkThread 落地后再加 |

Skill（`skills/voyager/SKILL.md`），装到
`~/.codex/skills/voyager/`、`~/.claude/skills/voyager/` 等：

| 用户说 | Skill 去做 |
|---|---|
| 「把这个交给 Codex」 | `voyager handoff` / `voyager switch` |
| 「看看 Claude 刚才做了什么」 | `voyager search` / thread |
| 「接着 Codex 昨天的工作」 | `voyager continue` |
| 「把我最近关于 Voyager 的几个会话合并一下」 | `voyager merge` |

自然语言在 skill 里。逻辑在 Voyager 里。

---

## 前端架构（轮到 UI 的时候）

```
              Voyager Core（编译器 + store）
                       │
               local API / daemon
                       │
         ┌─────────────┼─────────────┐
         │             │             │
        CLI           MCP           UI
         │             │             │
   Agent Skill    Agent 原生      VS Code 侧边栏
                                  （更晚：Context Composer）
```

**不要**先做各家插件。**不要**先做完整 Web App。
如果做 UI，第一刀是 VS Code Sidebar（用户本来就住在这里）：

- 当前项目 / 活动 thread
- 成员 sessions（provider + 多久以前）
- 已选上下文（文件、决策、冲突）
- Continue with + Context budget
- 把 Agent 事件和 git 混在一起的 Timeline
- **Context Composer**：左边勾选会话，右边实时 bundle + 预估 token，下面 Launch

那是 Phase 7。它是编译器的客户端，不是第二套实现。

---

## 分阶段计划

优先级是 pipeline，不是界面。每一阶段都可以独立合入。

### Phase 0 — 把 V1 的说法说清楚  *（只改文档）*

**为什么。** 现在的文案容易被读成「无缝迁移 Session」。

- README / WORKFLOWS：`handoff` / `continue` 写成工作接续；
  原生 resume 和跨 Agent bundle 分开讲。
- README「下一步」指向本文。
- 决策记录 D9（continuity 优先于 TUI）、D10（确定性编译器）。

**完成标准：** 读完不会以为 Voyager 能把 Claude 的 Session 原样搬到 Codex。

### Phase 1 — 多会话上下文合成  **（先做这个）**

**为什么。** 比单 Session handoff 更重要，后面全堵在这一步。

```
voyager merge A B C
voyager continue --from A,B,C --to claude
```

**做**

- 抽出编译模块（`voyager/continuity/`，或先长在 `handoff.py` 里、下一个 PR 再拆）。
- 选 N 个会话 → 抽事实 → 去重 → 按时间 overlay → bundle Markdown。
- V1 overlay 规则：最新会话的「停在哪」是当前状态；更早的助手结论放进
  **Prior（可能已被替代）**，带时间和 source id。不要默默丢掉，也不要写成当前事实。
- bundle 生成时现场拍 git（`branch` / `HEAD` / `status --short`）。
- 继续 D8：写文件，启动词只有「读这个路径」。
- 默认输出 `~/.voyager/bundles/`（V1 写在 cwd 是意外），`-o` 可覆盖。
- 测试：三个合成会话（提出 X / 否定 X 改 Y / 实现 Y）→ bundle 里 Y 是当前、
  X 是 superseded。回归：X 不得再作为开放选项出现。

**文件：** `voyager/handoff.py`（或 `voyager/continuity/`）、`voyager/cli.py`、
`voyager/mcp_server.py`、`tests/test_handoff.py` / `tests/test_continuity.py`。

**本阶段不做：** thread 表、`--goal` 排序、token 预算、UI。

### Phase 2 — WorkThread

**为什么。** `continue` 应该是「我正在推进的那件事」，不是「最新一条聊天」。

```
voyager continue --repo voyager --to codex
voyager thread list
voyager thread show <id>
voyager thread attach <sid>
```

**做**

- `threads` / `thread_sessions` 表；加法迁移（旧索引继续能用）。
- `continue --repo` 用上面的信号自动聚类。
- `voyager merge A B C` 创建（或更新）一个 thread。
- 无参数 `continue`：cwd 的 repo → 活动 thread → 若最新成员能原生 resume
  且没给 `--to`，走 resume；否则编译 + handoff。
- CLI 稳定后再加 MCP `voyager_thread`。

**本阶段不做：** 花哨的主题模型、重命名体验打磨、UI。

### Phase 3 — 面向目标的 handoff

```
voyager handoff A --to claude --goal "finish adapter tests"
voyager continue --repo voyager --goal "fix CI"
```

**做**

- 用 `--goal` 给可抽取事实排序（FTS + 文件路径 + 命令启发式：
  CI 对上 `pytest` / `.github/workflows` 等）。
- 低相关的节在进入 budget 之前就缩小或消失。
- 单会话 `handoff` 和多会话 `merge` 共用同一个 ranker。

**本阶段不做：** 调 LLM 去「理解」目标。

### Phase 4 — Context Budget / 自适应打包

```
voyager continue --budget auto
voyager handoff A --to codex --budget 12k
```

**做**

- 档位 `compact` / `balanced` / `full` / `auto` / 整数 token。
- 按优先级填充（目标 → 状态 → 决策 → 失败 → 指针 → 证据）直到上限。
- CLI 打印预估 token。

**本阶段不做：** 可学习压缩、各家 tokenizer。

### Phase 5 — Voyager Skill

**为什么。** Voyager 要进到每个 Agent 里面，而不是只活在人类记得打开的终端里。

```
skills/voyager/SKILL.md
voyager skill install          # 拷到已知的 skill 目录
```

**做**

- 一份 skill：什么时候调用 Voyager、用哪条 CLI/MCP、什么不要做
  （不要把 `export --format md` 整份倒进上下文）。
- 安装器覆盖 Codex / Claude Code / Grok 的 skill 位置；未知 Agent 打印路径。
- Phase 1–4 的 MCP 工具写进 skill。
- 测试：SKILL.md 存在、安装幂等、不写入 provider 的 *session* 目录。

**本阶段不做：** 各家插件 UI。

### Phase 6 — `voyager switch <agent>`

「一条命令换 Agent 接着干」的体验。

```
voyager switch codex
```

**做**

1. 解析活动 thread（cwd / `--repo` / 显式 `--thread`）。
2. 同一 provider 且 `can_resume` → 原生 resume（D7）。
3. 否则编译 bundle（goal + budget 用默认）→ 拉起目标（D8）。
4. 再看一遍 git working tree；脏得意外就警告。

用户只看到一条命令。内部是 select → compile → launch。

### Phase 7 — VS Code 侧边栏 / Context Composer  *（最后）*

必须等 Phase 1–4 存在，UI 才是编译器的客户端。

- VS Code 侧边栏：项目、thread、sessions、Continue / budget。
- Context Composer：勾选会话 → 实时 bundle + token 估计 → Launch。
- 需要时再加一层极薄的本地 HTTP/stdio API，包同一份 Python 函数。
  不是第二棵业务逻辑树。
- **不是**完整 Web App。**不是** N 个厂商插件。

---

## GitHub Issue

已在 `HarryHeYu/voyager` 打开（标签 `enhancement` + `continuity`）。
把勾选用作实现契约。

| Issue | 标题 | Phase | 被谁挡住 |
|---|---|---|---|
| [#1](https://github.com/HarryHeYu/voyager/issues/1) | Continuity Engine：总跟踪 issue | 0 | — |
| [#2](https://github.com/HarryHeYu/voyager/issues/2) | `voyager merge`：多会话上下文合成 | 1 | — |
| [#3](https://github.com/HarryHeYu/voyager/issues/3) | WorkThread：project → thread → sessions | 2 | #2 |
| [#4](https://github.com/HarryHeYu/voyager/issues/4) | 面向目标的抽取（`--goal`） | 3 | #2 |
| [#5](https://github.com/HarryHeYu/voyager/issues/5) | Context Budget（`--budget auto\|Nk`） | 4 | #2 |
| [#6](https://github.com/HarryHeYu/voyager/issues/6) | Voyager Skill + `voyager skill install` | 5 | #2 |
| [#7](https://github.com/HarryHeYu/voyager/issues/7) | `voyager switch <agent>` | 6 | #3, #4, #5 |
| [#8](https://github.com/HarryHeYu/voyager/issues/8) | VS Code 侧边栏 / Context Composer | 7 | #3, #4, #5 |

#1 是伞。#2–#7 完成就关；#8 明确是壁垒形成之后的事。

---

## 这一代明确不做

- 无缝 **Session** 迁移（system prompt / tool state / cached reasoning 搬不过去）。
- 把各家插件当第一 UI。
- 把 transcript concat 起来叫做 merge。
- 在 core 里用 LLM 做摘要、接云端 API、给 core 加新依赖。
- 用 bundle 冒充同平台原生 resume（D7）。
- 写入 provider 的 session 目录。
- 独立 Web App。
- 把 TUI / 更多 adapter 当成*战略*下一步。Adapter 在上游改格式时仍然重要，
  但不是这次的产品跳跃。

---

## 做成什么样算成功

一个人在同一个仓库里把活拆给 Claude、Codex、Grok，然后跑：

```
voyager continue --repo voyager --to codex --goal "finish adapter tests" --launch
```

Codex 在**新** Session 里启动，读到的 bundle 已经知道目标、活下来的决策
（Y 而不是 X）、失败的测试、该动的文件、当前 git HEAD——并且不会再提出 X。

这就是产品。

---

## 未决问题（不挡 Phase 1）

1. **Bundle 放哪。** 默认 `~/.voyager/bundles/` 还是仓库内 `.voyager/`（gitignore）。
   倾向全局目录，减少 cwd 污染和误提交；永远保留 `-o`。
2. **Thread 怎么诞生。** 只自动聚类，还是必须 `merge` 才创建。
   倾向：`continue --repo` 可以自动聚；用户 merge 或 switch 时再持久化。
3. **可选 LLM extra。** 以后用 `[llm]` extra 把引文提升成已决议的 Decision。
   必须 opt-in，不走 core 路径。不进 Phase 1–6。
4. **Daemon。** `voyager watch` 已经存在。本地 API daemon 只在 VS Code
   客户端需要时才做（Phase 7）。

需要拍板时写入 `docs/DECISIONS.md`。
