# Voyager 架构

![架构图](screenshots/architecture.png)

矢量版：`screenshots/architecture.svg`（同一份布局代码产出，便于继续微调）。
生成脚本：`scripts/make_diagram.py` —— 改平台清单或文案后重跑即可；
布局不变量（左右列等宽、左右堆叠等高、外边距一致、index 框居中、文字不溢出）
由脚本内的 `_check_layout()` 与 `tests/test_diagram.py` 双重守住。

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
- `sources` 表以 `(provider, path, sid)` 为主键记录 `(mtime, size)` —— 多会话源
  （一个 SQLite DB 对应 N 个 session）每个 session 一行，prune 永不误伤同库其他会话。
- 变更的 source 原子重写：`DELETE events WHERE sid=?` + `DELETE event_fts WHERE rowid IN (...)` + 重新 INSERT，包在事务里。
- prune 只删「磁盘上已无任何 source 行」的 session；未变更/解析失败的文件不触发 prune。
  multi-adapter 的 scan 抛异常（如 DB 被锁）时本轮保留现有索引，不视为空。
- 多会话源（zcode SQLite）：mtime 变化才全量重扫 + 按 live_ids prune；不变则整库跳过。
  Codex 按"分组内所有文件未变更则跳过该分组"做文件级增量。

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
`content/tool_input/tool_output/stdout` 截 600KB，`raw_json` 截 8KB，FTS body 截
600B——完整数据始终在 provider 原始文件里，事件里保留 source 指针（sources.path + seq）。

## 测试与 CI

适配器是唯一会因为"上游改格式"而悄悄坏掉的部分，所以每个平台一份回归测试：

```
tests/fixtures/<provider>/…   合成数据（文本 JSON/JSONL + SQL 种子，无二进制、无真实会话）
tests/conftest.py             在 tmp_path 里物化 fixture（SQLite / zstd 现场生成），
                              adapter_of + patch_paths 把适配器的路径全局指过去
tests/test_<provider>.py      每个适配器：字段 + 事件种类断言（格式漂移即失败）
tests/test_store.py           幂等/替换/prune 的索引层契约
tests/test_export.py test_handoff.py test_cli.py test_mcp.py
```

- 跑全量：`python -m pytest tests/ -q`；只装核心依赖时：`python scripts/run_tests_core_only.py`
  （block 掉 `mcp`/`zstandard`，相关测试自动 skip——即 `pip install voyager` 的真实形态）。
- CI（`.github/workflows/test.yml`）：Python 3.10–3.13（Linux）× 3.10/3.13（Windows）
  + 一个"零可选依赖"任务。
- 适配器的路径全局（`SESSIONS_DIR`/`PROJECTS_DIR`/`DB_PATH`/`VSCDB`/`CONV_DIR`）必须
  保持是模块级变量，测试靠它们重定向；`patch_paths` 会对不存在名字直接断言失败。

## Continuity Engine（已落地）与预留面

**Continuity Engine 已落地**：多会话合成（`voyager merge` / continuity.py）、
WorkThread（threads/thread_sessions/租约表）、goal ranker（ranker.py）、
Context Budget（budget.py）、Skill 安装器、`voyager switch`、本地 API
（api.py + `voyager api serve`）。当前架构见 [ROADMAP.zh-CN.md](ROADMAP.zh-CN.md)。

索引层仍为后续特性留有钩子（这些属于 docs/POST-1.0.md 的 backlog，**未实现**）：

索引层已为后续特性留了钩子，但不挡 Continuity：

- `files` 表已建：`voyager diff/files` 走 Claude file-history 版本链（`<hash>@vN`）。
- 录制层（Flight Recorder，**未实现**）：Claude 原生 hooks（PreToolUse/PostToolUse）+ Codex/ZCode
  rollout 文件 tail（零侵入）。统一事件模型可直接承载录制事件（kind/actor/tool/...）。
- fork：session 表 parent 语义已留（Codex history_base、Claude --fork-session、
  Grok rewind_points）。
