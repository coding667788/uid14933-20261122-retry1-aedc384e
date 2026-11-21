# watermark_audit — 水印证据归属离线复核

对**已登记**的水印目录与采集到的合成符号阵列做离线一致性校验，输出确定性的
UTF-8 JSON 复核报告。本工具只做校验，不包含任何水印嵌入、图像处理或人员跟踪
逻辑；全部数据均为虚构接收人与虚构报告。

- 运行环境：Windows 原生 Python 3.12，仅使用标准库，纯 CPU。
- 运行方式：`python -m watermark_audit CASE.json [OUTPUT.json]`
- 报告可稳定重现：同一案例文件多次运行产出字节一致的 JSON（无时间戳、键序固定）。

## 目录结构

```
watermark_audit/        主包（校验模型、约束求解、报告、CLI）
  __init__.py
  __main__.py           命令行入口
  errors.py             带精确字段路径的校验异常
  model.py              案例文档模型与严格校验
  solver.py             同源/异源约束、统一位置模型、分支限界搜索、冲突核
  report.py             确定性 JSON 报告组装
tests/                  unittest 测试（全部虚构数据）
  brute.py              独立暴力搜索，仅用于小案交叉核对
  test_watermark_audit.py
cases/                  虚构示例案例
  demo_case.json        可行案例（含擦除、低可信差异、双版本目录）
  demo_conflict.json    不可行案例（演示极小冲突片集合）
README.md
```

## 输入 IO 定义

顶层对象字段（未知字段会被拒绝并指出准确路径）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `case_id` | string，非空 | 案例编号 |
| `default_event_stage` | string，可选 | 片段未给 `event_stage` 时的默认阶段 |
| `catalog` | object | 登记目录，见下 |
| `fragments` | array，1–28 项 | 证据片段，每片候选拷贝 ≤ 5，全部片段已观察位置合计 ≤ 80 |
| `same_source_pairs` | array of [id,id]，可选 | 两片来自同一份拷贝（等价分组） |
| `different_source_pairs` | array of [id,id]，可选 | 两片不得来自同一份拷贝 |
| `per_fragment_cost_cap` | 非负整数，可选 | 单片低可信差异代价上限 |
| `total_budget` | 非负整数，可选 | 整案去重后总差异代价预算 |
| `batch_recipient_caps` | object，可选 | 每批次“不同责任接收人数量”上限 |

### catalog

```
catalog.pinned_version        string，必须存在于 versions
catalog.versions[]            保管补录的不同版本；已发布结论按 pinned 视图保留
  .version / .released_at / .note
  .copies[]
      .copy_id / .batch / .recipient
      .codeword               { "位置字符串": 符号 }，符号为字符串或整数（布尔拒绝）
      .missing[]              允许缺失（可被擦除）的位置，必须属于 codeword
  .custody[]
      .copy_id                必须与同版本某拷贝对应，每份拷贝都要有记录
      .stages[]
          .stage / .recipient 该拷贝在该保管阶段的持有人
```

阶段可以在新版本中合并或拆分；求解始终以 `pinned_version` 指向的目录视图为准，
新版本不会改写历史结论。

### fragments[]

```
fragment_id            string，非空、全局唯一
batch_candidates[]     批次候选，至少一个，且都存在于 pinned 目录
candidate_copies[]     拷贝候选，1–5 个，均存在且其批次属于 batch_candidates
event_stage            string，可缺省（取 default_event_stage）；归属定位到该阶段持有人
observations[]         至少一项，片内位置不得重复
    .position          非负整数
    .erased            true/false
    .symbol            非擦除项必填；擦除项必须省略或为 null
    .confidence        "high"（可信高位）或 "low"
    .cost              非负整数；仅低可信非擦除项可携带
```

## 匹配与计费规则

1. **擦除不是匹配**：`erased=true` 仅当该位置在拷贝的 `missing` 中才成立，
   不计符号、不计代价；否则该拷贝不可选。
2. **可信高位**：高可信非擦除观察与登记符号不一致即硬矛盾，该拷贝不可选。
3. **低可信差异**：符号不一致时按观察自带整数 `cost` 累计。
4. **统一原始位置模型**：多片可来自同一拷贝；同一拷贝上同一
   `(position, observed-symbol)` 错误无论被观察多少次只计一次，取所有报告中的
   **最小** cost。重复观察同一次错误不重复收费；位置相同但观察符号不同属于两个
   独立错误，不得合并。
5. **单片上限**：每片自身（去重前）的低可信代价须 ≤ `per_fragment_cost_cap`。
6. **整案预算**：去重后的总代价须 ≤ `total_budget`。
7. **批次责任人上限**：按每片 `event_stage` 取持有人，同批次涉及的不同接收人
   数量须 ≤ 该批次 cap。

## 优化目标（report）

在满足全部固定证据、同源/异源、批次 cap 与预算的解释中，按字典序最小化：

1. 涉及的**不同拷贝数**；
2. **去重后总差异代价**；
3. 按片段 ID 排序的来源拷贝 ID 元组（确定性决胜）。

## 审计判定（audit）

对目录中每个接收人给出三态判定：

- `inevitable`（必然相关）：每种可行解释都包含该接收人；
- `possible`（可能相关）：有的解释包含、有的不包含；
- `impossible`（不可能相关）：没有任何可行解释包含。

仅当**每种可行解释都只指向同一接收人**时才输出 `unique_attribution=true` 与
`unique_recipient`；否则该字段为 false，并在 `witnesses` 中给出完整兼容见证。
可行解释不足两份时（例如客观上只有一种解释），`witnesses` 如实只含一份，不虚构。

## 无可行解释时

报告 `status="infeasible"`，并在 `conflict` 中给出**极小冲突片集合**：按片段 ID
**降序逐一试删**——删除某片后剩余集合恢复可行则该片必需、保留；删除后仍冲突则
该片冗余、移除。`deletion_trace` 完整记录每次试删的判定。空集合视为可行。

## 输出 IO 定义

- `status`：`feasible` / `infeasible`。
- 可行时：
  - `report`：最优拷贝数、总代价、决胜元组、按批次责任接收人、逐片差异与计费明细；
  - `audit`：唯一归属标志、逐接收人判定、兼容见证。
- 不可行时：`report`/`audit` 为 `null`，`conflict` 给出冲突核与试删轨迹。
- `catalog.pinned_view` 内联 pinned 版本目录视图，便于核对历史结论。

## 复跑步骤

```bash
# 1. 运行可行示例，报告写到文件
python -m watermark_audit cases/demo_case.json out_report.json

# 2. 不传输出路径则打印到标准输出（UTF-8）
python -m watermark_audit cases/demo_case.json

# 3. 不可行示例
python -m watermark_audit cases/demo_conflict.json out_conflict.json

# 4. 运行全部单元测试（含独立暴力搜索交叉核对）
python -m unittest discover -s . -p "test_*.py"
```

可重现性自检：连续两次运行同一案例并 `diff` 两个输出文件，结果应完全一致。

## 解析错误

任何结构或类型问题都会以退出码 2 终止，并在 stderr 给出准确字段路径，例如：

```
validation error at $.fragments[0].observations[2].symbol: erased observations carry no symbol ...
validation error at $.catalog.pinned_version: pinned version 'v9' is not present in versions
validation error at case.json:3:1: invalid JSON syntax: ...
```
