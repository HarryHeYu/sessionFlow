# Voyager 🧭

**把你机器上所有 AI 编码 Agent 的会话历史，变成一份可查询的索引。**

[English](README.md)

Voyager 读取各 Agent 已经写在本机的会话数据——Codex、Claude Code、ZCode、
DSH（DeepSeek Harness）等——整合成一份统一的历史库：可浏览、可搜索、可导出、
可一键恢复原 Agent 继续对话。纯本地运行，无账号、无云端、无遥测。

```
$ voyager list
ID                                    PROV   UPDATED            MSG TOOL  TITLE
a1b2c3d4-...                          zcode  2026-09-13 01:13   162  206  重构存储引擎的写入路径
9f8e7d6c-...                          codex  2026-07-06 22:52    76  168  修复多线程下载器的竞态条件
5e4d3c2b-...                          claude 2026-07-17 12:46    89   70  分析数据集结构并设计评测脚本
```

## 为什么需要它

每家 Agent 都用自己的格式存历史：Codex 写 rollout JSONL，Claude Code 写
project JSONL 外加一套文件版本链，ZCode 用 SQLite，DSH 用 zstd 压缩的
JSONL。你每天都在跨这些工具干活——Voyager 把这些历史变成**一个**可以查询的东西。

- **按 repo 汇总跨 Agent 时间线**——这个项目上所有 Agent 都干了什么、什么时候干的？
- **全库全文搜索**——找到"那条命令是哪个会话跑的"、"那个文件被谁改过"
  （中文子串搜索可用）。
- **真正的导出**——人类可读的 Markdown，或同时包含归一化事件与原始事件的 JSON。
- **一键恢复**——Voyager 知道每家平台的恢复命令，直接帮你调起来。

## 安装

```sh
pip install -e .            # 核心（Codex / Claude / ZCode 适配器）
pip install -e ".[dsh]"     # 加上 DSH（需要 zstandard）
```

要求 Python ≥ 3.10，Windows / macOS / Linux 均可。如果 `voyager` 不在 PATH 里，
用 `python -m voyager.cli` 运行。

## 使用

第一次使用：`voyager scan` 会遍历所有支持的 Agent 的本地存储，在
`~/.voyager/index.db` 建立索引。之后想收录新会话就再跑一次 `scan`——
它是增量的，只重新读取有变化的部分。想让同步完全自动，可以常驻一个
watcher（或挂到计划任务里）：

```sh
voyager watch --interval 300    # 每 5 分钟自动重扫，常驻运行
```

```sh
voyager scan                # 发现并索引所有支持的 Agent 会话
voyager list                # 全部会话，按更新时间排序
voyager list --repo myproj  # 某个 repo 的会话
voyager show <id>           # 完整时间线：消息 / reasoning / 工具调用 / 退出码
voyager search "tensorboard"
voyager repo E:/code/myproj # 一个 repo 的跨 Agent 时间线
voyager export <id> --format md   # 或 --format json（含原始事件）
voyager resume <id>         # 调起原 Agent 恢复该会话
voyager handoff <id> --to codex   # 生成给另一个 agent 的上下文包
voyager continue            # 一条命令接着上次干（原生恢复或自动接力）
voyager files <id>          # 该会话碰过哪些文件
voyager diff <id>           # Claude 会话：从版本链重建前后 diff
voyager stats               # 索引统计
```

**跨 Agent 接力**：`voyager handoff <id> --to codex` 会把会话提炼成一份自包含的
上下文包（目标、指令、碰过的文件、执行过的命令、报错、工作停在哪），并给出目标
agent 的启动命令；加 `--launch` 立即启动。目标 agent 被告知"读这个文件接着干"——
`claude`、`codex`、`grok` 支持直接拉起；其他目标会给出生成的包文件手动粘贴。

**一条命令继续**：`voyager continue` 自动挑你最新的会话并做对的事——
codex/claude/dsh/grok 走原生恢复，其余自动生成接力包。`voyager continue
--repo myproj --launch` 直接回到某个项目的现场。

会话 ID 支持前缀匹配；前缀有歧义时会列出候选并退出，不会猜。

## 支持的平台

| 平台 | 数据源 | 消息 | 工具调用 | Shell 退出码 | 文件 diff | Token | 恢复 |
|---|---|---|---|---|---|---|---|
| Codex（CLI/VSCode/桌面版） | rollout JSONL | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ `codex resume` |
| Claude Code | project JSONL + file-history | ✅ | ✅ | ✅ | ✅ 版本链 | ✅ | ✅ `claude --resume` |
| ZCode | SQLite（`~/.zcode/cli/db`） | ✅ | ✅ | ✅ | ⚠️ 文件事件（编辑内容在 raw 里） | ✅ 用量表 | ❌ 仅桌面端 |
| DSH | zstd JSONL（`~/.dsh/sessions`） | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ `dsh --resume` |
| Grok CLI | `chat_history.jsonl` + `summary.json` | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ `grok -r` |
| Cursor | `state.vscdb`（SQLite） | ✅ | ✅ | ❌ | ⚠️ 在 raw 里 | ⚠️ | ❌ 仅 IDE |
| Kiro IDE | workspace-session JSON | ✅ | ❌ 未持久化 | ❌ | ❌ | ❌ | ❌ 仅 IDE |
| Antigravity | 会话 SQLite（protobuf） | ⚠️ 启发式 | ⚠️ 启发式 | ⚠️ 文本 | ⚠️ 快照在磁盘 | ❌ | ❌ 仅 IDE |

Cursor 与 Antigravity 适配器标记为实验性：Cursor 只读解析它的 KV 存储，
Antigravity 用启发式方式解码 protobuf blob（无公开 schema）。
每个工具的逐字段可得性矩阵和数据源路径见 [docs/RECON.md](docs/RECON.md)。

## 设计一句话

Provider 的文件只读不改。Adapter 把各平台事件翻译成统一的 `Session` / `Event`
模型，同时**保留原始事件**——大字段按层级截断并带回源指针，信息不丢。全部落进
本地 SQLite + FTS5（trigram 分词，中文子串可搜）。扫描幂等：source 按
`(mtime, size)` 跟踪，变了才重解析；源文件消失的会话自动清理。

细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 和
[docs/DECISIONS.md](docs/DECISIONS.md)。

## 欢迎共建

自然的下一步：交互式 TUI、更多平台适配器、以及一个实时录制层——把 Agent 的
每一步操作录下来，让任何会话都能回放、或从任意一步换模型重跑。

## License

MIT — 见 [LICENSE](LICENSE)。
