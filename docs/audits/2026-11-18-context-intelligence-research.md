# Context Intelligence research — 2026-11-18

> 研究记录，不是 roadmap 承诺。最多选两个方向做小型实验；本轮选了 (1) 与 (2)，
> 数据在 `E:/sessionflow-scratch/r-full-review/context-intelligence-micro.json`。
> 没有调用 LLM、没有真实会话、没有改代码。

## 1. 七方向比较

| # | 方向 | 现有基础 | 可验证信号 | 成本 / 风险 | 判断 |
|---|---|---|---|---|---|
| 1 | Goal-aware Evidence Selection | `ranker.extract_candidate_facts/rank_candidates`；Bundle `## Goal-ranked evidence` | 改 goal 后 top-k 事实集是否稳定变化；目标噪声是否退出 top-k | 低；纯确定性排序 | **选为小实验** |
| 2 | Contradiction / supersession 显式化 | 手写 order + `Other sessions' conclusions` + `keep both readings` | 冲突说法是否并存；handoff order 是否参与 headline | 中；真正的“谁否定谁”仍需结构化声明 | **选为小实验** |
| 3 | Provenance-aware confidence | event origin、source_state、evidence digest | 未分类/缺失源是否被降级表述 | 中；不能把 MATCH 说成事实为真 | 延后；先要真实覆盖率 |
| 4 | Tiered L0/L1 adaptive packing | `build_tiered_bundle`, `l1_hard_max`, STRONG-first | 预算切分时 STRONG 是否被 WEAK 挤掉 | 中；已有 Codex 10k 限制经验 | 延后；本轮无新 provider 约束 |
| 5 | Retrieval-augmented exact pointers | `event_ref()`、`voyager show #seq/digest`、`evidence verify` | 引用是否可重算、是否 MISMATCH | 中低；适合与 3 合并 | 延后；不是本轮缺口 |
| 6 | Cross-agent state diff（git/files/tests） | `get_git_snapshot()`, commands/errors/files 段 | 最新说法是否被工具证据支持/反驳 | 中高；容易把“报告”误写成“验证” | 延后；需明确边界 |
| 7 | Model-assisted summary / selection | 现有 core 明确无 LLM 依赖 | 只有外部 opt-in 才能测 | 高；不可重复、成本、隐私 | 不进入本轮 |

## 2. 实验 1：Goal-aware Selection 是否真的对目标敏感

方法：同一个三会话 fixture（CI 会话 + UI 噪声会话），分别以 `fix CI pipeline` 与 `change UI color palette` 为 goal 构建 Bundle，只看 `## Goal-ranked evidence` 的 top 3。

| goal | top 1 | CI 事实在 top 3 | UI 噪声在 top 3 |
|---|---|---|---|
| `fix CI pipeline` | `GOAL_C fix CI pipeline` | 是 | 否 |
| `change UI color palette` | `NOISE_C UI color palette` | 是（`pytest -q` 仍在） | 是 |

**结论：CONFIRMED（有限）。** 排名对 goal 有明确响应，top 1 从 CI 目标切换到 UI 噪声目标；但 `pytest -q` 在两种目标下都留在 top 3，说明命令类事实的通用权重仍偏高。这个小实验支持“目标敏感”，不支持“目标能完全排除无关证据”。

## 3. 实验 2：矛盾是否被保留而不是被最新说法覆盖

方法：三会话 `PROPOSE X → DECIDED Y → NEXT implement Y` 与两会话 `config/db.py → temp/db.py`，用真实 thread handoff order 构建 Bundle。

| 观察 | 结果 |
|---|---|
| headline | `## Conclusion of the last session in handoff order` |
| 旧结论段落 | `## Other sessions' conclusions` 存在 |
| 明确提示 | `keep both readings` 存在 |
| 文件/命令证据 | `config/db.py` 与 `pytest -q` 保留 |

**结论：CONFIRMED（有限）。** 当前实现没有把“更新”伪装成“更正确”，也没有把旧说法静默删除。真正的 `X → Y (superseded)` 结构仍需要显式声明；本轮不把启发式并列写成已解决的语义冲突推理。

## 4. 推荐下一步（不实施）

1. 对方向 1：给 ranker 增加“目标词与工具命令的关联强度”小实验，先看 `pytest -q` 是否应随 goal 改变权重；不改用户可见 Bundle 结构。
2. 对方向 2：设计 additive 的结构化 supersedes 记录；必须保持“proposal 与 decision 都可追溯”，不得用最新时间戳裁决真值。
3. 方向 3/5 只有在真实 DSH provenance coverage 或 evidence MISMATCH 场景有需求时再启动。

## 5. 边界

这两个实验都使用合成数据与确定性评分；它们说明“上下文准备行为”，不说明模型最终是否完成任务。没有真实 Extension Host、真实 provider 会话或付费模型调用，也没有产生新的 Release 或 Schema 变更。
