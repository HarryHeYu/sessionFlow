# Cross-review adjudication — 2026-11-18

> Phase 4：把两份独立报告的观察放在同一张表上裁决。
> 先有独立的 JYY 与 Jyy1 报告，再交叉，不用后一份去补前一份的结论。
> 判定词：`CONFIRMED` / `SUSPECTED` / `DESIGN GAP` / `NOT VERIFIED`。

## 1. 裁决表

| ID | Module | Spec | Observation | Reproduction | Impact | Severity | Verdict | Proposed Fix |
|---|---|---|---|---|---|---|---|---|
| V01 | VS Code one-click switch | 树项的 `threadId` 是命令的显式目标；一键切换必须作用于所选 WorkThread | `extension.js:447-466` 校验 `item.threadId` 后仍发送 `voyager switch ${target}`，未带 `--thread` | `probe-one-click-switch.js` → `sent="voyager switch codex"`, `includesThread=false` | 多活跃线程时把租约/pending/可能启动落到错误线程 | **High** | **CONFIRMED** | 发送显式 `--thread`；参数加最小引号；测试断言命令包含所选 id |
| V02 | Core stdio bridge | `tests/test_api.py` 声明坏参数返回 error，绝不崩溃；长生命周期客户端依赖“一条请求不杀死流” | `api.py:574` 的 `_OPS.get(op)` 在 try 外；`op=[]` 抛 `TypeError`，后续行无响应 | `probe-bridge-unhashable-op.py`：1st ok、2nd/3rd 空、stderr `unhashable type: 'list'` | VS Code 单一 bridge 进程被一条畸形消息打死；插件 one-shot 影响较小 | **High** | **CONFIRMED** | 先校验请求信封（`dict` / `op:str` / `params:dict`），再 dispatch；真实子进程回归测试验证坏请求后仍可继续 |
| V03 | VS Code Selector | `thread_id` 是关联 token；`detailGeneration` 被调用方注释称为 generation，但响应比较只检查 thread_id | `selector.js:121-122,182-189,275,304-313` 递增 generation 却从不比较；同线程旧响应可覆盖新响应 | `probe-selector-generation.js` → `showsNew=false`, `showsStale=true` | 错误/过期详情短暂显示；不会写 WorkThread | Medium | **CONFIRMED** | 在 select 消息中带 generation，响应回显；只有 generation 与当前值相等才渲染；保留 thread_id 双重校验 |
| V04 | Plugin discovery probe | `ProbeFailure.found` 区分“找到但拒绝”和“没找到”；具体错误必须可行动 | `bridge.ts:252-287` 把 `JSON.parse` 异常与 spawn 失败同归 `found:false` | `probe-plugin-probe-json.mjs` → core 输出 `not json` 且 exit 0，却被报 `VOYAGER_NOT_FOUND` | 安装诊断把配置/输出问题误报成未安装 | Medium | **CONFIRMED** | 把 JSON 解析失败单独分类为 `found:true` 的 config/protocol error，保留原 stderr/stdout 摘要 |
| V05 | Plugin `callOp()` | 与 `runJson()` 的失败语义一致：子进程非零退出不应被静默当成成功 | `bridge.ts:428-449` 不检查 `RunResult.code`；`runJson()` 检查 | `probe-plugin-callop-exit.mjs` → 输出 result 后 exit 3 仍被接受 | 真实 Core 正常路径不触发；异常 core/wrapper 下可能掩盖失败 | Low | **CONFIRMED**，但本轮不实施 | 单独评估 Core op 是否可能合法非零退出；不得为了对称性直接改契约 |
| V06 | DSH provenance | `role=user` 不是 human；结构字段才能分类 | Core classifier 与 12 文件结构样本一致，197/197 user rows 有可映射结构 | `docs/audits/2026-11-dsh-provenance-structure.md`；`test_dsh.py` | 真索引 store-level coverage 仍未知 | Medium | **PARTIAL / NOT VERIFIED** | 保持 Plugin #2 OPEN；取得授权真实索引后再测，不能用 fixture 100% 替代 |
| V07 | Plugin/Core capability | Plugin v1.3.0 对 Core v1.5.0，bridge schema 1 | Core main 新增 `merge --repo`，Plugin merge tool 无 `repo` 参数 | `src/tools/merge.ts:30-39` vs `voyager/cli.py` + `tests/test_merge_repo.py` | 用户无法从 DSH 指定新线程 repo_root；旧行为不变 | Low | **DESIGN GAP**（后发布能力） | 不作为 bug 修复；若产品要暴露，单独 additive 工具参数 PR |
| V08 | Core ref pin | 安装提示应与 README 发布 pin 可解释 | `REQUIRED_CORE_REF=f2a6445`，README pin `v1.5.0` | `docs/release-v1.3.0.md:130-131` 已显式记录这是已知差异 | 诊断信息可能指向历史 commit | Low | **DESIGN GAP**（已记录） | 不在本轮改 bridge；若变更需单独决策与发布说明 |
| V09 | VS Code 真实 Extension Host | 功能必须在真实 VS Code Extension Host 验证 | 本轮只用静态检查与 node:vm DOM stub | 未启动 VS Code | 不能把 UI 脚本通过写成实机通过 | — | **NOT VERIFIED** | 保持 Core #59 OPEN；在真实 Extension Host 中重测 V01/V03 修复 |
| V10 | Continuity Engine 单一引擎 | `handoff_thread()` 是唯一写 handoff；ambiguity 不得被 act 路径静默选择 | 源码与端到端测试一致；read-only preview 标 ambiguous/default | `docs/API.md:114-147`；`tests/test_handoff_convergence.py` 等 | 无新缺陷证据 | — | **VERIFIED (scope-limited)** | 不改；不因审评制造重构 |

## 2. 交叉结论

1. **没有互相矛盾的确定结论。** JYY 发现都在 Core/UI 状态与桥接层；Jyy1 发现都在 Plugin 诊断与能力边界；provenance 两边都停在 `NOT VERIFIED`。
2. **实施门槛**：本轮只把 High 且能红先绿后的 V01、V02 作为必须修复；V03 与 V04 是真实缺陷但影响面较低，记录为后续候选，不为凑 PR 而混入。
3. **不可升级项**：V06/V09/V10 的证据边界保持不变。V05 虽有可运行反例，但真实 Core 正常路径不触发，先做契约评估再决定是否修。
4. **发布边界**：上述修复都不改 Bridge/Store schema、不发 Release、不改 Tag；若进入 main，仅更新 Unreleased 记录。

## 3. 本轮明确不做

- 不把 V07/V08 当作版本一致性事故；它们已被文档记录，改动属于新决策。
- 不为 Plugin #2 注入真实用户索引或读取真实消息正文。
- 不用合成 fixture 或 DOM stub 声称 Extension Host 通过。
- 不新增 Provider，不做无目标重构，不修改历史 Release。
