# CTP 网关：无密码开发与安全接入

当前项目已经具备可测试的 CTP 编排层，但**尚未连接长江期货，也没有发送任何委托**。真实 SDK 的包名、二进制版本和字段映射由期货公司决定，因此项目把它隔离成可选适配器；没有安装适配器时会输出明确诊断，不会回退到未知实现。

## 当前已经实现的部分

- `CtpConfig`：从 JSON 和环境变量读取配置，密码与 AuthCode 使用脱敏对象保存，默认禁止在 JSON 内写明文密钥。
- `CtpAdapter`：SDK 无关的认证、行情、委托、成交、持仓和资金回调协议。
- `CtpGateway`：连接状态机、结算确认后的账户同步、行情订阅、报单/撤单映射、回调去重、心跳超时与指数退避重连框架。
- 下单前风控：单次手数、单品种持仓、持仓品种数、未完成委托数、每日请求数、单品种保证金和总保证金占比。
- 安全开关：默认 `enabled=false`；实盘还要求 `mode=live`、`live_trading_enabled=true`、程序化交易报备确认，以及连接后限时二次 `arm`。

这些功能均可用 `FakeCtpAdapter` 离线测试，不需要账号、密码、网络或真实 SDK。

## 文件位置

- 配置与环境变量：`src/futures_quant/api/ctp_config.py`
- SDK 适配协议和标准回调：`src/futures_quant/api/ctp_adapter.py`
- 状态机与下单前风控：`src/futures_quant/api/ctp_gateway.py`
- 离线 Fake 测试：`tests/test_ctp_gateway.py`
- 长江期货安全模板：`configs/api.changjiang.example.json`

## 配置安全规则

模板必须保持禁用：

```json
{
  "mode": "paper",
  "enabled": false,
  "live_trading_enabled": false,
  "programmatic_trading_report_confirmed": false,
  "user_id_env": "CJFCO_USER_ID",
  "password_env": "CJFCO_PASSWORD",
  "app_id_env": "CJFCO_APP_ID",
  "auth_code_env": "CJFCO_AUTH_CODE"
}
```

不要把密码或 AuthCode 写入 JSON、源码、测试、日志、截图或聊天。加载器默认拒绝 JSON 中的明文 `password` 和 `auth_code`。`repr(config)`、状态窗口和 SDK 诊断均不返回这两个字段。

只进行 SDK/配置诊断时，不需要设置任何密钥，也不需要登录：

```python
from futures_quant.api.ctp_adapter import diagnose_ctp_adapter
from futures_quant.api.ctp_config import load_ctp_config

cfg = load_ctp_config("configs/api.changjiang.example.json", environ={})
print(diagnose_ctp_adapter(cfg))  # 仅检查入口导入，不实例化工厂、不联网
```

也可直接运行项目内置的离线诊断命令；它强制使用空环境变量映射，因此不会读取本机
账号、密码或 AuthCode：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant ctp-diagnose `
  --config configs\api.changjiang.example.json
```

输出中的 `credential_environment_read=false` 和 `network_action=false` 是该命令的
固定安全保证；它只检查非敏感模板字段和 SDK 适配入口。

## 适配器需要完成的映射

期货公司 SDK 包装器应实现 `CtpAdapter` 的九个方法，并把 SDK 回调转换成项目类型：

1. 连接行情/交易前置，在成功节点依次回调连接、认证、登录和结算确认状态。
2. 把行情转换为标准 `Bar`，并通过 `on_bar` 推送。
3. 把委托回报转换为 `CtpOrderUpdate`，把成交回报转换为 `CtpTradeUpdate`。
4. 聚合 CTP 按方向、今昨仓拆分的持仓记录，转换为 `CtpPositionSnapshot`。
5. 把资金查询转换为 `CtpAccountSnapshot`。
6. 完成查询后必须调用 `on_order_query_complete()` 和 `on_trade_query_complete()`；账户和持仓使用 `is_last=True` 收尾。四类初始同步全部完成前，网关不会进入 `READY`。
7. 上期所/能源中心平仓时，适配器应依据今昨仓把通用 `close` 正确拆成 `close_today`/`close_yesterday`。通用网关不会猜测该字段。

可选 SDK 通过 `adapter` 字段配置为 `package.module:factory`。工厂返回一个实现 `CtpAdapter` 的对象。项目不会自动尝试导入名字相似但版本未知的 CTP 包。

## 下单安全门

仿真环境也必须先明确设置 `enabled=true`。实盘则需要全部满足：

1. 长江期货已经完成程序化交易信息报告并开通对应权限。
2. `mode=live`。
3. `live_trading_enabled=true`。
4. `programmatic_trading_report_confirmed=true`。
5. 网关完成登录、结算确认、资金/持仓/委托/成交同步，状态为 `READY`。
6. 本次连接后由操作者调用 `arm_live_trading("ARM LIVE TRADING")`；默认五分钟过期，断线立即失效。
7. 委托通过所有本地风控。

撤单不要求 live arm，因为断开解锁后仍应允许撤掉风险中的活动委托。未连接、未完成同步、缺少权威资金或合约乘数/保证金率、跨零反手、双向持仓含义不明确时，系统都会拒单。

## 后续需要人工授权的阶段

以下工作现在没有执行：

- 安装或加载长江期货指定 CTP SDK。
- 输入账户、密码、AppID 或 AuthCode。
- 登录行情/交易前置。
- 确认结算单、查询真实账户、订阅真实行情。
- 发送仿真或实盘委托。

获得期货公司提供的**仿真 SDK 文档与安装包**后，可先只实现 SDK 适配器并继续用 Fake/录制回调测试；随后由账户本人在本机设置临时环境变量，先做只读登录与账务核对。真实下单应最后单独授权。
