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

```sh
voyager scan                # 发现并索引所有支持的 Agent 会话
voyager list                # 全部会话，按更新时间排序
voyager list --repo myproj  # 某个 repo 的会话
voyager show <id>           # 完整时间线：消息 / reasoning / 工具调用 / 退出码
voyager search "tensorboard"
voyager repo E:/code/myproj # 一个 repo 的跨 Agent 时间线
voyager export <id> --format md   # 或 --format json（含原始事件）
voyager resume <id>         # 调起原 Agent 恢复该会话
voyager files <id>          # 该会话碰过哪些文件
voyager diff <id>           # Claude 会话：从版本链重建前后 diff
voyager stats               # 索引统计
```

会话 ID 支持前缀匹配；前缀有歧义时会列出候选并退出，不会猜。

## 支持的平台

| 平台 | 数据源 | 消息 | 工具调用 | Shell 退出码 | 文件 diff | Token | 恢复 |
|---|---|---|---|---|---|---|---|
| Codex（CLI/VSCode/桌面版） | rollout JSONL | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ `codex resume` |
| Claude Code | project JSONL + file-history | ✅ | ✅ | ✅ | ✅ 版本链 | ✅ | ✅ `claude --resume` |
| ZCode | SQLite（`~/.zcode/cli/db`） | ✅ | ✅ | ✅ | ⚠️ 文件事件（编辑内容在 raw 里） | ✅ 用量表 | ❌ 仅桌面端 |
| DSH | zstd JSONL（`~/.dsh/sessions`） | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ `dsh --resume` |

Grok、Cursor、Antigravity、Kiro 的适配方案已调研完毕，见
[docs/RECON.md](docs/RECON.md)（各家工具本地数据存哪、记了什么的完整清单），
按现有 Adapter 接口扩展即可。

## 设计一句话

Provider 的文件只读不改。Adapter 把各平台事件翻译成统一的 `Session` / `Event`
模型，同时**保留原始事件**——大字段按层级截断并带回源指针，信息不丢。全部落进
本地 SQLite + FTS5（trigram 分词，中文子串可搜）。扫描幂等：source 按
`(mtime, size)` 跟踪，变了才重解析；源文件消失的会话自动清理。

细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 和
[docs/DECISIONS.md](docs/DECISIONS.md)。

## 欢迎共建

自然的下一步：交互式 TUI、更多平台适配器、跨 Agent 接力（`voyager handoff
<id> --to codex` 生成统一上下文包）、以及一个实时录制层——把 Agent 的每一步
操作录下来，让任何会话都能回放、或从任意一步换模型重跑。

## License

MIT — 见 [LICENSE](LICENSE)。
