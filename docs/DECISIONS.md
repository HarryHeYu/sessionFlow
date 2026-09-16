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
不是原来那条聊天记录。同平台续聊仍然走原生 resume（D7）。路线图 Phase 1b / #9
先做同步；#10 的 Grok/Codex writer 必须等 #11 租约；Claude 仍走 bundle。

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
这是路线图 Phase 1b / issue #9，并且挡住 `voyager switch`（#7）。

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

**Consequences**: Phase 2 的 WorkThread 必须带 `thread_leases`（issue #11）。
`switch`（#7）和 writer（#10）都挡住在这把锁后面。测试要覆盖：第二家
switch 失败、过期租约可抢、持锁期间只吸入持锁方。
