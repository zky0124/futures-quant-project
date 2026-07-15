# 共享账户多品种组合回测

`futures_quant.broker.portfolio` 用一个现金、权益和保证金池同步回测多个合约，解决批量单品种回测各自拥有完整初始资金、事后等权拼接带来的资金占用失真。

核心时序如下：同一时间戳先一次性更新所有可用开盘价，再执行各品种上一根收盘产生的待成交信号；随后一次性更新所有收盘价，最后调用策略生成下一根待成交信号。减仓订单先于开仓订单执行，多个竞争开仓按 symbol 排序，因此共享保证金不足时的结果可重复。某品种缺少同时间戳 K 线时，沿用它最近的收盘估值，其待成交信号等到该品种下一根实际 K 线开盘才执行。

```python
from futures_quant.broker.portfolio import (
    PortfolioRiskLimits,
    SharedPortfolioBroker,
    run_portfolio_backtest,
)

broker = SharedPortfolioBroker(
    initial_cash=500_000,
    contract_specs=contract_specs,  # dict[str, ContractSpec]
    risk_limits=PortfolioRiskLimits(
        max_margin_usage=0.45,
        max_symbol_exposure=0.10,
        daily_loss_stop=0.02,
    ),
    slippage_ticks=1,
)
result = run_portfolio_backtest(bars_by_symbol, strategy, broker)
```

每个 symbol 必须有自己的 `ContractSpec`，成交价按该合约 `tick_size` 计算滑点，盈亏与名义价值使用 `contract_multiplier`，保证金和佣金分别使用该合约的 `margin_rate`、`commission_rate`。账户级 `max_margin_usage` 检查所有持仓的合计保证金；`max_symbol_exposure` 检查单一品种的名义敞口。触发当日亏损阈值后，只允许保持原方向且绝对仓位变小的减仓，或直接平仓；跨零反手会被拒绝。期末所有剩余持仓按各自最后可用收盘价平仓，并正常计入双边滑点和佣金。

结果包含：

- `equity_curve`：每个组合时间戳一行的共享现金、权益、保证金、名义敞口和持仓快照；
- `trades`：逐笔成交及合约参数、参考价、成交价、滑点成本、佣金和已实现盈亏；
- `rejections`：风控拒单及原因；
- `summary`：组合收益、回撤、Sharpe、成本、最大保证金占用和拒单统计。

## 币种限制

当前模块没有 FX 汇率序列，也不会把人民币、美元和 USDT 盈亏直接相加。未提供币种映射时只接受 SHFE、INE、DCE、CZCE、GFEX、CFFEX 的人民币合约。境外或加密合约必须显式传入 `base_currency` 与完整 `symbol_currencies`，并且所有活跃品种币种必须与基础币种相同；出现混合币种会直接报错。若要把国内期货、国际期货和比特币放入同一真实组合，需要先实现按时间戳对齐的 FX/稳定币换算和相应保证金规则。
