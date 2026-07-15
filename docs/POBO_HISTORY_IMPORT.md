# 博易大师 PoboHis 行情导入

批量扫描全部本地缓存、按项目品种映射并生成来源审计清单的用法，见
[`POBO_BATCH_IMPORT.md`](POBO_BATCH_IMPORT.md)。

项目可以把博易大师本地 `5Min/*.his` 缓存转换为标准 CSV：

```text
datetime,symbol,open,high,low,close,volume,open_interest
```

## 先在博易大师补齐可用缓存

1. 打开需要的实际合约、主力或连续合约，并切换到 15 分钟 K 线。
2. 在 K 线窗口内连续按 `Ctrl+Left`，每次向历史方向移动整屏；到达本地边界时，
   博易会从历史服务器继续请求并把记录写入 `Data/<市场>/5Min/*.his`。
3. 等待页面完成刷新后继续按 `Ctrl+Left`，直到文件大小不再增长或已到达目标日期。
4. 按 `Ctrl+End` 返回最新 K 线。

“工具 -> 数据刷新”只会刷新当前窗口；“功能 -> 历史回忆”主要用于最近若干日的
分时回看，都不是三年 K 线批量下载器。博易大师自动填充并登录终端，也不代表本
项目已经取得 CTP 前置地址、AppID、AuthCode 或程序化交易权限。

本机实测螺纹主力 `010690.his` 可完整补到 11,707 根 5 分钟线，覆盖
`2025-10-28 10:45` 至 `2026-07-13 15:00`。这是可审计的真实短样本，但只有约
8 个半月，不能作为“近三年可信回测”。

## 使用命令

在项目根目录执行：

```powershell
$env:PYTHONPATH = "src"
python -m futures_quant import-pobo-his `
  --input "F:\量化\长江期货-博易大师7交易版\pobo7\Data\21005\5Min\010690.his" `
  --output "data\pobo\RB0_15m.csv" `
  --symbol "RB0"
```

`NameTable.xml` 默认从 `.his` 上级市场目录自动读取。例如上面的文件会读取
`Data/21005/NameTable.xml`，并按文件名 `010690` 查找 PBCode，取得名称、BRCode
和 PriceRate。若名称表位于别处，可显式传入：

```powershell
--name-table "D:\Pobo\Data\21005\NameTable.xml"
```

不传 `--symbol` 时，CSV 使用名称表里的 BRCode（示例为 `rb_ZL`）。为了与当前
项目的主力连续品种命名一致，通常建议显式传入 `--symbol RB0`。

## 时间聚合规则

- 输入按博易 5 分钟 K 线读取，默认输出 15 分钟 K 线。
- 15 分钟桶按自然时钟边界结束，例如 `10:50/10:55/11:00` 合成 `11:00`。
- 遇到超过 7.5 分钟的间隔立即切断，午休、夜盘休市和隔夜数据不会合在一起。
- 如果 `.his` 从时段中途开始，文件开头无法确认完整的首个桶会被丢弃，避免
  后面所有 15 分钟 K 线永久错位。
- 可用 `--target-minutes 30` 或 `--target-minutes 60` 生成其他 5 分钟整数倍周期。

## 数据可信度提示

导入器只转换博易当前已经下载到本地的记录，不会自动补齐三年历史。运行后应
检查命令输出的 `source_bar_count`、CSV 首尾时间，并继续运行 `validate-history`
做重复时间、OHLC 和缺失值检查。主力连续序列还需要单独核对换月规则和价格
调整方式，不能仅凭文件名把短期缓存视为完整三年真实行情。
