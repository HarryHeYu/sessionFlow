# Future R&D route — 2026-11-18

> 基于本轮模块矩阵、双独立审评、交叉裁决、A–H benchmark 与两个 Context Intelligence 小实验。
> 这是**优先级建议**，不是已批准的 roadmap；本轮不实施以下路线，也不发 Release。

## 1. 排序原则

1. 先修会进入写路径或破坏连续性的确定性缺陷；
2. 再补真实验收边界，不能把 NOT_VERIFIED 写成完成；
3. 然后提升上下文选择/冲突表达，但必须保持确定性、离线、可引用；
4. 新 Provider 与更宽 UI 排在核心可靠性之后。

## 2. 方向比较

| 优先级 | 方向 | 为什么现在做 / 不做 | 最近可执行下一步 | 验收边界 |
|---:|---|---|---|---|
| 1 | **Reliable Continuity** | V01/V02 已进入修复；WorkThread 选择、租约、ambiguity、stale response 都直接影响“继续哪份工作” | 合并 V01/V02；V03 generation 与 V04 probe 分类作为独立候选 | 写路径必须显式选线程；坏请求不能杀桥；不能用 UI 默认代替用户选择 |
| 2 | **VS Code UX / Real Extension Host** | UI 是唯一用户可见面，但真实 Extension Host 仍 NOT_VERIFIED；V01 属于此类 | 在真实 VS Code 内复测 one-click switch、selector、composer；之后才谈 VSIX | Core #59 保持 OPEN 直到实机通过；DOM stub 不算 |
| 3 | **Cross-provider Live Verification** | Core #30、Plugin #2、Native Resume 都卡在真实环境；这是产品可信度上限 | 只在授权/隔离环境做；先 DSH 真实索引 provenance，再 OpenCode 真版本、Native Resume | 没有真实证据就保持 NOT_VERIFIED；不得用合成 fixture 替代 |
| 4 | **Context Intelligence** | 小实验显示 goal 排序有响应，但命令类事实权重偏高；冲突并列已保留，未做真 supersedes | 先做 ranker 权重的小实验；再设计 additive supersedes 结构 | 不改变 Core 依赖（无 LLM）；不把引用一致性写成事实为真 |
| 5 | **Benchmark / Research** | A–H 只测上下文准备质量，ignorance of real agent success 很明显；可作为后续实验基础 | 增加真实模型 opt-in 重复试验前，先决定是否值得隐私/成本 | 永远标注 no-LLM / synthetic；不报成功率 |
| 6 | **DSH Agent Discovery** | 模型工具选择只有 OBSERVED, NOT REPEATABLE；改描述曾被证明是坏方向 | 杠杆在提示词/文档侧，且需固定场景集重复测量 | 不得用强制 tool-call 数量当成功；不改六个/七个工具名 |
| 7 | **新 Provider** | 九个 Provider 已覆盖主要格式；新增会扩大 schema 漂移面，本轮无真实需求 | 推迟；除非出现真实安装且格式可确认的 provider | 只读 adapter、合成 fixture、能力矩阵同步 |
| 8 | **多 Agent 协作界面** | 目前只有单 WorkThread 的选择/时间线/预览；多 Agent 并发写仍由租约禁止 | 等真实 Extension Host 与 reliable continuity 完成后再讨论 | 不绕开 single-writer lease；不做多写镜像 |

## 3. 两周内的合理切片（仅建议）

- **Slice A（可靠性）**：V03 selector generation、V04 plugin probe 分类；两者各有可运行反例，互不依赖。
- **Slice B（验收）**：Core #59 真实 Extension Host 复测；Plugin #2 在授权索引上测 store-level coverage。
- **Slice C（研究）**：ranker 权重实验 + supersedes 结构设计文档；先 ADR，不直接改 Bundle 契约。

## 4. 明确不做

- 不因“Open Issues 变少”降低验收标准。
- 不把 README/ROADMAP 的排序写成已承诺排期。
- 不在没有真实环境时新增 Provider 或宣布 Native Resume 可用。
- 不为 Context Intelligence 把 LLM 引入 core 默认路径。
