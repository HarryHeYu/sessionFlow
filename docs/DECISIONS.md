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
