# 交易软件 API 接入说明

本项目把交易软件 API 隔离在 `TradingGateway` 和 `MarketDataSource` 两层：

- `TradingGateway` 负责连接、订阅、下单、撤单、成交回报。
- `MarketDataSource` 负责把 CSV、数据库或交易软件行情统一转换成项目内部的 `Bar`。

这样做的目标是：接入 CTP、QMT、Ptrade、天勤、掘金或其他交易软件时，不改策略、不改回测撮合，只改适配层。

## 需要实现的接口

文件：`src/futures_quant/api/base.py`

- `connect()`：连接行情和交易前置。
- `subscribe(symbol)`：订阅合约行情。
- `send_order(order)`：发送委托，返回委托编号。
- `cancel_order(order_id)`：撤单。
- `latest_bar(symbol)`：返回最近一根标准行情。
- `trades()`：返回成交记录。

文件：`src/futures_quant/data/source.py`

- `CsvBarSource`：当前已经实现，用 CSV 文件跑回测。
- `GatewaySnapshotSource`：当前已经实现，用交易网关的最新行情生成标准 Bar。
- 后续如需分钟线或 tick 回放，可以新增 `DatabaseBarSource`、`ReplayBarSource` 或 `StreamingBarSource`。

## CTP 接入位置

文件：`src/futures_quant/api/ctp_gateway.py`

真实接入时，只在这个文件里导入期货公司提供的 CTP Python SDK 或第三方封装。不要在策略里直接 import 柜台 SDK。

## 配置样例

参考：`configs/api.example.json`

生产环境不要把真实密码写入仓库，建议通过环境变量或本机加密配置读取。

## 接入顺序

1. 运行 `python -m futures_quant gateway-smoke`，确认项目内网关边界正常。
2. 补全 `CtpGateway.connect()`，只做登录和账户查询，不下单。
3. 补全行情订阅，把真实行情落成项目的 `Bar` 模型。
4. 用行情落盘工具保存真实行情，当前样例命令是 `python -m futures_quant record-sample-bars --output data/recorded/RB2405_gateway_sample.csv`。
5. 在 `configs/backtest.json` 中把 `data.path` 指向录下来的 CSV，用 `backtest` 命令回测验证。
6. 补全 `send_order()` 和 `cancel_order()`，先连仿真环境。
7. 增加结算单、持仓、保证金、手续费和限仓查询。
8. 增加实盘前风控：最大保证金占用、涨跌停、限仓、临近交割月、节假日前降仓、异常撤单频率。

## 合约参数

文件：`configs/contracts.csv`

字段：

- `symbol`
- `exchange`
- `product`
- `contract_multiplier`
- `tick_size`
- `margin_rate`
- `commission_rate`

回测启动时会优先读取合约参数表，减少手动配置合约乘数、tick 和保证金率时写错的风险。

## 本地模拟完整链路

在没有真实 SDK 前，可以用下面的命令模拟“API 行情进入系统后再回测”：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant replay-gateway-csv --input data/sample/RB2405_1d.csv --output data/recorded/RB2405_gateway_replay.csv --symbol RB2405
python -m futures_quant backtest --config configs/backtest.json --data-path data/recorded/RB2405_gateway_replay.csv --symbol RB2405 --report-path reports/gateway_replay_summary.csv
```

这条链路验证的是：只要真实 API 适配层能产出项目内部的 `Bar`，后续落盘和回测结果输出就可以复用。

## 多品种历史数据

项目当前提供一个确定性 demo 历史数据生成器，用于验证批量回测流程：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant generate-demo-history --universe configs/universe.json --output-dir data/history
python -m futures_quant batch-backtest --config configs/backtest_multi.json --universe configs/universe.json --history-dir data/history --report-path reports/multi_asset_summary.csv
```

覆盖范围：

- 国内期货：螺纹钢、铁矿石、沪铜、沪金、豆粕、PTA、沪深300股指期货。
- 加密货币：BTCUSDT。
- 国际期货：WTI 原油、COMEX 黄金、标普500 E-mini。

重要说明：demo 历史数据只用于工程验证，不代表真实市场行情。真实接入时，把历史 API 下载的数据转换成同样的 CSV 格式即可复用批量回测。

## 历史数据 Provider

命令：

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

当前 provider：

- `synthetic`：本地确定性数据，只适合验证工程链路；由它得到的收益、Sharpe 和排名均不代表真实市场表现。
- `akshare`：调用 `akshare.futures_zh_daily_sina` 获取国内期货历史数据，需要本机安装 AKShare。
- `binance`：调用 Binance spot kline API 获取 BTCUSDT 等历史数据，需要公网可访问。
- `http`：读取交易软件或数据平台提供的 CSV/JSON 历史行情接口，配置文件通过 `--provider-config` 传入。

### 通用 HTTP 历史接口

复制 `configs/http_history.example.json` 为本机配置并修改：

- `url_template`：请求地址，支持 `{symbol}`、`{start}`、`{end}`。
- `format`：`csv` 或 `json`。
- `headers`：鉴权请求头；示例 Token 必须替换，真实密钥不要提交到仓库。
- `timeout`：单次请求超时秒数。
- `encoding`：响应编码，默认 `utf-8`。
- `data_path`：仅 JSON 使用，按层列出 K 线数组路径，例如 `["data", "bars"]`。
- `field_mapping`：方向固定为“项目标准字段 -> 接口原字段”，例如 `{"datetime": "trade_time", "open": "open_price"}`。

项目标准必填字段为 `datetime/open/high/low/close/volume`，`open_interest` 可缺省并自动记为 0。接口返回的数据会按 universe 的起止日期过滤并按时间升序排列。

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant fetch-history --provider http --provider-config configs/http_history.json --universe configs/universe.json --output-dir data/history_http --suffix _1d.csv
python -m futures_quant run-pipeline --provider http --provider-config configs/http_history.json --universe configs/universe.json --history-dir data/history_http --suffix _1d.csv --config configs/backtest_multi.json
```

若交易软件只能导出文件，可把 `url_template` 写成 `file:///D:/history/{symbol}.csv`，用同一 Provider 离线验证字段映射和完整流水线，再切换到真实 HTTP 地址。

真实交易软件历史接口也应做成 provider：输入 universe 中的 `symbol/start/end`，输出项目内部 `Bar` 列表，最后用 `CsvBarRecorder` 写成标准 CSV。

批量回测后可用 `analyze-batch` 查看：

- 等权组合净值和组合总收益/最大回撤。
- 国内期货、加密货币、国际期货的分组表现。
- 按 Sharpe 和收益排序的品种排名。
- 可直接阅读的 Markdown 回测报告。

`validate-history` 会输出 `reports/data_quality_report.csv`，用于在回测前检查历史数据是否存在缺字段、重复 bar、日期排序问题、OHLC 关系错误、空值、非正价格等问题。

`run-pipeline` 在任一品种请求失败时会停止，并把接口错误写入历史目录的 `_fetch_manifest.csv`；manifest 同时记录 provider 和配置文件路径用于结果追溯，但不会写入配置内容或请求头 Token。默认也会在数据质量报告出现 warning/error 时停止。如果你确认质量问题可接受，可以加 `--allow-warnings` 继续执行，但该参数不会忽略接口拉取失败。

## 你需要提供的材料

- 交易软件或柜台类型：CTP、QMT、Ptrade、天勤、掘金、文华、迅投等。
- SDK 文件、安装包、Python 包名或官方接入文档。
- 仿真环境前置地址和账号信息。
- 合约参数表：合约乘数、最小变动价位、保证金率、手续费。
- 你希望回测的数据周期：日线、分钟线、tick 或多周期组合。
