# 博易大师真实行情批量导入与审计

本流程只读取博易大师已经写入本机的行情缓存，不读取账号、密码，不操作交易窗口，也不会向券商发单。

## 一键扫描和导出

在项目根目录执行：

```powershell
$env:PYTHONPATH = "src"
$python = if ($env:FQ_PYTHON) { $env:FQ_PYTHON } else { "python" }
$poboData = "F:\量化\长江期货-博易大师7交易版\pobo7\Data"

& $python -m futures_quant import-pobo-batch `
  --data-root $poboData `
  --contracts "configs\contracts.csv" `
  --universe "configs\universe_domestic_3y.json" `
  --output-dir "data\pobo_real_15m" `
  --manifest "reports\pobo_import_manifest.csv"
```

命令递归扫描 `Data/<市场>/5Min/*.his`，读取相邻市场目录中的
`NameTable.xml`，转换为标准15分钟CSV，并为每一个发现的缓存文件写一行审计清单。
同时会在输出目录写入 `_source_manifest.json`，供工作台明确标识该目录为“博易本地
缓存导入的真实行情”；这个标识不替代覆盖范围审计。

只想导出部分品种时可增加：

```powershell
--symbols "RB0,CU0,AU0"
```

指定品种必须同时存在于 `configs/contracts.csv` 和所选 universe 配置中，防止导出无法按合约乘数、保证金率和手续费回测的数据。

## 品种映射和多序列选择

- 实际合约按BRCode精确匹配，例如 `RB2610 -> RB2610`；只有项目配置中存在该实际合约时才导出。
- 博易主力、连续和加权代码分别按 `_ZL`、`_LX`、`_ZS` 映射到项目的 `ROOT0`，例如 `rb_ZL -> RB0`。
- 同一品种存在多种序列时，默认选择顺序为主力、连续、加权，即 `ZL,LX,ZS`。
- 可用 `--series-preference "LX,ZL,ZS"` 调整选择顺序；未选中的缓存仍保留在manifest中，状态为 `skipped`。
- `_L3`、`_L4` 等近月序列不会自动冒充项目主连。

博易的主力换月规则、连续合约价格调整方式和加权指数构造方法没有随缓存公开，因此manifest会分别记录
`main_contract_roll_rule_unknown`、`continuous_roll_adjustment_unknown` 或
`weighted_index_construction_unknown`。在规则得到券商或数据商书面确认前，这些序列可用于工程验证，但不能据此声称完成了严格可复现的主连回测。

## Manifest字段

`reports/pobo_import_manifest.csv` 的核心字段包括：

- 来源：`source_file`、`name_table`、`PBCode`、`BRCode`、文件大小、UTC修改时间和SHA-256。
- 映射：博易名称、序列类型、项目 `symbol` 和实际输出文件。
- 数量：本地5分钟根数、聚合后的15分钟根数、文件头中的服务器5分钟总根数。
- 时间：首尾时间、覆盖自然日数。
- 质量：重复时间数、乱序数、时间缺口数、最大缺口、文件开头被丢弃的不完整15分钟桶。
- 结论：`ok`、`warning`、`skipped`、`error` 及机器可读的告警串。

博易文件头在 `0x610` 附近保存 `HisKLineCount`。本工具把它记为“服务器总根数”，并与文件中实际记录数分开：

- 本地根数小于服务器总根数：缓存只包含尾部数据，标记 `cache_truncated_at_start`。
- 本地根数等于服务器总根数：仅说明该博易序列的服务器可见记录已下载完整，不代表覆盖了三年。
- 文件头没有这个字段：标记 `server_bar_count_unknown`，不会猜测数值。

## 质量告警的解释

- `coverage_below_required_days`：默认要求 `365.25 × 3` 个自然日；不足时不可称为近三年可信回测。
- `duplicate_timestamps` / `out_of_order_records`：数据会被拒绝导出，避免回测读取歧义行情。
- `timestamp_gaps`：统计所有相邻5分钟时间戳大于5分钟的间隔。这里包含午休、夜盘休市和节假日，manifest明确标记“尚未按交易时段分类”；不能把这个原始数字直接当成缺失K线数。
- `leading_partial_15m_bucket_discarded`：本地文件从15分钟桶中途开始，开头不完整记录已丢弃，避免后续K线永久错位。
- `no_complete_15m_bars` 或 `decode_error`：文件不产生回测输入，状态为 `error`。

## 推荐的可信回测顺序

1. 运行批量导入并保存manifest，不要手工修改来源文件。
2. 确认目标品种本地根数等于服务器总根数，且覆盖至少三年。
3. 取得并记录主力换月、复权或价差调整规则；否则优先使用可审计的实际合约拼接流程。
4. 对输出目录继续运行 `validate-history`，再检查交易所交易时段、夜盘归属、涨跌停和换月跳空。
5. 数据审计通过后，才执行训练/验证/最终测试隔离及手续费、滑点压力测试。

当前manifest是可重复验证的数据来源证据，不是收益承诺。博易服务器本身只提供多少历史，批量导入器就只能如实记录多少历史，不会用合成数据填补缺口。
