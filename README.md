# 国内期货量化项目

> 工作台默认策略为「自适应趋势」安全基准。15分钟 MA169 穿越/MA13 分批策略仍保留，但 60% 单品种保证金预算和 3% 单笔风险只用于压力测试；系统现在强制单笔最多 5 手。更稳健的双均线候选配置见 `configs/backtest_ma_pullback_safe.json`。双击 `启动量化工作台.bat` 或运行 `python -m futures_quant dashboard`。

> 中文显示：工作台的持仓、历史成交、历史盈亏和参数优化表格统一使用中文表头；工作台生成的用户文件使用中文文件名和中文表头。命令行流程在保留英文内部文件供程序读取的同时，会自动生成文件名带 `_中文` 的中文表头副本。

这是一个可继续开发的国内期货量化工程骨架，当前已经跑通：

`CSV 行情 -> 标准 Bar 数据源 -> 策略信号 -> 风控检查 -> 回测撮合 -> 绩效结果`

后续接入交易软件 API 时，只需要把真实行情和下单能力适配到统一接口，不需要重写策略和回测核心。

## 当前能力

- CSV K 线行情加载，字段为 `datetime,symbol,open,high,low,close,volume,open_interest`
- 标准行情数据源接口：`MarketDataSource`
- 双均线 CTA 策略示例
- 15分钟 MA169/MA13策略：MA169穿越入场、0.1ATR缓冲失效退出、MA13每次按剩余仓位比例递减
- 一键扫描当前数据目录中有行情文件的品种，以相同初始资金独立回测并输出收益、回撤、Sharpe、成交和拒单排名
- 强化自适应研究策略：1～5手风险定仓、ATR硬止损、1R保本、2R分批、ATR跟踪及15/60分钟两阶段样本外优化
- 自适应趋势策略：唐奇安突破、趋势/动量确认、波动率目标仓位和名义敞口上限
- 训练/验证/最终测试隔离的全局参数优化，以及手续费/滑点压力测试
- 可配置 CSV/JSON HTTP 历史行情 Provider
- 手续费、滑点、合约乘数、保证金率、保证金占用、单品种敞口等基础回测参数
- 回测结果输出：摘要、权益曲线、成交记录
- 统一交易软件 API 边界：`TradingGateway`
- 本地模拟网关：`MockGateway`
- CTP 无密码网关框架：状态机、回调归一化、重连、下单前风控与实盘二次解锁；未配置 SDK/凭据时默认禁用
- 配置校验：启动回测前检查保证金、手续费、合约乘数、策略窗口等关键参数
- 合约参数表：`configs/contracts.csv`
- 行情落盘工具：把交易网关输出的标准 Bar 写成 CSV，供回测复用
- 多品种 universe：`configs/universe.json`
- 多市场演示回测配置：`configs/backtest_multi.json`

重要边界：`data/domestic_15m` 等目录中的自带行情仍是 synthetic 数据，不能解释为真实收益。项目另有本机博易缓存导入的真实短样本 `data/pobo_real_15m`，但其覆盖约 230--259 天、主力换月规则尚未确认，也不能冒充近三年可信回测。独立子账户等权、币种折算、换月和交易所规则等限制详见 `docs/MODEL_RISK.md`，接实盘前必须逐项处理。

## 博易真实短样本与批量导入

已支持读取博易大师本地 `Data/<市场>/5Min/*.his` 缓存，按交易时段安全聚合成 15 分钟标准 CSV，并输出来源、SHA-256、覆盖范围、缓存截断、时间缺口和主力序列语义警告。

```powershell
$env:PYTHONPATH = "src"
$python = if ($env:FQ_PYTHON) { $env:FQ_PYTHON } else { "python" }
& $python -m futures_quant import-pobo-batch `
  --data-root "F:\量化\长江期货-博易大师7交易版\pobo7\Data" `
  --output-dir data\pobo_real_15m `
  --manifest reports\pobo_import_manifest.csv
```

操作说明见 `docs/POBO_BATCH_IMPORT.md`，已完成的真实短样本组合评估见 `docs/POBO_REAL_SHORT_SAMPLE_EVALUATION.md`。它们都明确区分工程验证与可信回测。
申请三年授权数据时可直接使用 `docs/REAL_DATA_ACQUISITION_CHECKLIST.md`。

### 在工作台测试本机真实约 8 个月样本

1. 双击 `启动量化工作台.bat`，在“回测与策略”页点击“使用本机真实短样本（约8个月）”。
2. 工作台会设为 `data/pobo_real_15m`、`_15m.csv` 和 15 分钟，并显示“当前目录可回测 22/69 个品种”。默认勾选前 5 个可用品种。
3. 单品种或组合回测前，点击“全选有数据”只选择当前目录中实际存在的行情；品种列表会标记“有数据/缺数据”。
4. “一键扫描当前可用 22 个品种收益”只扫描本机已有文件，自动跳过其余 47 个未导入品种，并把跳过清单写入扫描配置。

这是终端缓存导入的真实短样本，适合验证信号、成本、风控和工作台流程；它不是三年样本，主力连续合约换月也尚未审计完成，不能据此作为实盘盈利结论。

## CTP API（不需要先输入密码）

下面的诊断仅读取公开模板，不读取环境变量中的账号/密码/AuthCode，不导入 SDK、不联网、不登录、不下单：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant ctp-diagnose --config configs\api.changjiang.example.json
```

后续 SDK 适配和实盘安全门说明见 `docs/CTP_GATEWAY_DEVELOPMENT.md`。在获得期货公司仿真 SDK、程序化交易权限和账户本人单独授权之前，项目保持 `enabled=false` 且工作台不提供登录或下单入口。

## 快速运行

在项目根目录执行：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant backtest --config configs/backtest.json
```

输出文件：

- `reports/backtest_summary.csv`
- `reports/equity_curve.csv`
- `reports/trades.csv`

## 多品种历史数据回测

生成覆盖国内主要期货、比特币、国际期货的 demo 历史数据：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant generate-demo-history --universe configs/universe.json --output-dir data/history
```

批量回测并输出汇总：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant batch-backtest --config configs/backtest_multi.json --universe configs/universe.json --history-dir data/history --report-path reports/multi_asset_summary.csv
```

分析批量回测结果，生成等权组合净值、分组汇总和品种排名：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant analyze-batch --summary-path reports/multi_asset_summary.csv --reports-dir reports --output-dir reports/analysis
```

生成可直接阅读的 Markdown 报告：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant make-report --analysis-dir reports/analysis --output reports/backtest_report.md --title 多品种期货量化回测报告
```

结果文件：

- `reports/multi_asset_summary.csv`
- 每个品种的单独摘要：`reports/<symbol>_summary.csv`
- 每个品种的权益曲线和成交记录：`reports/<symbol>_summary_equity_curve.csv`、`reports/<symbol>_summary_trades.csv`
- 组合分析：`reports/analysis/portfolio_summary.csv`
- 组合净值：`reports/analysis/portfolio_equity.csv`
- 分组汇总：`reports/analysis/group_summary.csv`
- 品种排名：`reports/analysis/instrument_ranking.csv`
- Markdown 报告：`reports/backtest_report.md`

注意：`generate-demo-history` 生成的是确定性 demo 历史数据，用于验证工程链路，不代表真实市场行情。接入真实历史 API 后，只需要把真实数据保存成同样的 CSV 格式，再运行 `batch-backtest`。

使用历史数据 Provider 拉取并保存标准 CSV：

一键完整流程：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant run-pipeline --provider synthetic --universe configs/universe.json --history-dir data/history_api --suffix _1d.csv --config configs/backtest_multi.json --quality-report reports/data_quality_report.csv --batch-summary reports/multi_asset_api_summary.csv --analysis-dir reports/analysis --report reports/backtest_report.md --title 多品种期货量化回测报告
```

分步调试流程：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant fetch-history --provider synthetic --universe configs/universe.json --output-dir data/history_api --suffix _1d.csv
python -m futures_quant validate-history --history-dir data/history_api --suffix _1d.csv --output reports/data_quality_report.csv
python -m futures_quant batch-backtest --config configs/backtest_multi.json --universe configs/universe.json --history-dir data/history_api --suffix _1d.csv --report-path reports/multi_asset_api_summary.csv
python -m futures_quant analyze-batch --summary-path reports/multi_asset_api_summary.csv --reports-dir reports --output-dir reports/analysis
python -m futures_quant make-report --analysis-dir reports/analysis --output reports/backtest_report.md --title 多品种期货量化回测报告
```

当前已预留 Provider：

- `synthetic`：稳定的本地工程验证数据；其回测收益只验证程序链路，不能代表真实策略表现、预期收益或可交易性。
- `akshare`：用于国内期货历史日线，需先安装 AKShare。
- `binance`：用于 BTCUSDT 等币安现货日线，需公网接口可访问。
- `http`：通过可配置的 CSV/JSON HTTP 接口读取交易软件或数据平台历史行情。

HTTP 接口配置样例见 `configs/http_history.example.json`。`url_template` 可使用 `{symbol}`、`{start}`、`{end}` 占位符；`field_mapping` 的键是项目标准字段，值是接口原字段；JSON 响应可用 `data_path` 指定 K 线数组所在路径。不要把真实 Token 提交到仓库。

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant fetch-history --provider http --provider-config configs/http_history.json --universe configs/universe.json --output-dir data/history_http --suffix _1d.csv
python -m futures_quant run-pipeline --provider http --provider-config configs/http_history.json --universe configs/universe.json --history-dir data/history_http --suffix _1d.csv --config configs/backtest_multi.json
```

除了 `https://`，HTTP Provider 也支持 `file:///` URL，可用交易软件导出的本地 CSV/JSON 做无网络联调。读取后会映射字段、统一时间、按请求区间过滤并按时间升序写成标准 CSV。

如果外部 API 不稳定，推荐先把交易软件或数据平台导出的历史数据转换成标准 CSV，再运行批量回测。

`run-pipeline` 会在任一品种拉取失败时停止并把原因写入 `_fetch_manifest.csv`；也会在 `validate-history` 发现 warning/error 时停止，避免旧数据或坏数据继续进入回测。确实需要忽略质量告警时，可加 `--allow-warnings`，但拉取失败仍会停止。

## 验证

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests
```

## 全局参数优化与样本外评估

原策略 `adaptive_trend` 保留为基准。研究版 `adaptive_trend_v2` 在唐奇安突破、长期趋势、时间序列动量和波动率仓位基础上，增加止损风险、名义敞口、保证金和5手上限的联合定仓，以及ATR保护性退出。所有参考指标排除当前及未来K线，普通信号在下一根开盘撮合。

先运行单品种增强策略回测：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant backtest --config configs/backtest_adaptive.json
```

策略优化的目标是提高样本外风险调整后收益，而不是保证绝对收益率。参数选择应重点比较 Sharpe、Calmar、最大回撤和成本压力结果。

`optimize-strategy` 按时间顺序切分训练、验证和最终测试。所有候选参数都在同一个 universe 上使用同一组参数；候选排名只使用训练/验证数据，胜出参数被冻结后才读取最终测试段。默认选择得分为训练得分与验证得分的较小值，避免仅凭单一阶段的高分宣称稳健。最终测试结果不会反馈给参数选择。

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant optimize-strategy --config configs/backtest_multi.json --optimization-config configs/optimization_adaptive.json --universe configs/universe.json --history-dir data/history_api --suffix _1d.csv --output-dir reports/optimization
```

优化配置支持参数笛卡尔网格、固定日期或比例切分、收益/Sharpe/Calmar/稳健复合目标、逐品种最低交易覆盖，以及手续费倍数和滑点 ticks 的结构化压力测试。默认示例搜索 `adaptive_trend`，并将 `backtest_multi.json` 中原始 `dual_ma` 参数作为最终测试基准。年化因子由数据的实际时间跨度与观测频率推导，因此日线、7×24 小时数据和后续分钟线不会共用硬编码的 252。

主要输出：

- `candidate_ranking.csv`：仅含训练和验证指标的候选排名
- `selected_parameters.json`：选中参数、选择数据截止日及测试隔离声明
- `selected_backtest_config.json`：基于原回测配置、仅替换策略后的可执行配置，可直接传给 `batch-backtest`
- `oos_instrument_comparison.csv`：最终测试中逐品种的优化/基准对比
- `oos_portfolio_comparison.csv`、`oos_portfolio_equity.csv`：最终测试等权组合比较
- `cost_sensitivity.csv`：选中参数在不同手续费和滑点下的最终测试压力结果
- `split_manifest.csv`：每个品种各时间段的边界、样本数和是否参与选择
- `optimization_report.md`：便于阅读的结果摘要

提高候选数量并不等于提高可信收益。应优先使用真实、无幸存者偏差的合约数据，并保留最终测试隔离；synthetic 数据上的改善只代表流程正确，不能作为收益预期。

## 单一共享账户组合回测

`batch-backtest` 适合研究逐品种信号和等权净值；需要检验真实资金占用时，使用 `portfolio-backtest`。该命令把所有选中品种放入一个现金、权益和保证金池，同一时刻先统一估值，再按“减仓优先、开仓按代码稳定排序”撮合。默认从 universe 中选择人民币国内交易所品种：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant portfolio-backtest --config reports/optimization/selected_backtest_config.json --universe configs/universe.json --history-dir data/history_api --suffix _1d.csv --output-dir reports/shared_portfolio --evaluation-start 2025-12-01
```

主要输出为 `summary.csv`、`equity_curve.csv`、`trades.csv`、`rejections.csv`；提供 `--evaluation-start` 时还会输出保留此前策略状态和持仓的 `oos_summary.csv`。策略风险预算按 `1/sqrt(品种数)` 缩放，避免每个品种都按完整账户权益独立放大仓位。

当前共享账户不会静默混加人民币、美元和 USDT。境外单币种组合必须显式指定，例如 `--symbols CL,GC,ES --base-currency USD --symbol-currency USD`；混合币种组合会直接报错，直至接入按时间戳对齐的 FX 序列。实现口径详见 `docs/PORTFOLIO_BACKTEST.md`。

验证项目内 API 边界：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant gateway-smoke
```

验证网关行情落盘：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant record-sample-bars --output data/recorded/RB2405_gateway_sample.csv
```

模拟“交易软件 API 行情进入系统后再回测”的完整链路：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant replay-gateway-csv --input data/sample/RB2405_1d.csv --output data/recorded/RB2405_gateway_replay.csv --symbol RB2405
python -m futures_quant backtest --config configs/backtest.json --data-path data/recorded/RB2405_gateway_replay.csv --symbol RB2405 --report-path reports/gateway_replay_summary.csv
```

## 接入真实交易软件 API 的位置

真实交易软件或期货柜台 API 不应该写进策略里，只需要实现：

- `src/futures_quant/api/base.py` 里的 `TradingGateway`
- `src/futures_quant/data/source.py` 里的行情数据源适配
- 如果是 CTP 类接口，优先补全 `src/futures_quant/api/ctp_gateway.py`
- 详细步骤见 `docs/API_INTEGRATION.md`

需要从你的期货公司或交易软件侧取得：

- 行情前置地址
- 交易前置地址
- broker_id
- user_id
- password
- app_id / auth_code
- 合约、保证金、手续费、限仓等查询接口或日常参数表
- SDK 文件或 Python 包安装方式
- 仿真环境账号，先不要直接使用实盘账号

## 推荐实操流程

1. 补全真实交易软件网关，只连接和订阅行情，不下单。
2. 将真实行情转换成项目内部 `Bar`。
3. 用行情落盘工具保存成 CSV，格式参考 `data/recorded/RB2405_gateway_replay.csv`。
4. 在 `configs/backtest.json` 中把 `data.path` 改成录下来的 CSV。
5. 用 `backtest` 命令跑出回测结果。
6. 回测和仿真都确认后，再接下单、撤单、成交回报。

多品种历史 API 接入后的推荐流程：

1. 通过交易软件、AKShare、TuShare、Binance 或其他数据源下载历史 K 线。
2. 转换为项目标准 CSV 字段：`datetime,symbol,open,high,low,close,volume,open_interest`。
3. 运行 `validate-history` 检查缺字段、重复日期、OHLC 异常、空值和非正价格。
4. 将合约参数写入 `configs/contracts.csv`。
5. 将品种清单写入 `configs/universe.json`。
6. 运行 `batch-backtest` 查看多品种结果。

## 下一步开发建议

1. 加入真实合约换月规则，避免只用主连数据回测。
2. 增加分钟线回测与夜盘交易日归属处理。
3. 增加涨跌停不可成交、临近交割月禁入、节假日前降仓等交易所规则。
4. 接入真实交易软件 API 后，先使用仿真环境，不直接实盘。
5. 增加策略版本号、参数快照、订单追踪和每日结算核对。
