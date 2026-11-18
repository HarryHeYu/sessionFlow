# Continuity effectiveness benchmark — 2026-11-18

> **One round's synthetic experiment, not an agent-success benchmark.**
> 本轮没有 LLM / paid provider 调用。它测的是“交给下一个 agent 之前的上下文准备质量”：
> 必要事实是否保留、跨会话矛盾是否被标明、retention/ambiguity/provenance 是否可解释、预算是否有效。
> 不能写成“sessionFlow 让 agent 成功率提高 X%”。

## 1. 设置

| 项 | 值 |
|---|---|
| Host | Windows 11, Git Bash, Python 3.13.12 |
| Core | `971df2d`, local import |
| Data | synthetic only；隔离 DB `E:/sessionflow-scratch/r-full-review/continuity-benchmark/benchmark-1795016890.db` |
| Harness | `E:/sessionflow-scratch/r-full-review/continuity-benchmark.py` |
| Raw verdict | `E:/sessionflow-scratch/r-full-review/continuity-benchmark/verdict.json` |
| No real data | 未读取真实会话正文，未写真实 home/index |

比较四种输入：

1. **no_history** — 空上下文；
2. **raw_history** — 全部事件文本与命令直接拼接；
3. **latest_summary** — 最新会话全文 + 每个会话标题的朴素摘要；
4. **sessionFlow Bundle** — 真实 `build_continuation_bundle()`；E 场景用 `compact` 预算，G 场景用真实 `api.current_work()` 的 ambiguity 字段。

计分：`required_hits / required_total` 是预定义必要事实的命中数；`tokens` 用 chars/4 估计，只表示体积，不代表质量。

## 2. A–H 最小实验集与结果

| ID | 场景 | 必要事实 | no_history | raw_history | latest_summary | sessionFlow Bundle |
|---|---|---:|---:|---:|---:|---:|
| A | 单会话目标与失败命令 | 4 | 0/4 · 0 tok | 4/4 · 17 tok | 4/4 · 21 tok | 4/4 · 272 tok |
| B | 三会话提案→否决→实施 | 4 | 0/4 · 0 tok | 3/4 · 30 tok | 1/4 · 21 tok | 4/4 · 405 tok |
| C | 目标条件化（CI vs UI 噪声） | 2 | 0/2 · 0 tok | 2/2 · 28 tok | 0/2 · 22 tok | 2/2 · 401 tok |
| D | 最新说法与早先证据冲突 | 4 | 0/4 · 0 tok | 3/4 · 20 tok | 1/4 · 13 tok | 4/4 · 370 tok |
| E | compact 预算保留关键命令 | 2 | 0/2 · 0 tok | 2/2 · 825 tok | 2/2 · 829 tok | 2/2 · 310 tok |
| F | source missing 的可解释性 | 3 | 0/3 · 0 tok | 1/3 · 15 tok | 1/3 · 20 tok | 3/3 · 243 tok |
| G | 多活跃 WorkThread 歧义 | 3 | 0/3 · 0 tok | 0/3 · 9 tok | 0/3 · 13 tok | 3/3 · 116 tok |
| H | 重复命令与长噪声 | 2 | 0/2 · 0 tok | 2/2 · 958 tok | 2/2 · 962 tok | 2/2 · 666 tok |
| **合计** |  | **24** | **0/24** | **17/24** | **11/24** | **24/24** |

## 3. 逐场景判断

- **A**：三者都能覆盖简单单会话事实；Bundle 明显更长，说明它不该只用来替代“贴原文”。
- **B**：只有 Bundle 同时保留 X、Y、下一步，并给出 `Other sessions' conclusions` 的“keep both readings”说明。latest/summary 把旧结论压掉。
- **C**：Bundle 的 `## Goal-ranked evidence` 存在；目标相关两条都进入前 3，UI 噪声未进入前 3。注意：这测的是排序段落，不是模型是否真的忽略后文噪声。
- **D**：Bundle 同时保留 `config/db.py`、最近说法 `temp/db.py`、文件证据和“keep both readings”；raw 只留下了大部分字面事实，latest 只留下最近说法。
- **E**：compact 预算把 825/829 token 的朴素上下文降到 310 token，且仍保留“rotate leaked token immediately”。这是预算有效性的直接证据，不是全场景体积优势。
- **F**：只有 Bundle 明说 `source not available` 与 `SOURCE_MISSING`；这与 O2 保留语义一致。
- **G**：只有 Bridge 的 `current_work()` 暴露 `thread_ambiguous`、`thread_candidates`、`thread_default_reason`；拼接文本无法表达“默认不是选择”。
- **H**：Bundle 比 raw/latest 短约 30%，并去重命令；但噪声仍占用一定体积，说明去重不是压缩算法。

## 4. 结论与边界

1. 在“必要事实保留”这个定义上，Bundle 24/24；raw 17/24；latest/summary 11/24；无历史 0/24。
2. Bundle 的优势主要来自**结构**：跨会话结论、repository/files/commands/errors、证据来源、retention 与 ambiguity 标签；不是来自“更少 token”。
3. compact 预算在特定噪声场景下有效，但没有全场景压缩保证。
4. 本实验**不能**证明真实 agent 的完成率、正确率或节省成本；那需要授权真实会话与真实模型重复试验。
5. 本实验没有创造新功能、没有写真实索引、没有改 Bridge/Store schema。
