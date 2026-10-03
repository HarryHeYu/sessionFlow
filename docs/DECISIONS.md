# Voyager 决策记录

## D1 — 语言选 Python（而非 C/Rust）

**Decision**: 用 Python 3.10+ 标准库 + 可选 `zstandard` 实现全部适配器。

**Reason**: 六家平台的格式全是 JSON/JSONL/SQLite 解析，瓶颈在格式适配速度而非运行速度；
Python 的 json/sqlite3/difflib 全部内置，迭代最快。这个项目的目标是正确性和数据完整性，
不是练手语言特性。

**Alternatives**: Go（单二进制分发好，但适配迭代慢一档）；Rust（同上）。

**Consequences**: 用户需有 Python；`pip install -e .` 安装。TUI（Textual）天然适配。

## D2 — raw_event 分层截断存储而非全量

**Decision**: `raw_json` 截 8KB、正文截 600KB、FTS body 截 600B，超限带
"[truncated N chars]" 标记。

**Reason**: 实测本机语料（13.5 万事件）：raw 全量要 645MB、FTS body 2KB 就让索引到
1.3GB。raw 的独特价值是归一化没覆盖的 provider 字段，它们集中在 payload 头部；
完整原文永远在 provider 源文件里（sources 表记录精确路径 + seq）。

**Alternatives**: 全量保存（磁盘换安心）；外部 raw 存储（复杂度不值）。

**Consequences**: `voyager export --format json` 的 raw_event 对超大事件是截断版；
`show` 显示的归一化字段不受影响。若需要更大 raw，调 store.py 的三个常量重建索引。

## D3 — FTS5 用 trigram 而非 unicode61

**Decision**: `tokenize='trigram'`。

**Reason**: unicode61 把连续 CJK 当一个 token，`搜索"中文测试"匹配不到"中文测试消息"`——
中文搜索直接失效（实测踩坑）。trigram 支持 ≥3 字符子串匹配，中英通吃。
SQLite ≥3.34 即可用（本机 3.50）。

**Alternatives**: unicode61 + 手工 CJK 分词；ICU tokenizer（需扩展）。

**Consequences**: <3 字符的查询匹配不到（对中文无碍，对 2 字母英文缩写需用 `*` 前缀）。

## D4 — ZCode 整库重扫而非增量

**Decision**: ZCode adapter 在 db.sqlite mtime 变化时全量重建其 38 个 session。

**Reason**: message/part 表没有可用的 per-session 更新水位；有活动会话时 mtime 每次都变，
增量方案要自己维护游标，MVP 不值得。

**Alternatives**: 记录 per-session max(sequence) 增量拉取。

**Consequences**: 有活动 ZCode 会话时每次 scan 多花 ~9s。后续可优化。

**2026-10-02 复测（重要更正）**: 本机 ZCode DB 已涨到 71.4 MB / 78 session /
8,868 event / 16,652 part，全量重扫 **41.6 s**。但拆开看：

- 整个扫描只派生 **31 次 `subprocess.run`**，而本机沙箱每次进程启动约 **1.34 s**
  ⇒ **≈100% 的耗时是进程启动税，不是数据库**。
- ZCode 自己的解析（16k 次 JSON parse、8,868 个 event）在亚秒级。
- `git_info` 的 per-cwd 缓存**工作正常**：78 个 session → 9 个不同目录 → 31 次 spawn
  （而不是 78 × 4）。也就是说这里**没有** N+1。

⇒ **上面的 Alternatives（per-session max(sequence) 水位）目前没有测量支持。**
它要优化的是不存在的成本，却会引入「漏会话」的正确性风险（message/part 表没有水位，
改动得自己维护游标）。**在真实终端（非沙箱）测出扫描确实成为瓶颈之前，不要做。**

**顺带发现（与环境无关，值得记）**: 扫描的进程数 ≈ 不同工作区数 × 最多 4 条 git 命令
（`rev-parse --show-toplevel` / `--abbrev-ref HEAD` / `rev-parse HEAD` / `remote get-url`）。
本机 9 个仓库 → 31 次。若用户有 ~200 个仓库，真实终端下约 800 次 spawn（15–30 s）——
那才是真正的可扩展性风险，而且**不限 ZCode**（`finish_session(session, git_info(cwd))`
对每个 adapter 都按 session 调用）。真要优化，方向是把 4 条 git 命令合并成 1–2 条
（`git rev-parse` 一次可接受多个参数），而不是改 ZCode 的扫描策略。

## D5 — Codex 按"文件名内嵌 session_id"分组合并 rollout

**Decision**: 同一 session 的多个 rollout 文件（VSCode/Desktop 的 window 续接）合并为一个
voyager 会话，事件按文件时间顺序拼接。

**Reason**: 78 个 rollout 实际只有 62 个 session；不合并会导致 resume 目标混乱、
时间线割裂。history_base/续接元数据保留在 raw_metadata.continuations。

**Alternatives**: 用 history_base 显式建链（准确但复杂，需要处理基线偏移量）。

**Consequences**: 若 Codex 未来改变文件命名规则需同步改 `_session_id_of`。

## D6 — 标题清洗 <environment_context>

**Decision**: Codex 首条用户消息里的 `<environment_context>`/`<user_instructions>` 块
从标题中剔除；若剔除后为空则取后续消息，最终回退 session id。

**Reason**: 实测大量会话首条消息是注入的环境块，标题全是 XML 噪声。

**Consequences**: 正则需跟随 Codex 注入格式变化。

## D7 — Resume 不做伪实现

**Decision**: `can_resume=False` 的平台（ZCode 桌面端、Kiro、Antigravity、Cursor）
`voyager resume` 直接打印 "Resume unsupported for this provider"。

**Reason**: 假装能恢复（比如只打印上下文）会误导用户。等确认了真实的 CLI 入口再开。

**Consequences**: ZCode 恢复目前要去桌面端手动点。

## D8 — Handoff 用"文件引用"注入而非 argv 传全文

**Decision**: `voyager handoff` 把上下文包写成 Markdown 文件，目标 agent 的启动
prompt 只有一句话："读这个文件接着干"（文件绝对路径附在 prompt 里）。

**Reason**: ① Windows argv 上限 ~32K，超长上下文会炸；② 包含用户消息的全文
塞进命令行需要处理层层转义/引号注入；③ 目标 agent 本来就会读文件——Codex/
Claude/Grok 都是 agentic CLI，读文件比解析巨型参数更符合它们的工作方式。

**Alternatives**: stdin 管道传全文（部分 CLI 不支持从 stdin 读初始 prompt）；
把包写进目标 agent 自己的 session 格式（格式私有且易碎，正是本项目反对的）。

**Consequences**: 目标 agent 必须能读文件系统（三者皆可）；包文件是普通
Markdown，用户可以先人工审阅/删改再拉起。D7 的原则同样适用：DSH 等没有确认
"初始 prompt" 入口的平台，只生成包文件并提示手动粘贴。

## D9 — 下一阶段做 Continuity Engine，而不是 TUI / 更多 Adapter

**Decision**: 在索引层已经可用之后，战略投入转向跨 Agent 的 **工作接续 /
上下文编译**（WorkThread、多会话合成、goal-conditioned bundle、Context
Budget），而不是先做 TUI、完整 Web App、或按厂商各写一份插件。
Adapter 仍然维护（上游改格式会坏），但不是产品跳跃。路线图见
`docs/ROADMAP.md`。

**Reason**: Voyager 的独特资产是已经归一化的多 Provider Session/Event、
repo、文件、命令、错误和时间线。把这些编译成「下一个 Agent 真正需要的
上下文」是别的 history manager 和单 Session handoff-skill 做不到的。
UI 只是同一套编译器的前端；先做 UI 等于在核心 pipeline 还没有时画外壳。

**Alternatives**: 继续加 Adapter 覆盖面；先做 VS Code / Web 查看器；
把 V1 handoff 再打磨成更好的单会话摘要。

**Consequences**: README 的 "What's next" 指向 Continuity Engine。
`voyager continue` / `handoff` 的演进按 ROADMAP Phase 1–6 走；
VS Code Sidebar / Context Composer 明确排在编译器之后（Phase 7）。

## D10 — Continuity 编译器在 core 里必须确定性、离线

**Decision**: 多会话合成、冲突 overlay、goal ranking、budget packing
第一版全部是对已索引字段 + 现场 `git` 的确定性抽取。core 不调用 LLM、
不联网、不加 tokenizer 依赖。token 预算用 `chars/4` 估算。
目标 Agent 是读 bundle 的推理者。可选的 LLM 精炼若出现，只能是 opt-in extra，
不能成为默认路径。

**Reason**: FAQ 和 CONTRIBUTING 已经承诺「项目里没有网络代码」。
把摘要外包给云模型会破坏 local-first，也让测试变成「模型今天怎么说」。
索引里已经有足够硬事实（文件、命令、exit code、错误、时间戳、git）
做 V1 overlay：新会话的结论为当前，旧结论标 superseded 并保留出处。

**Alternatives**: 默认走 LLM 做 narrative summary；嵌入本地小模型。

**Consequences**: bundle 里每条断言带 provenance
（`extracted` / `inferred` / `unverified`）。编译器撑不住的叙事宁可省略，
也不编。Phase 1 的验收测试是合成夹具，不调任何模型。

## D11 — 跨 Agent 默认走编译 Bundle，不改写目标 Session 文件

**Decision**: `voyager switch` / 跨 provider `continue --to` 的默认路径是
「增量 scan → 从归一化 Event 编译 Continuation Bundle → 新 Session + 读这个文件」（D8）。
不把 Claude JSONL 改写成 Codex rollout，不往目标 Agent 的 session 目录写合成历史。
若以后做 transcript transplant，必须同时满足：opt-in（`--mode transcript`）、
只写**新** session id、只压 user/assistant 文本（丢掉 tool call）、只覆盖
已证实能 resume 合成 JSONL 的 CLI（目前候选：Claude / Codex / Grok / DSH）、
每个 writer 有夹具测试。Cursor / ZCode / Antigravity / Kiro 不在范围内。

**Reason**: 八家落盘格式不能互换；工具名对不上；不成对的 tool_use/result
会让下一次 API 调用失败；Grok/Codex reasoning 加密；往活 Session 目录写
会和正在跑的 Agent 抢文件。2026-09-16 无头探测：Grok / Codex 对合成纯文本
JSONL resume 是 HIT；Claude 超时。即便 writer 落地，也必须套在单写者租约
（D13）里：只写给当前持锁方、只写新 id、活着的时候不回写。D8 已经拒绝
「写成目标格式再假装 resume」。

**Alternatives**: 默认就写目标 Session 文件，TUI 里能翻到旧回合（更「像无缝」，
但格式脆弱、未证实）；两两 converter 矩阵（8×7，不可维护）。

**Consequences**: 用户在目标 Agent 里看到的是一份 Bundle 开场的**新**对话，
不是原来那条聊天记录。同平台续聊仍然走原生 resume（D7）。Phase 1b/#9
同步已落地；#10 的 Grok/Codex writer 已在 #11 租约下以 opt-in 方式交付
（`--mode transcript`）；Claude 仍走 bundle。

## D12 — Continuity 命令在编译前必须刷新索引

**Decision**: `handoff` / `merge` / `continue` / `switch` 读 store 之前先跑一次
增量 `scan`（尊重 sources 的 mtime+size，不用 `--force`）。命令已经知道
provider 或 repo 时，尽量只扫那一块。`voyager watch`（默认 300s）继续当后台，
但 switch 的正确性不依赖它刚好刚跑过。同步是**单向**的：provider 文件 → 索引。
不做两家 Session 文件的双向实时镜像。

**Reason**: 刚在 Claude 里聊完立刻 `voyager switch codex` 时，最后一轮可能
还没进 `~/.voyager/index.db`。跨 Agent「无缝」先死在陈旧索引，不是死在
bundle 文案。格式翻译已经发生在 adapter 的 parse()；缺的是 parse 的时机。

**Alternatives**: 强制用户先 `voyager scan`（会忘）；只靠 `watch`（300s 窗口
里一定过时）；双向写回各家 Session 文件（D11 否决）。

**Consequences**: 编译路径会多一次（通常很快的）增量 scan。测试必须覆盖
「mtime 变了的 source 出现在下一份 bundle」和「没变的 source 不重解析」。
这是路线图 Phase 1b / issue #9，已落地（`freshness:` 行即其产物），
`voyager switch`（#7）因此依赖它。

## D13 — 一个 WorkThread 同时只有一个写者

**Decision**: 跨 Agent 接续的规范历史住在 Voyager 里（归一化 Event / 以后的
thread 日志），**追加写入**。每个 WorkThread 有一份租约：`holder` provider、
`native_session_id`、`pid`、`heartbeat_at`、`lease_token`。
`voyager switch <agent>` 必须先抢到租约；抢不到就失败并打印持有者，
绝不默默开第二份写入。`voyager watch` 在跟踪持锁 Session 时刷新心跳。
心跳超过 120s 或 pid 消失视为过期。`--steal` / `thread unlock` 必须显式。

同步时机：

1. **打开时**：冲刷上一任 → 物化规范日志到新持锁方（有 writer 则新 session id；
   否则 bundle）。
2. **运行中**：只从持锁方的原生文件 **吸入** 规范日志。不回写这份正在用的文件。
3. **切走时**：最后一次吸入，释放租约。

**Reason**: 用户要的是「统一格式 + 打开就同步 + 不断写入」，这在**单写者**下
做得到。两家 Agent 同时写同一份聊天（无论是原生文件还是规范日志）会把
「当前状态」交织掉。探测已证明 Grok/Codex 可以在打开时物化一份新 JSONL；
活着回写仍会和 Agent 自己的 append 抢文件。

**Alternatives**: 无锁、靠用户别同时开两家（会忘）；两家都实时镜像规范日志
（多写者）；持锁期间也回写原生文件（和 D11 同一场竞赛）。

**Consequences**: Phase 2 的 `thread_leases` 已落地（issue #11，
`c41f99b`）。`voyager switch`（#7）已消费这把锁；writer（#10）的
codex/grok 实现同样运行在租约之下。测试要覆盖：第二家 switch 失败、
过期租约可抢、持锁期间只吸入持锁方。

## D14 — 四条 handoff 命令共用同一个引擎

**Decision**: `voyager switch` / `continue` / `handoff` / `merge`（以及 MCP 的
`voyager_switch` / `voyager_handoff` / `voyager_merge`）不再各自实现一遍流程，
统一走 `continuity.handoff_thread()`。引擎接受三种来源：一个 WorkThread、
一组显式 session、或单个 session；来源是 session 时，会**收养**已经拥有它的
WorkThread（**包含**关系，不是相等关系），于是同一把租约和同一条 pending
attach 依然生效。

引擎本身**不打印、不拉起、不抛异常、不写 provider 文件、不创建 WorkThread**：
它返回一个结果字典，由调用方决定措辞和退出码（CLI 是 `_render_handoff`）。
不打印是硬要求——MCP 的 JSON-RPC 就走 stdout。

`style` 决定目标 agent 读到的规范上下文：`continuation`（多 session 的
Continuation Bundle，默认）或 `package`（单 session 的 Context Package，
`voyager handoff` 用）。两者都编译自同一份索引证据——这是**表现形式**的差异，
不是第二套接续模型。租约与 pending attach 只在**真的换手**时产生：给了
`target`，且有 WorkThread 可保护；否则引擎照样编译上下文，并报告
`scope == "session"`（这是正确的，不是降级——租约保护的是一个 WorkThread
不被两个写者同时写，两次互不相干的 session 级 handoff 不构成线程冲突）。

**Reason**: 收敛前每条命令都有自己的流程副本，同一个 WorkThread 的行为取决于
你敲了哪条命令：

- `switch T --to X` 抢锁并写 pending，而 `continue --thread T --to X` 两样都
  不做——两家 agent 可以同时写同一个线程，目标 agent 的新 session 也永远不会
  被下一次 scan 收养；
- `continue --thread T --to X` 忽略 D7，即使线程里已有该 provider 的可恢复
  成员也照样编 bundle；
- `switch --bundle` 解析了却没传给引擎，是静默 no-op；
- `switch <agent>`（不带 `--thread`）抛 `UnboundLocalError`：候选列表推导式的
  `t` 在 py3 里不会泄漏到外层作用域，所以 `thread=t` 是未绑定的。

"每条命令各自记得做一遍"正是这个项目反复踩的坑（见 D11/D13 的 Alternatives）。

**Alternatives**: CLI 各自实现、只共享 compiler——安全语义仍会漂移，正是上面
第一、二条；把 `handoff`/`merge` 也做成会抢锁的"真换手"——`voyager handoff`
是**导出**，`--to` 命名的是这份包的**读者**而不是要恢复的 session，抢锁只会
挡住下一次 handoff。

**Consequences**: `tests/test_handoff_convergence.py` 固定了收敛后的行为与引擎
不变式（静音、错误即值、绝不创建 WorkThread、没有启动路径时归还租约）。
新增 `Store.thread_find_containing()`（包含语义，`thread_find_by_members` 的
相等语义不够用）。删除 `_merge_and_handoff` / `_handoff_from_row` /
`_render_budgeted` 三份重复实现。`continue` 补上 `--bundle`。

## D15 — source 消失 ≠ 用户想删历史

**Decision**: provider 的 source 文件**全部**消失时，Voyager **保留**该 session 的
canonical 历史，只把它标记为 `SOURCE_MISSING`：

- `sessions.source_state`：NULL/`LIVE` 或 `SOURCE_MISSING`；
  `sessions.source_missing_since` 记录首次不可见的时间。
- `sources.last_seen` / `sources.missing_since` 记录**每个 source** 的历史
  （provider / path / 最后见到 / 何时开始缺失 / sid），这样 doctor 与 timeline
  能解释"这段历史**为什么**被保留"，而不是只报一个 `retained=true`。
- events / files / FTS 行**一律保留** ⇒ search、timeline、thread summary、
  checkpoint 引用、历史证据检索全都还能用。
- **排除**：自动 startup continuity、active-provider 选择、native resume 假设、
  live source health。实现上是 `Store.live_thread_members()`，
  `auto.get_continuation_context()` 改用它编译上下文，并回报
  `retained_members`。
- **只要还有一个 source 在盘上，session 就是 LIVE**（不是"任一 source 消失即
  retained"）。source 回来后重新 ingest 同一个 native session id 即
  **reconcile**：一行 canonical session、无重复、状态回 `LIVE`、
  `source_missing_since` 清空、events 刷新。
- **没有自动 purge。** 真正的删除必须是显式动作（将来的 `voyager history purge`），
  另案设计。

**Reason**: 旧实现是 `prune_missing_sessions()` 直接 `DELETE` session + events +
files + FTS。但走到那一步时 provider 文件**已经没了**，索引握着的是**唯一一份**
归一化副本 ⇒ provider 一轮转（Codex 清老 rollout、`~/.claude/projects` 被清、
ZCode/Cursor 换库）就把用户从没要求删除的历史**永久**抹掉了。
"source 不见了"被当成了"用户想删"。这两件事没有任何蕴含关系。

**Alternatives**: 只加一个 `retained` bool（doctor/timeline 无法解释原因）；
`SOURCE_MISSING` 与 `RETAINED` 分成两个状态（O2 里没有任何行为差异 —— 状态只
存在却不被区分，就会有人把它设错，所以合并成一个）；自动按时间 purge
（会把"暂时不可见"（网络盘、临时清理）当成"永久删除"）。

**Consequences**: 迁移是**纯 additive**（`ALTER TABLE ADD COLUMN`），旧行
`source_state` 保持 NULL 并读作 LIVE —— **不回填**，因为"写这行时 source 是否在
盘上"事后不可知，猜就会把活 session 标成 retained。retained 历史有成本，所以
`store.retained_stats()` 量出 sessions/events/近似字节，`voyager doctor` 以
**非阻塞**方式报告（`retention` 块 + `retained N session(s) …`）；只有在
**某个 active WorkThread 一个 live member 都不剩**时才升级为 `warning`
（`RETENTION_STRANDED_WORKTHREAD`），因为那时该 thread 的 continuity 已经
无料可编。`api.overview` / `api.thread_detail` 与 thread brief 都会带上
`source_state`（**标记，不隐藏**），UI 怎么显示留给 O3。

**"排除 continuity"必须是全路径的，不只是 startup 那条。** 第一次实现只改了
`auto.get_continuation_context()`，结果 handoff 引擎仍在把 retained 历史编进
bundle、并把 retained session 当作 native-resume 候选（`resolve_handoff_source`
用的是 `thread_members`）。补齐后：`resolve_handoff_source` 的 thread scope 走
`live_thread_members`；`handoff_thread` 的 resume 候选额外要求 `is_live()`，
所以**即使显式指名**一个 retained session 也不会去 resume（退回 bundle）；
`discover_continuity` 的 `latest_holder` 也只从 live member 里选；
`continue --thread` 在全 retained 时**说明原因**再拒绝。
`voyager list` / `show` 也会标记。**教训：加一条"某类 session 不参与 X"的规则时，
要把 X 的每条路径都找出来，不能只改你第一个想到的那个。**

回归在 `tests/test_retention.py`（22 个）+ 改写的
`tests/test_cli.py::test_scan_retains_sessions_whose_source_vanished`。

## D16 — Timeline 只有一个模型，且只放有证据的事件

**Decision**: WorkThread 的时间线只有一份实现：`voyager/timeline.py::
build_thread_timeline(thread_id)`。CLI（`voyager thread timeline`）、Dashboard、
VS Code webview、以及 stdio API 的 `thread_timeline` op **都只是它的消费者**；
任何一处都不得再写第二套 aggregation。

事件只允许来自两类**已有时间戳**的 canonical 依据：

1. 已有列：`threads.created_at`（THREAD_CREATED）、
   `thread_sessions.attached_at`（SESSION_ATTACHED）、
   `thread_pending.created_at`（HANDOFF / PROVIDER_SWITCHED）、
   `checkpoints.created_at`（CHECKPOINT_CREATED / BLOCKER_ADDED / TEST_GATE /
   COMMIT_OBSERVED）、`sessions.source_missing_since`（SOURCE_MISSING）。
2. 追加日志 `thread_events`（O3 新增，append-only）：只放**别处没有时间戳**的事实
   —— 状态迁移（THREAD_CLOSED / THREAD_REOPENED / THREAD_ARCHIVED）、
   source 回来（SOURCE_RETURNED）、以及 source 消失的那一刻。

**禁止**扫描 assistant 的自然语言去猜"这句像里程碑"。`BLOCKER_RESOLVED` 只在
**后一个 checkpoint 明确把它记成 milestone** 时才产生 —— blocker 单纯消失不算证据
（可能是被丢掉了）。

**Reason**: 三条理由，按重要性排序。

1. **三套 aggregation 一定会漂移，而漂移是看不见的。** 同一个 thread 在 CLI 和
   dashboard 上给出不同的故事，用户没有任何办法判断哪个对。
2. **时间戳只能排序，不能定权威。** 它不能用来解决 WorkThread 歧义（D13/D14 已有
   明确的规则），也不能推导"谁是当前持有者"—— 那是租约和显式规则的事。
3. **猜出来的时间线比短的时间线更糟。** 从散文里推断里程碑会让时间线看起来更丰富，
   同时把不可验证的东西伪装成事实。项目里所有"宣称"都要有机器证据，时间线不能例外。

**Alternatives**: 让每个前端各自聚合（简单，但必然漂移）；把 assistant 文本里的
关键词当事件（丰富，但等于编造）；用 `threads.updated_at` 当状态迁移时间
（**行不通**：`thread_touch` 也写它，所以它不能代表状态变更）。

**Consequences**: 新增 append-only 表 `thread_events(thread_id, ts, kind, provider,
session_id, detail_json)` + `(thread_id, ts, id)` 索引。它只承载别处无法表达的事实，
所以**不会与派生事件重复**；SOURCE_MISSING 在迁移发生时写入，`_source_missing()`
只为"O3 之前就已经 retained、因此没有日志行"的 session 兜底派生 —— 日志优先，
同一 episode 不会出现两次。

性能上时间线**只读 thread 作用域的行，从不碰 `events` 表**：它是
lifecycle/milestone 视图，不是 transcript dump。60 session / 6,000 event 的 thread
仍然只发 ≤10 条查询（回归里钉住了这一点），`--limit` 保留最新 N 条再恢复时间顺序。

O3.3 的措辞是契约的一部分：SOURCE_MISSING 一律表述为
"Provider source disappeared — history retained locally"，
恢复是 "Provider source restored — session reconciled"，
**绝不使用 deleted / lost 这类词**（回归会检查 dashboard 与 timeline 的输出里不出现）。

O3.6：时间线**自己不切换**。UI 只提供"复制 canonical CLI 命令"，切换继续走
`continuity.handoff_thread()` —— 不新增第二套切换逻辑（回归会检查 webview 里
没有自己的 switch 调用）。

## D17 — Doctor 是唯一健康模型，`--fix` 只跑 SAFE_DERIVED_REPAIR

**Context**: `voyager doctor` 在 O4 之前是一个 ad-hoc 字典：`check_store`、
`check_continuity`、`check_hook_config`、`check_cache`、`check_retention` 各自
返回形状不同的 dict，issue 只有 `kind`/`id`/`detail` 三个字段。Dashboard
"自己判断健康"（直接读 `blocking` 数组），CLI 没有 `--fix`。四个新需求
（lease 健康、pending 健康、verification 诊断、cache 修复）无法塞进旧形状
而不再混入"provider 没自然触发 = 坏了"的噪音。

**Decision**: O4 定义一个 canonical issue 模型：

- `Issue(code, severity, category, message, evidence, suggested_action,
  auto_fixable, repair_kind)` —— 每个检查的输出都进这个形状。
- `severity`: `info` | `warning` | `critical`（三个级别，不多不少）。
- `repair_kind`: `READ_ONLY_DIAGNOSIS` | `SAFE_DERIVED_REPAIR` |
  `USER_DECISION_REQUIRED` | `EXTERNAL_PROVIDER_ISSUE`（四类，不多不少）。

`doctor --fix` **只执行** `SAFE_DERIVED_REPAIR` 且 `auto_fixable=True` 的 issue。
**明确禁止**：

- 删 retained history（`USER_DECISION_REQUIRED`）；
- 解决 ambiguity（`USER_DECISION_REQUIRED`）；
- 偷 / 清 lease（`USER_DECISION_REQUIRED`）；
- 碰 provider 文件（`EXTERNAL_PROVIDER_ISSUE`）；
- 跑 VACUUM（那是 maintenance，不是 repair）。

`--dry-run` 只打印计划，不执行。plain `doctor` 永远只读。

Dashboard 不再"自己判断健康"——它直接消费 `doctor.run()` 的 canonical issues，
通过 `severity` / `repair_kind` 分组展示。`render_html()` 新增 leases/pending 行，
但分组逻辑与 doctor 的 `render()` 一致。

**Mutation guarantee**: `USER_DECISION_REQUIRED` 的 issue 即使传 `--fix` 也
**一字不改**（回归里有四个 mutation test 钉住：lease、pending、retained
history、ambiguity 各一个，断言 before == after）。

**Backward compatibility**: `Issue.to_dict()` 同时输出 legacy `id` / `detail` /
`kind` 键（`kind` 由 `_legacy_kind(severity, repair_kind)` 推导），所以现有
consumer（dashboard、CLI、测试）不需要 flag-day 改造。`run()` 返回的 report
dict 在旧键之外新增 `leases`、`pending`、`verification` 三个 top-level key。

**O4.13**: O1 的两条性能 debt（`OVERVIEW_RECENT_SESSIONS_N1`、
`SCAN_MATERIALISE_ALL_BEFORE_WRITE`）只在 doctor 里**报告**，不修。
`KNOWN_DEBTS` 和 `O1_DEBTS` 都用 canonical 字段，不再用旧的 `kind`/`id`/`detail`。
