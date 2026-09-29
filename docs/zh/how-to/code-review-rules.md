# 代码检视规则手册

本文总结 openJiuwen agent-studio 项目近期提交代码中被 CodeCheck 与人工检视发现的问题，沉淀为可复用的通用规则，供提交前自查与评审参考。配套本地自检脚本：`tools/codecheck_self.py`（详见文末）。

## 一、CodeCheck G 规则（华为 CodeCheck 引擎，增量扫描）

规则编号体系为华为 CodeCheck 引擎内置（G=通用规则，CTL/FNM/ERR/LOG/VAR/COM 为类别码），规则全集在服务端任务配置中，仓库内可参考存量规避范例。

| 规则 | 内容 | 规避范式 |
|---|---|---|
| **G.CTL.03** | if/while 条件中布尔操作数 ≤3（4/3 即报） | **先写谓词函数再写 if**：每个 if ≤3 操作数，谓词内部可拆多段 return。仓库范式：`end.py:_has_response_payload`、`loop-modal.component.ts:isNumericStringLiteral` |
| **G.FNM.01** | Python 函数默认参数禁用可变对象 | 默认 `None`，函数内创建 |
| **G.FNM.02** | 闭包禁捕获循环变量 | 用 state holder 字典显式传递 |
| **G.FNM.03** | 函数参数过多（约 >5 个） | 封装参数对象/参数组 |
| **G.ERR.07** | `except Exception` 需显式标注 | `# noqa: G.ERR.07`（引擎识别 noqa；仓库惯例优先规避而非抑制） |
| **G.VAR.03** | 重复 import/声明 | 集中 import |

⚠️ 引擎是**增量扫描**：改动的函数里连未改动的既有行也会被连带标记。**提交前对改动函数全文自查**，不要只看新增行。

## 二、真值判断陷阱（高频缺陷）

**规则：对值域包含数字/布尔的字段，禁止真值判断。**

`0`、`false`、`''` 都是 falsy。integer 初始值 0、boolean false 的中间变量曾被当成"未填写"跳过，导致变量从引用树消失并连带清空下游已配置的引用。

正确姿势：显式判空并提取成谓词：

```typescript
private isLiteralFilled(param: IWorkflowField): boolean {
  return param.value.content !== '' && param.value.content != null;
}
```

Python 同理：`value or 0`、`if content:` 这类写法都要审视值域是否包含 0/false。

两个补充陷阱：

- **显式判空也别忘容器**：空数组 `[]` / 空对象 `{}` 同样能通过 `!== '' && != null` 的检查，是否算"已填写"取决于业务语义（ref 未选择时 content 为 `[]`，不应视为已填）
- **真实后果链要写进检视意见**：0/false 被真值判断跳过 → 变量从引用树消失 → `reSelectRefsWithNewOps` 连带清空下游已配置引用——说清传导路径，评审才有优先级

## 三、类型切换与数据一致性（UI 表单通用）

1. **切换类型/来源必须处置旧内容**：重置为新类型默认值（integer/number→0、boolean→false、string→''），否则旧文本随新类型持久化，schema 类型声明与值类型不一致
2. **默认值逻辑收敛到共享辅助函数，并在所有变更入口统一调用**：来源切换、类型切换、新增、读取——四个入口漏一个就出不一致（如 ref→literal 切换后合法标量类型携带空串）。参考范式：`loop-modal.component.ts:defaultLiteralContent`
3. **序列化/读取双向清洗**：字符串承载的数值，保存时 `Number()` 转换、读取时同样转换——对齐既有先例 `getNumLoopVar`。注意：**表单校验不阻断保存流程**，序列化处要把绕过校验的非法值归一，不能只依赖校验器
4. **归一策略分三档：缺失→默认值、形态不符→就近转换、无法转换→默认值**。例：integer 字段的 1.5 属于"形态不符"（数值在但非整数），应 `Math.trunc(1.5)→1` 保留量级，而不是重置为默认 0 丢失用户输入；`'abc'` 无法转换才回退默认。就近转换的方向要与运行时消费方对齐（运行时 `int()` 即截断语义），仓库存量先例：`loop_set_variable.py` 的 `int()`、前端 `Math.floor`、ng-zorro `[nzPrecision]="0"`。**归一目标用类型默认值而非 null**：null 仍可能被下游引用并触发运行时转换错误；注意**表单校验不阻断保存流程**，序列化处要把绕过校验的非法值归一，不能只依赖校验器
5. **boolean 用控件约束**（true/false 下拉），不用自由文本 + 事后校验
6. **值输入按类型挂校验器**：integer→`integerStr`、number→`numberStr`、其余 `nonEmptyValidator`
7. **混合形态列表用固定列布局**：表头与数据行的列结构不要按首行动态驱动（literal/ref 混排会错位）；ref 行对应列显示同步的只读值，而不是隐藏整列导致表头空缺
8. **读取端治愈必须显式触发持久化**：打开弹窗/页面时修正脏数据后要设置 save 标记走既有保存链，否则 `tagCompareNoChange` 类的跳过逻辑会让治愈失效——变成每次打开都重复清洗，存量脏数据永远无法自愈

## 四、防御式代码的自洽性

1. **提取谓词必须逐项核对原条件语义**：把复合 if 拆成谓词函数是检视高频回归源——类型判别项最易丢失（丢失 `value.type === 'literal'` 后，ref 行的空数组 `[]` 通过了显式判空，误入 literal 分支推出无 type 的引用节点污染下游）。重构后按"类型 × 填写状态"逐象限自测
2. **读写守卫必须对称**：`result[i]` 读取有越界守卫、`result[i] = x` 写入也要有——同一函数内"结构不匹配安全"的承诺要在读/写/长度三个维度全部兑现
3. **复合引用串解析防"首尾成壳"**：`${A}/${B}` 剥壳后 origin 仍含花括号，需显式排除（`{`/`}` not in origin），使其落入插值路径而非被误判为整串引用
4. **外部依赖打补丁四件套**（`jiuwen/extension/patches/` 惯例）：幂等 flag、`TODO` 回收标注、边界写入 docstring、兜底异常隔离
5. **返回可变对象要拷贝**：兜底/缓存返回的 dict/list 落库前 deepcopy，防下游原地修改污染权威数据

## 五、测试纪律

1. **禁止模块级 assert 依赖"首次调用返回值"**：幂等/单例机制下，收集顺序不同返回值不同 → 一次收集错误中断全量测试。改用**状态断言**（如 `目标 is 替换函数`）
2. **本地必须跑真实测试文件**：平行验证脚本逻辑等价但数据不同——变量名笔误（写 plain 读 mem_counter）只有 CI 全量能抓
3. **新功能引入新值域必须补边界用例**：0/false 相关缺陷在旧用例全过、新值域用例全挂时才暴露
4. **i18n 新列头先查重名**：en-US 里 `type` 与 `param_type` 同为 "Type"，复用会造成两列同名；必要时新增专用 key（如 `data_type`）

## 六、提交合规

1. **版权头**：外部贡献者不声明 Huawei 版权（仓库无 CLA；先例 PR #2098、Salty_Dong）
2. **注释语言**跟随所在目录既有惯例（如 patches/ 目录为中文）
3. **版权年份**用当前年份
4. **类型标注**用管道风格 `X | None`（requires-python ≥3.11）
5. **noqa 标记**需有配置支撑：仓库无 ruff 配置，不写 `# noqa: BLE001` 这类标记

## 七、本地自检工具

`tools/codecheck_self.py`（标准库实现，无额外依赖）：

```bash
# 增量：检查相对基线分支的变更文件
python tools/codecheck_self.py --base origin/studio-2.0-dev

# 显式指定文件
python tools/codecheck_self.py path1.py path2.ts
```

覆盖规则：G.CTL.03（Python AST 精确计数 / TS 启发式）、G.FNM.01（可变默认参数）、flake8 通用项建议另行执行。退出码非 0 表示存在违规，可挂 pre-commit hook。

规则全集在服务端 CodeCheck 任务配置中，本地无法获取；本工具覆盖的是本仓库已实证命中的规则子集，新规则命中后应在本文档同步补充。

