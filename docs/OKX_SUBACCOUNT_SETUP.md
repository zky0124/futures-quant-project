# OKX 子账户 REST API 安全接入

当前实现完成了 OKX REST 接入的底层准备，但默认不会联网、不会查询私人账户、也不会下单。它与国内期货 CTP 接口相互独立；OKX 数据和订单不能当作国内期货数据或国内券商成交。

当前官方 REST 基址使用 `https://openapi.okx.com`。Demo 与 Live 使用不同类型的 API Key；Demo 私有请求还会自动携带 `x-simulated-trading: 1`。

## 已具备的能力

- 公共接口：产品列表、行情列表、K 线及历史 K 线。
- 私有只读接口：账户配置、余额、持仓、未完成委托、单笔委托、委托历史、归档委托历史、成交历史。
- 公共服务器时间校验、最近3天与最近3个月成交查询、受身份保护的撤单接口。
- 按 OKX 规范完成 `HMAC-SHA256` 签名，查询参数和 JSON 请求体都会进入签名原文。
- `demo` 模式自动发送 `x-simulated-trading: 1`，`live` 模式不发送该标头。
- 使用 `/api/v5/account/config` 核验 API Key 所属 UID 与主账户 UID。子账户应满足 `uid != mainUid`；准备下单时还必须与预先填写的 `expected_uid` 完全一致。
- 下单接口已实现，但配置、子账户身份、Trade 权限、品种白名单、单笔数量上限和短时运行解锁任一条件不满足时都会拒绝。
- 默认要求 API Key 已绑定 IP；检测到 Withdraw 权限会直接拒绝该 Key。

子账户名称不会被当作安全身份依据。子账户 API Key 的账户配置响应能够可靠提供 UID 和主 UID，而不保证提供可用于核验的子账户名称；因此本项目使用 UID 精确匹配。

## 文件位置

- 配置加载与安全门：`src/futures_quant/api/okx_config.py`
- REST、签名、查询和下单：`src/futures_quant/api/okx_rest.py`
- 不含凭据的模板：`configs/api.okx.example.json`
- 环境变量名称示例：`configs/okx.env.example`
- 模拟 HTTP 测试：`tests/test_okx_rest.py`

## API Key 应如何创建

在 OKX 子账户中创建专用 API Key，建议：

1. Key 必须属于准备接入的子账户，不使用主账户 Key。
2. 首次核验只授予 Read 权限；准备模拟盘下单时才增加 Trade 权限。
3. 不授予 Withdraw/提币权限。
4. 配置 OKX 支持的 IP 白名单；网络出口变化时先停用 Key。
5. Demo Key 只配 `environment: "demo"`；Live Key 只配 `environment: "live"`。两种 Key 不混用。
6. API Passphrase 是创建 API Key 时设置的口令，不是 OKX 登录密码。

不要把 API Key、Secret Key 或 Passphrase 发到聊天中、写入 JSON、提交 Git，或截图展示。项目拒绝从 JSON 读取这三项，只允许由进程环境变量提供。

## 分阶段启用

### 第一阶段：仅公共行情

公共客户端不需要 API Key：

```python
from futures_quant.api.okx_rest import OkxPublicClient

client = OkxPublicClient()
rows = client.get_candles("BTC-USDT-SWAP", bar="15m", limit=100)
```

调用这段代码会实际联网。测试代码使用模拟传输层，不会联网。

### 第二阶段：子账户只读核验

复制模板到 Git 已忽略的本地配置：

```powershell
Copy-Item configs\api.okx.example.json configs\api.okx.local.json
```

只修改本地 JSON 中以下字段：

```json
{
  "enabled": true,
  "environment": "demo",
  "private_api_enabled": true,
  "order_submission_enabled": false,
  "live_trading_enabled": false
}
```

不要把值填入 `configs/okx.env.example`。最简单也最安全的首次核验方式，是双击项目根目录的 `启动OKX只读诊断.bat`：脚本会在本机窗口中隐蔽读取 API Key、Secret Key 和 Passphrase，只把它们放入当前诊断进程，完成、失败或输入中断时都会清除。它不会保存凭据，也不会下单。

若需要手动运行，可在当前 PowerShell 会话中隐蔽输入三项值：

```powershell
$k = Read-Host "OKX API Key" -AsSecureString
$env:OKX_API_KEY = [Net.NetworkCredential]::new("", $k).Password
Remove-Variable k
$s = Read-Host "OKX Secret Key" -AsSecureString
$env:OKX_SECRET_KEY = [Net.NetworkCredential]::new("", $s).Password
Remove-Variable s
$p = Read-Host "OKX API Passphrase" -AsSecureString
$env:OKX_PASSPHRASE = [Net.NetworkCredential]::new("", $p).Password
Remove-Variable p
```

这些变量只在当前 PowerShell 及其子进程中生效。手动命令执行完后应立即关闭该窗口，或显式删除三项环境变量；日常使用优先双击启动脚本，让它自动清理。

首次只读核验：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant okx-diagnose `
  --config configs\api.okx.local.json `
  --connect-read-only
```

该命令只执行“服务器校时 → 子账户身份 → 余额条目数 → 持仓条目数 → 未完成订单条目数”，不会下单、撤单，也不会打印密钥和账户金额。

也可以在 Python 中调用：

```python
from futures_quant.api.okx_config import load_okx_config
from futures_quant.api.okx_rest import OkxPrivateClient

cfg = load_okx_config("configs/api.okx.local.json")
client = OkxPrivateClient(cfg)
identity = client.verify_subaccount_identity()
print(identity.redacted_summary())
print(client.get_balances())
print(client.get_positions(inst_type="SWAP"))
print(client.get_pending_orders(inst_type="SWAP"))
```

核对 UID 后，把目标子账户 UID 放进 `OKX_EXPECTED_UID`；可同时把主账户 UID 放进 `OKX_EXPECTED_MAIN_UID`。没有 `OKX_EXPECTED_UID` 时，代码允许只读核验，但绝不允许下单。

### 第三阶段：Demo 减仓单验证

只有完成只读核验后才进入这一阶段。保持 `environment: "demo"` 和 `live_trading_enabled: false`，并在本地配置中显式设置：

```json
{
  "order_submission_enabled": true,
  "allow_opening_orders": false,
  "allowed_order_types": ["limit"],
  "order_limits": {
    "BTC-USDT-SWAP": "0.01"
  }
}
```

`order_limits` 的数值是 OKX 请求字段 `sz` 的单位，不统一等于“币”或“张”：衍生品通常按合约张数，现货则取决于产品和交易参数。必须先读取产品信息中的合约面值、最小下单量和步长，确认单位后再设置，不能照抄示例数字。

Demo 下单还要在当前进程完成身份核验并短时解锁：

```python
from futures_quant.api.okx_rest import OkxOrderRequest

client.verify_subaccount_identity()
client.arm_order_submission(client.order_arm_phrase)
ack = client.place_order(
    OkxOrderRequest(
        inst_id="BTC-USDT-SWAP",
        side="sell",
        size="0.01",
        price="70000",
        trade_mode="cross",
        order_type="limit",
        reduce_only=True,
    )
)
print(ack)
client.disarm_order_submission()
```

已存在订单的撤单不要求开启开仓权限或保持短时下单解锁，但仍必须先精确核验子账户 UID、确认 Trade 权限：

```python
ack = client.cancel_order("BTC-USDT-SWAP", order_id="OKX_ORDER_ID")
print(ack)
```

模板默认只允许限价、只减仓订单。开放开仓需要另行把 `allow_opening_orders` 改为 `true`，这不应在首次接入时进行。

## Live 模式额外门槛

Live 查询可在保持 `order_submission_enabled: false` 时进行。Live 发单同时要求：

- `environment: "live"`
- `private_api_enabled: true`
- `order_submission_enabled: true`
- `live_trading_enabled: true`
- `require_subaccount: true`
- 精确配置并核验 `OKX_EXPECTED_UID`
- 当前 API Key 返回 Trade 权限
- 当前产品位于 `order_limits` 且数量不超过上限
- 运行时输入精确解锁短语；解锁默认 120 秒后失效

建议在工作台接入阶段继续保持 Live 发单关闭。API 接通、账户状态展示、Demo 委托与成交回报全部验收后，再单独评审实盘权限。

## 历史数据边界

`get_order_history()`、`get_order_history(archive=True)` 和 `get_fills_history()` 已分别接好当前/归档委托与成交端点，支持 `after`、`before`、`limit` 分页参数。OKX 会限制单次条数和在线保留时长；超过在线保留范围的数据需要按 OKX 当前规则导出并本地归档，不能假设 REST 接口永久保存全部历史。

本模块不会把策略信号自动接到 OKX 发单。启用自动策略交易前仍必须明确交易产品、合约规格、账户持仓模式和风控口径，并先完成 Demo 验收。

公共 OKX K 线现在可以独立保存为项目标准 CSV，不读取任何 API 密钥：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant okx-download-history `
  --inst-id BTC-USDT-SWAP `
  --bar 15m `
  --start 2025-11-01 `
  --output data\okx_real_15m\BTC-USDT-SWAP_15m.csv
```

下载器按官方规则每页最多300根、使用最老时间戳向过去翻页，去重、升序并排除 `confirm=0` 的未收盘K线；同时写入来源清单。OKX 加密资产 CSV 必须放在独立目录，不能混入 `data/pobo_real_15m`。
