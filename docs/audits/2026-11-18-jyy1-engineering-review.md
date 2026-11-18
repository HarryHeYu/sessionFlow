# Jyy1 independent engineering review — 2026-11-18

> 这是**独立**审评：在交叉裁决前，只使用 Jyy1 方法库工程路径，不引用 JYY 审评结论。
> 方法来源：`E:/code/Jyy1/skill/SKILL.md`。本报告不冒充 JYY 本人观点。
> 证据等级：A=直接运行或源码直读；B=多源归纳；C=Skill 应用。
>
> 护栏：**演示跑通 ≠ 需求实现**；具体值必须查证；合成证据不能升级为 live；
> 真实 Extension Host 未验证就写 `NOT_VERIFIED`。

## 1. Coverage gate

| 被审项 | Direct | Partial | Absent | 结论 |
|---|---:|---:|---:|---|
| 七工具注册与 Core op 映射 | ✓ |  |  | 源码 + 契约测试 [A] |
| `sessionflow_evidence_verify` 五态透传 | ✓ |  |  | 真实 v1.5.0 发布验证记录 [B] |
| DSH 结构字段 provenance | ✓ |  |  | 12 文件结构采样；197/197 user 行可映射 [A/B] |
| DSH 真实索引 store-level coverage |  |  | ✓ | 无授权真实索引副本，Plugin #2 仍 OPEN |
| VS Code 真正 Extension Host E2E |  |  | ✓ | Core #59 仍 OPEN |
| 模型驱动工具选择 |  | ✓ |  | OBSERVED, NOT REPEATABLE；不得报成功率 |

解释：前两项“通过”只说明对应测试/契约存在，不自动升级为生产环境验证；后两项不能因为合成脚本通过而改写状态。

## 2. 工程路由：Build / Debug 事实

- Plugin `package.json:3` 版本为 `1.3.0`；`src/bridge.ts:21` 的 `REQUIRED_SCHEMA_VERSION = 1`。
- `src/index.ts:65-79` 注册七个工具；第六/第七工具之间没有第二个 verification 实现。
- `src/tools/evidence-verify.ts:139-143` 走 `callOp('evidence_verify', ...)`，保留 Core 的五态 verdict，不会把非零退出码扁平化成 `CLI_FAILED`。[A]
- `docs/release-v1.3.0.md:130-131` 明确记载 `REQUIRED_CORE_REF=f2a6445` 与 README `v1.5.0` pin 不同源，并把它列为需要单独决策的 bridge 行为。**这是已知设计债务，不是本轮新缺陷。** [B]
- Core main 在 `971df2d` 增加了 `merge --repo`，Plugin `src/tools/merge.ts:30-39` 没有 `repo` 参数；Plugin v1.3.0 的发布契约是 Core v1.5.0，因此这属于**后发布能力未暴露（DESIGN GAP）**，不应倒推为 v1.3.0 缺陷。[A/B]

## 3. 故障注入：bridge 错误分类

### 3.1 core 答复非 JSON 被报成“未安装”[A]

`src/bridge.ts:252-287` 的 `probe()` 把 `JSON.parse(stdout.trim())` 放进同一个 `catch`，而该 `catch` 固定返回 `found:false`。于是一个**存在并退出 0** 的 executable 只要输出非 JSON，就会被 `resolveVoyagerUncached()` 当成“could not be started or timed out”。

驱动：`E:/sessionflow-scratch/r-full-review/probe-plugin-probe-json.mjs`，用 Python 程序作为 `voyagerBin`，对 discovery probe 输出 `not json` 并退出 0。

实际输出：

```json
{
  "code": "VOYAGER_NOT_FOUND",
  "message": "the configured sessionFlow/Voyager executable does not work: ... could not be started or timed out: Unexpected token 'o', \"not json\" is not valid JSON"
}
```

**判定：CONFIRMED（中等严重度）。** 诊断方向错误：用户看到的是“没找到/没启动”，而实际是 core 回答格式不对。`found` 的语义在 `ProbeFailure` 注释中被明确赋予重要性，这里没有兑现。

### 3.2 `callOp()` 接受非零退出码后的 JSON[A]

`src/bridge.ts:428-449` 调用 `run()` 后只读 `stdout`，没有检查 `RunResult.code`。`runJson()` 在 `:470-475` 则检查了退出码。

驱动：`E:/sessionflow-scratch/r-full-review/probe-plugin-callop-exit.mjs`。stub 对 discovery probe 返回合法 schema，对一个普通 op 输出 `{"result":{"ok":true}}` 后 `sys.exit(3)`。

实际输出：

```json
{ "acceptedResult": { "ok": true } }
```

**判定：CONFIRMED（较低严重度，暂不改）。** 当子进程在发出结果后异常退出时，插件会把结果当成成功。当前真实 Core 的 `voyager api` 正常路径不会这样；修复应放在“错误分类一致性”中评估，而不是本轮为了对称而改 bridge 行为。

## 4. DSH provenance：真实效果而非覆盖率幻觉

Core `voyager/provenance.py:152-231` 的 DSH classifier 只读取结构字段：

- `user/message` 且 `source.kind == 'user'`（或 fixture 别名 `client`）→ `human`；
- bootstrap kinds → `provider_bootstrap`；
- system/tool kinds → `provider_system`；
- 缺失或新 kind → `UNKNOWN`；
- `role=user` **不能**单独推出 human。

`docs/audits/2026-11-dsh-provenance-structure.md` 对 12 个文件的 197 条 user row 做了枚举，所有样本都映射到允许的结构类型，`UNKNOWN=0`。但这是**结构样本覆盖**，不是“用户真实索引中每条事件都已被正确分类”。

**判定：PARTIAL。** 结构性规则有直接证据；store-level real coverage 仍 `NOT VERIFIED`。Plugin #2 保持 OPEN 是正确的，合成 fixture 100% coverage 不能替代真实验收。

## 5. VS Code 真实体验

Jyy1 工程护栏明确：headless DOM stub 不是 Extension Host。

- 仓库内 `tests/test_vscode_extension.py` 是静态与 parser 级检查；
- `tests/test_selector_race.py` 在 node:vm 里运行 shipped inline script，但 DOM 是 stub；
- 早期真实 API + shipped webview 脚本 11/11 验证仍不是 VS Code 内的 Extension Host；
- `Core #59` 明确记录真实 Extension Host E2E `NOT_VERIFIED`。

**判定：NOT VERIFIED。** 本轮没有启动 VS Code，也没有用截图/headless 结果代替实机。

## 6. 最小修复候选（只列，不在本审评中执行）

1. 让 discovery 的 JSON 解析错误返回 `found:true` 的 `BAD_CONFIG`/`INCOMPATIBLE` 类别，而不是 `VOYAGER_NOT_FOUND`。
2. 在 `callOp()` 中检查 `code !== 0`，与 `runJson()` 行为一致；需先确认这不会把 Core 的有意非零状态误判——当前 Core `voyager api` 的 op 错误在 JSON 里返回，不走进程退出码。
3. `merge --repo` 是否暴露给 Plugin 属于产品/契约决策，不是 bug fix。

## 7. 方法边界

本报告用过 Jyy1 的 Build/Debug 路由、coverage gate 与“演示跑通 ≠ 需求实现”护栏；它们是基于公开材料整理的方法库，不代表 JYY 本人对本项目发表意见。所有运行实验均使用合成 stub，不读取真实用户会话正文，不写真实索引，也未访问 SSH 凭据或认证材料。
