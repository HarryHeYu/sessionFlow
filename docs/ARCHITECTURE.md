# Voyager 架构

## 数据流

```
Provider Data (read-only, never modified)
     │
     ▼
Adapter (per-platform)          voyager/adapters/{codex,claude,zcode,dsh,...}.py
     │   discover() → sources
     │   parse(source) → {session, events, extra_sources}
     │   [or] scan() → [bundle, ...]          # multi-session sources (SQLite)
     ▼
Normalized Session / Event      voyager/model.py  (plain dicts + field whitelist)
     │   every event keeps raw_event (provider-native)
     ▼
Store (SQLite + FTS5)           voyager/store.py
     │   sources 表: (provider, path, mtime, size, sid) → 幂等
     │   events.rowid == event_fts.rowid → per-session FTS rebuild
     ▼
CLI                             voyager/cli.py  (scan/list/show/search/repo/export/resume/files/diff)
Export                          voyager/export.py (md / json)
```

## 关键机制

### 幂等扫描
- 每个 source 以 `(provider, path)` 为主键记录 `(mtime, size)`；不变则跳过。
- 变更的 source 原子重写：`DELETE events WHERE sid=?` + `DELETE event_fts WHERE rowid IN (...)` + 重新 INSERT，包在事务里。
- 文件型 adapter（codex/claude/dsh）：prune 保留「磁盘上仍存在」的 source 对应的 session —— 未变更/解析失败的文件不会误删既有会话；只有从磁盘消失的文件才触发 prune。
- 多会话源（zcode SQLite）：mtime 变化才全量重扫 + 按 live_ids prune；不变则整库跳过。

### Repo 归属
优先级 `provider 提供的 git 元数据`（如 Codex session_meta.git、Grok summary.json）
→ 本机 `git -C <cwd> rev-parse`（root/branch/commit/remote，结果按 cwd 缓存）
→ cwd 兜底。`voyager repo <pattern>` 对 repo_root/remote/cwd 做子串匹配聚合。

### Resume
session 行存 `resume_cmd`（如 `codex resume <id>`）。`voyager resume <id>`
打印或执行；`can_resume=False` 的平台（zcode/kiro/antigravity/cursor）明确报
"unsupported"，不做伪实现。

### FTS
tokenize=trigram：CJK 子串可搜（≥3 字符）；rowid=events.id，按会话重建即删即插。

### 截断策略
`content/tool_input/tool_output/stdout` 截 2MB，`raw_json` 截 200KB——完整数据
始终在 provider 原始文件里，事件里保留 source 指针（sources.path + seq）。

## Phase 2 预留

- `files` 表已建：`voyager diff/files` 走 Claude file-history 版本链（`<hash>@vN`）。
- 录制层（Flight Recorder）：Claude 原生 hooks（PreToolUse/PostToolUse）+ Codex/ZCode
  rollout 文件 tail（零侵入）。统一事件模型可直接承载录制事件（kind/actor/tool/...）。
- fork：session 表 parent 语义已留（Codex history_base、Claude --fork-session、
  Grok rewind_points）。
