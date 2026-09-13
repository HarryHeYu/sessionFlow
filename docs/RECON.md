# Voyager 平台侦察报告

> 2026-09-13 · 全部结论基于本机真实数据验证，非文档推断。
> 调查范围：9 个平台 + 未安装清点。

## 0. 平台清点结果

| 平台 | 状态 | 说明 |
|---|---|---|
| Codex CLI / VS Code 扩展 / Desktop | 已装 | 三端**共用** `~/.codex` 存储（originator 区分：vscode 58 / desktop 20 / cli） |
| Claude Code 2.1.78 | 已装 | 40 个 session |
| DSH 0.1.5-rc.1 | 已装 | 60+ session，zstd 压缩 |
| ZCode 0.16.5 | 已装 | SQLite 主库，38 session |
| Grok CLI 1.0.5 | 已装 | 每会话一目录 + FTS5 搜索库 |
| Kiro IDE (v?) / Kiro CLI 0.12.263 | 已装 | CLI 实为 headless，**无 session 文件**；IDE 数据在 globalStorage |
| Antigravity | 已装 | SQLite + protobuf，`.pb` 加密 |
| Cursor 3.19.13 | 已装 | state.vscdb 的 cursorDiskKV（12213 行 KV） |
| Cline 3.72.0 | 已装但**任务数据为空** | 框架在、无历史任务 |
| Copilot CLI | 已装但无会话数据 | 仅进程启动日志 |
| ChatGPT Desktop / Gemini CLI / Aider / Continue / OpenCode / Goose / Roo / Kiro CLI sessions | NOT INSTALLED 或无数据 | Gemini 数据被 Antigravity 复用（`~/.gemini/antigravity/`） |

## 1. Session 路径清单（本机实测）

```
Codex       ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>_<window_id>.jsonl
            + ~/.codex/state_5.sqlite (threads: git_sha/branch/origin_url)
            + ~/.codex/thread_history_1.sqlite, session_index.jsonl
Claude      ~/.claude/projects/<munged-cwd>/<sessionId>.jsonl
            + ~/.claude/file-history/<sessionId>/<hash>@vN (文件全量版本链!)
            + ~/.claude/history.jsonl (全局提示历史)
DSH         ~/.dsh/sessions/<munged-cwd>/session-<uuid>/session.jsonl.zstd  (zstd!)
            + ~/.dsh/storages/session_projcache.json (耗时统计)
ZCode       ~/.zcode/cli/db/db.sqlite (38 sessions, WAL)
            + ~/.zcode/cli/rollout/model-io-sess_*.jsonl (LLM 请求/响应原文, 15.9MB 级)
            + ~/.zcode/v2/checkpoints/<workspace>/ (加密快照 manifest)
Grok        ~/.grok/sessions/<URL编码cwd>/session-<uuidv7>/
              ├ chat_history.jsonl (消息+tool_result 正文)
              ├ events.jsonl  ├ updates.jsonl (ACP 流)  ├ rewind_points.jsonl
              └ summary.json (git_root_dir/remotes/head_commit/branch!)
            + ~/.grok/sessions/session_search.sqlite (FTS5, 可借鉴其索引设计)
Kiro IDE    ~/AppData/Roaming/Kiro/User/globalStorage/kiro.kiroagent/
              └ workspace-sessions/<base64url(cwd)>/<uuid>.json + sessions.json
            + dev_data/tokens_generated.jsonl (token 按次追加，无 session 关联)
Antigravity ~/.gemini/antigravity/conversations/<uuid>.db (SQLite: steps 表, protobuf blob)
            + conversations/<uuid>.pb (加密, 不可读)
            + ~/.gemini/antigravity/code_tracker/{active,history}/<repo>_<commit>/<hash>_<file> (全文件快照)
            + ~/.gemini/config/projects/<uuid>.json (gitFolder/defaultBranch)
Cursor      ~/AppData/Roaming/Cursor/User/globalStorage/state.vscdb
              └ cursorDiskKV: composerData:*(45 会话) bubbleId:*(9981 消息)
                 agentKv:*(1994 完整 LLM 请求) inlineDiff:/checkpointId:*
```

## 2. Schema 对比（关键 event type）

| 平台 | 容器 | 消息 | Tool Call | Tool Result | 文件修改 |
|---|---|---|---|---|---|
| Codex | 行 `{timestamp,ordinal,type,payload}` | response_item/message | function_call{name,args,call_id} + custom_tool_call{input 脚本} | function_call_output 含 `Exit code: N`；custom 版有 {exit_code, wall_time, output} 数组 | ❌ 无结构化 diff（apply_patch 混在脚本里） |
| Claude | 行 `{uuid,parentUuid,timestamp,cwd,gitBranch,sessionId,...}` | user/assistant (content blocks) | tool_use{name,input,id} | tool_result + 行级 toolUseResult{stdout,exit code} | ✅✅ file-history-snapshot + `<hash>@vN` 全量版本链 + Edit 的 old/new_string |
| DSH | 行 `{type,seq,time(ms),data}` | user/message, assistant/message（**reasoning 全文内嵌**） | tool/call{callId,name,arguments} | tool/result（stdout 有，无 exit code/stderr） | ❌ |
| ZCode | SQLite `message`/`part` 表 | message.data JSON（role/model/cost/tokens/path.cwd+root） | part type=tool: {callID,tool,state{input,output,metadata}} | tool_usage 表：exit_code/stdout_bytes/stderr_bytes/审批 | ✅ Edit part 内 old_string/new_string + 加密 checkpoints |
| Grok | chat_history.jsonl（OpenAI 风格） | user/assistant/**reasoning(encrypted_content 加密)** | tool_calls[{id,name,arguments}] | tool_result 正文在 chat_history；events.jsonl 有 duration/outcome | rewind_points 的 file_snapshots（多空） |
| Kiro | JSON 会话文件 `history[]` | user/assistant（content parts: text/mention/file） | ❌ 不持久化（只在 promptLogs 渲染后的 prompt 文本里） | ❌ | ❌ |
| Antigravity | SQLite `steps(idx,step_type,step_payload blob)` | type 15 流式消息 blob | type 132: call_<id>+工具名+JSON 参数（run_command/view_file/write_to_file/grep_search…） | type 101 通知含 "exited with code N" | code_tracker 全文件快照（active/history，按 repo_commit 分目录）✅ |
| Cursor | KV blob（JSON） | bubble type 1/2 | toolFormerData{toolCallId,name,rawArgs,params,result,审批数据} | 结果+consoleLogs+interpreterResults | ✅✅ inlineDiff/gitDiffs/fileDiffTrajectories/checkpointId，统计 12213 行 |

## 3. 字段可得性矩阵（✅实测 / ⭕部分 / ❌无）

| 字段 | Codex | Claude | DSH | ZCode | Grok | Kiro | Antigravity | Cursor |
|---|---|---|---|---|---|---|---|---|
| session_id | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅(db名) | ✅(composerId) |
| parent/fork | ✅ history_base+spawn_edges | ⭕ fork=新ID | ❌ | ✅ parent_id | ⭕ session_relationship | ⭕ "(Continued)" | ? | ✅ subagentComposerIds |
| 时间戳 | ✅ 每行 | ✅ 每行 | ✅ ms | ✅ | ✅ | ✅ | ⭕ 在文本内 | ✅ |
| cwd | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| git commit | ✅ | ❌ (仅 branch) | ❌ | ⭕ root | ✅ head_commit | ❌ | ✅ 目录名 | ⭕ trackedGitRepos |
| 用户/assistant 消息 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ blob | ✅ |
| reasoning | ⭕ 加密 blob | ✅ 明文 | ✅ 明文 | ✅ | ❌ 加密 | ⭕ | ⭕ blob | ✅ allThinkingBlocks |
| tool call | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ |
| shell exit code | ✅ | ✅ | ❌ | ✅ | ⭕ 文本 | ❌ | ⭕ 文本 | ✅ |
| 文件 diff | ❌ | ✅✅ | ❌ | ✅ | ⭕ 快照 | ❌ | ✅ 快照 | ✅✅ |
| token in/out/cache | ✅✅ 全 | ✅ | ❌ | ✅✅ model_usage 表 | ❌ | ⭕ 全局无 session | ❌ | ⭕ 常为 0 |
| cost | ❌ (rate_limits) | ❌ | ❌ | ✅ cost 字段 | ❌ | ❌ | ❌ | ❌ |
| context window | ✅ 258400 | ⭕ | ✅ 1M | ⭕ | ❌ | ✅ % | ❌ | ⭕ |
| error | ⭕ turn_aborted | ✅ api_error | ❌ | ✅ error_type | ⭕ outcome | ✅ status=failed | ✅ type 17 | ✅ |

## 4. Resume 能力

```text
Codex       codex resume <id|--last> [--all]  ✅ 任意旧会话 ✅ codex fork ✅ codex apply
Claude      claude -r <sessionId> / -c / --fork-session / --session-id  ✅ 任意 ✅ fork ✅ --add-dir
DSH         dsh --profile tui --resume <session>  ✅
ZCode       桌面端 resume（CLI 可执行未在 PATH；DB 有完整 parent/title 供恢复）⭕
Grok        grok -r <id|title> / --continue / --fork-session / --restore-code / sessions restore  ✅
Kiro        IDE 内继续（"(Continued)"）；CLI 无 session 命令  ❌ CLI
Antigravity 无 CLI；IDE 重开 cascade  ❌
Cursor      IDE 常驻会话  ❌ CLI
```

**Voyager `resume` 可实现**：Codex / Claude / DSH / Grok 直接调原 CLI；ZCode 需进一步确认 CLI 入口；Kiro/Antigravity/Cursor 只能"打开 IDE + 提示上下文"级别。

## 5. Hook 能力（Phase 2 关键）

```text
Claude Code  ✅ 原生 hooks（PreToolUse/PostToolUse/Stop，settings.json）—— 本机未配置，随装随用 → 首选录制对象
Codex        ⚠️ 无 Pre/Post hook；有 notify + MCP + apply/fork 子命令。录制方案：包装 codex 二进制 or 读 rollout 文件增量 tail（rollout 本身就是准实时黑匣子！）
ZCode        ⭕ 支持 hooks（本机未配置）+ checkpoints + model-io rollout tail
DSH/Grok/Kiro/Antigravity/Cursor  ❌ 无 hook；Grok/Antigravity 的 events/code_tracker 可 tail 增量读
```

**关键洞察**：Codex/ZCode/Grok 的本地文件本身就是实时追加的事件流 → Phase 2 的"黑匣子"对这三家可以**零侵入 tail 实现**；Claude 用原生 hooks 补齐；其余平台降级为只读聚合。

## 6. Voyager 统一数据模型

```sql
-- 核心表（normalized + raw 双写，原始事件永不丢弃）
session(id TEXT PK, provider TEXT, agent TEXT,        -- agent=cli|vscode|desktop...
        model TEXT, started_at INT, ended_at INT,
        cwd TEXT, repo_root TEXT, remote_url TEXT, branch TEXT, commit TEXT,
        title TEXT, summary TEXT,
        parent_session_id TEXT,                       -- fork/续接/子代理
        source_path TEXT,                             -- 原始数据定位
        resume_cmd TEXT,                              -- 平台恢复命令模板
        meta_json TEXT)                               -- 平台特有字段全量保留

event(id INTEGER PK, session_id TEXT, ts INT, seq INT,
      kind TEXT,        -- user|assistant|reasoning|tool_call|tool_result|error|snapshot|usage|meta
      actor TEXT,       -- user|model|tool|system
      tool TEXT, call_id TEXT,
      command TEXT, exit_code INT,
      files_json TEXT,  -- [{path, before_hash, after_hash}]
      diff TEXT,        -- 有则存
      text TEXT,        -- 消息/输出正文（FTS5 索引）
      usage_json TEXT,  -- {input,output,cache_read,cache_write,cost,context_window}
      raw_json TEXT)    -- 原始事件（不丢字段）

FTS: event_text(text) -- 全文搜索
```

## 7. 适配器优先级

**第一批（数据最全、格式最干净）**
1. **Claude Code** — JSONL 明文、reasoning 明文、file-history 版本链、原生 hooks、resume 完整
2. **Codex** — JSONL、git 三件套、token 全、fork 链、resume/fork 完整
3. **ZCode** — SQLite 直读、model_usage 表、Edit before/after

**第二批**
4. **DSH** — zstd JSONL（需 zstandard），字段略稀但结构规整
5. **Grok** — summary.json（git 全套）+ chat_history + FTS
6. **Cursor** — state.vscdb 只读解析（diff 数据最全但 KV 结构杂）

**第三批（降级聚合）**
7. **Antigravity** — protobuf blob 解析成本高，先只索引 session 级元数据 + code_tracker 快照
8. **Kiro** — 只有会话级 JSON（无 tool/diff），列表+标题聚合
9. Cline/Copilot — 数据为空，留接口

## 8. Phase 1 MVP 目录结构

```
voyager/
├── pyproject.toml            # entry: voyager = voyager.cli:main
├── README.md  LICENSE
├── voyager/
│   ├── cli.py                # scan / list / show / search / repo / export / resume
│   ├── store.py              # SQLite schema + upsert
│   ├── model.py              # dataclass: Session / Event
│   ├── adapters/
│   │   ├── base.py           # Adapter 协议：discover() -> [SessionRef], load(ref) -> [Event]
│   │   ├── claude_code.py    # + file-history diff 重建
│   │   ├── codex.py          # + thread_spawn_edges fork 链
│   │   ├── zcode.py          # + model_usage 聚合
│   │   ├── dsh.py            # zstd 解压
│   │   ├── grok.py
│   │   ├── cursor.py         # vscdb ro 打开
│   │   ├── antigravity.py    # 元数据级
│   │   └── kiro.py
│   ├── export.py             # Markdown / JSON
│   ├── handoff.py            # Context Package 生成（Phase 1 末）
│   └── tui/                  # Phase 1 后半: Textual
└── tests/
```

命令面（MVP）：
```bash
voyager scan                  # 发现+索引全部平台
voyager list [--platform X] [--repo E:/code/xxx] [--since 7d]
voyager show <id> [--diff] [--raw]
voyager search <query>        # FTS5
voyager repo <path>           # 跨 agent 的 repo 时间线
voyager export <id> [-m md|json]
voyager resume <id>           # 生成并执行平台 resume 命令
```

## 9. 未确认问题

1. ZCode 桌面端 resume 的 CLI 入口（`zcode` 不在 PATH，是否 `--resume sess_xxx` 存在待验证）
2. Antigravity `steps.step_payload` protobuf 无 schema —— 需要用 protobuf 反射或找 wire type 规律，成本待评估
3. Cursor token 为 0 是否写在其他 key（agentKv 的请求侧 usageData 未深挖）
4. Codex reasoning 的 encrypted_content 确认无法本地解密（服务端密钥）
5. Kiro CLI（Amazon Q 架构）是否在别的盘存 session（本机 ~/.local/share 为空）
6. DSH 是否有隐藏的 exit code 字段（tool/result content 深层未穷举）
7. VS Code Copilot Chat 会话存在 `~/AppData/Roaming/Code/User/workspaceStorage/<hash>/state.vscdb` 的可能性未排除
