# JYY independent systems review — 2026-11-18

> 这是**独立**审评：在交叉裁决前，只使用 JYY 方法库的方法迁移，不引用 Jyy1 的结论。
> 方法来源：`E:/code/jyy/skill/SKILL.md`、`E:/code/jyy/cognitive/SKILL.md`。
> 这些目录是基于公开材料整理的知识库，本报告**不冒充蒋炎岩本人观点**。
> 证据等级：A=可直接运行/源码直读；B=多文档归纳；C=Skill 推理。

## 1. 审评边界

对象：Core `971df2d` 与 Plugin `0cf495b` 中与系统正确性相关的共同面——Continuity Engine、VS Code 状态机、API/stdio 契约。

不使用真实用户会话正文；破坏性实验使用合成数据、隔离目录或纯函数桩。
未运行真实 Extension Host，相关结论只标 `NOT_VERIFIED`。

## 2. 状态机优先：Selector 与一键切换

### 2.1 Selector 的应有状态机 [Skill 推理，C]

```
IDLE
  ├─ overview-ok ──> SELECTED(thread=T, generation=G)
  │                    └─ request(T,G) ──> RENDER(T,G) | ERROR(T,G)
  └─ overview-error ─> ERROR_STATE（不得渲染“没有线程”）

不变量 I1：只有 generation 等于当前 G 且 thread_id 等于当前 T 的 detail 响应可以渲染。
不变量 I2：refresh 必须使旧 generation 失效，或对当前选择重新发请求。
```

`selector.js:121-122` 确实声明了 `detailGeneration`，`selector.js:182-189` 与 `:275` 也递增它；但 `selector.js:304-313` 只比较 `msg.thread_id !== state.selected`，没有比较 generation。也就是说 I1 被实现成“当前线程”，而不是“当前请求”。

### 2.2 最小可运行反例 [A]

驱动：`E:/sessionflow-scratch/r-full-review/probe-selector-generation.js`。它在 node:vm 中执行 **shipped** `selector.js` 的内联脚本，注入 `A → B → A` 三个选择事件，并让第一笔 A 的响应晚于第二笔 A 返回。

实际输出：

```json
{
  "sent": ["THREAD-A", "THREAD-B", "THREAD-A"],
  "showsNew": false,
  "showsStale": true,
  "detailGenerationUsedInCode": false
}
```

**判定：CONFIRMED（中等严重度）。** 同一线程的旧响应能覆盖新响应；当前测试 `tests/test_selector_race.py` 只覆盖不同线程与无 correlation id，没有覆盖同线程两次请求。

### 2.3 一键切换的应有状态机 [Skill 推理，C]

```
树项 item.threadId = T
  └─ 用户点击 switch(T) ──> 命令必须携带 T ──> handoff 引擎只处理 T
```

`extension.js:447-466` 接收并验证了 `item.threadId`，但随后发送的字符串是 `voyager switch ${target}`，没有 `--thread ${item.threadId}`。`tests/test_vscode_extension.py:250-269` 甚至把这条字符串钉成了契约，因此测试通过不代表功能满足 UI 语义。

驱动：`E:/sessionflow-scratch/r-full-review/probe-one-click-switch.js`，用最小 vscode 桩执行 shipped `extension.js`，注册命令后传入 `{threadId: "thr-EXPECTED"}`。

实际输出：

```json
{
  "sent": "voyager switch codex",
  "includesThread": false
}
```

**判定：CONFIRMED（高严重度）。** 按钮声称对所选 WorkThread 切换，实际由 CLI 按 cwd/repo 重新解析；当仓库有多个活跃线程时，可能切换错误线程。这里不是“展示层小问题”，而是把 UI 选择的意图丢掉后进入写路径（租约 + pending + 可能的启动）。

## 3. 规格 vs 实现：Continuity Engine

`docs/API.md:114-147` 与 `voyager/continuity.py:1447-1492` 给出的规格是：

- `handoff_thread()` 是唯一引擎；
- 不打印、不启动、不为预期路径抛异常；
- 不写 provider 文件、不创建 WorkThread；
- 租约只在 `target` 已给且存在 WorkThread 时产生。

源码检查与现有收敛测试（`tests/test_handoff_convergence.py`、`test_ambiguous_workthread_handlers.py`）支持这些不变量；`auto.resolve_work_thread()` 对多活跃线程返回 `ambiguous`，`auto.discover_continuity()` 将 `continuity_available` 置为 `False`，CLI act 路径退出 2。[A/B]

需要注意的边界：`api.current_work()` / `api.continue_context()` 在 ambiguous 情况下仍返回默认线程与 bundle，但附加 `thread_ambiguous`、`thread_candidates` 与 `thread_default_reason`。这是**只读预览**，不是 act；规格允许“默认可展示但必须标注”，与 CLI 的“停止并询问”不矛盾。[A]

`Native Resume` 在所有文档中仍为 `NOT_VERIFIED`；本报告没有增加它的事实状态。[B]

## 4. 故障注入：stdio API 桥

测试模块 `tests/test_api.py:1-8` 声明契约：“unknown ops and bad params produce `{"error": ...}`, never a crash”。`voyager/api.py:570-583` 中 `fn = _OPS.get(op)` 位于 `try` 之前；如果 JSON 请求里的 `op` 是数组，`dict.get()` 抛 `TypeError`，整个 `serve()` 循环退出。

驱动：`E:/sessionflow-scratch/r-full-review/probe-bridge-unhashable-op.py`，向真实的 `python -m voyager.api serve` 连续写三条 JSON 行：合法 `integration_info`、`op: []`、合法 `integration_info`。

实际输出要点：

```text
第 1 行：正常 result
第 2 行：空
第 3 行：空
stderr: TypeError: unhashable type: 'list'
  File ".../voyager/api.py", line 574, in handle_request
```

**判定：CONFIRMED（高严重度）。** VS Code 扩展使用单一长生命周期 bridge（`extension.js:43-95`）；一条畸形消息即可让这个子进程死掉，之后所有 UI 请求都无响应。Plugin 的 `callOp()` 使用一次性子进程，受影响范围较小，但同一桥接契约仍被破坏。

这不是“未知 op”或“坏参数返回 error”的问题，而是**请求信封形状未先校验**。

## 5. 最小可验证实验清单

| ID | 实验 | 期望 | 实际 | 判定 |
|---|---|---|---|---|
| J1 | `A → B → A`，第一笔 A 晚回 | 当前 A 的新响应保留 | 旧 A 覆盖新 A | CONFIRMED |
| J2 | 一键切换传入 `thr-EXPECTED` | 命令包含 `--thread thr-EXPECTED` | `voyager switch codex` | CONFIRMED |
| J3 | 向 stdio 桥发送 `op: []` 后再发合法请求 | 两条 error/result 都返回 | 桥进程崩溃，后续无响应 | CONFIRMED |
| J4 | 多活跃 WorkThread 的只读 preview | 必须标注 ambiguous 与 default | 源码与测试均有标注 | VERIFIED (read-only) |
| J5 | 真实 Extension Host 手测 | 在 VS Code 内复现 J1/J2 | 未执行 | NOT_VERIFIED |

## 6. 方法边界

- 以上“状态机”是用 JYY Skill 的 state-machine-first / minimum-runnable-example / specification-vs-implementation 方法写出的推理模型，不是 JYY 本人给出的产品规范。
- J1/J2/J3 的 reproducer 都在仓库外 scratch，未读取真实会话、未写真实索引、未启动会弹窗的进程。
- 本轮不做修复决策；修复优先级由 Phase 4 交叉裁决给出。
