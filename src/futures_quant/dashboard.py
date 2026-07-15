from __future__ import annotations

from collections.abc import Iterable
import importlib.util
import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, BooleanVar, Canvas, StringVar, Text, Tk, filedialog, messagebox
from tkinter import ttk

import pandas as pd

from futures_quant.analysis.scanner import scan_instruments
from futures_quant.broker.portfolio import (
    PortfolioRiskLimits,
    SharedPortfolioBroker,
    run_portfolio_backtest,
)
from futures_quant.cli import build_strategy, strategy_parameters
from futures_quant.config import StrategyConfig
from futures_quant.data.contracts import load_contract_specs
from futures_quant.data.csv_loader import load_bars
from futures_quant.data.history import Instrument, load_universe
from futures_quant.data.pobo import import_pobo_his
from futures_quant.data.timeframe import aggregate_bars, validate_intervals
from futures_quant.optimization.walk_forward import optimize_strategy
from futures_quant.presentation.chinese import (
    chinese_column_name,
    chinese_summary_frame,
    chinese_value,
    write_chinese_csv,
    write_chinese_companion,
)


STRATEGY_LABELS = {
    "强化自适应（研究候选，待真实三年验证）": "adaptive_trend_v2",
    "MA169穿越/MA13分批（15分钟默认）": "dual_ma_pullback",
    "双周期反转": "dual_period_reversal",
    "双均线基准（原始）": "dual_ma",
    "自适应趋势": "adaptive_trend",
}

DEFAULT_STRATEGY_LABEL = "自适应趋势"
DUAL_MA_STRESS_WARNING = (
    "⚠ 高风险压力测试：该配置允许单品种使用60%权益保证金预算、单笔风险3%。"
    "螺纹真实短样本曾回撤约25%，仅用于研究，不是推荐实盘参数。"
)

CTP_SDK_MODULE_CANDIDATES = (
    "vnpy_ctp",
    "openctp_ctp",
    "thostmduserapi",
    "thosttraderapi",
    "ctpbee_api",
)


@dataclass(frozen=True)
class DataSourceClassification:
    kind: str
    label: str
    detail: str


@dataclass(frozen=True)
class ApiReadiness:
    status_code: str
    status: str
    safe_fields: tuple[tuple[str, str], ...]
    problems: tuple[str, ...]


def classify_data_source(path: str | Path) -> DataSourceClassification:
    """Classify a data path without treating an unlabelled file as real data."""

    resolved = Path(path)
    manifest_candidates: list[Path] = []
    if resolved.is_file():
        manifest_candidates.append(
            resolved.with_suffix(resolved.suffix + ".source.json")
        )
    else:
        manifest_candidates.append(resolved / "_source_manifest.json")
        if resolved.exists():
            manifest_candidates.extend(
                sorted(resolved.glob("*.csv.source.json"))
            )

    declared_kinds: set[str] = set()
    declared_providers: set[str] = set()
    for manifest_path in manifest_candidates:
        if not manifest_path.exists():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        kind = str(payload.get("data_kind", "")).strip().lower()
        provider = str(payload.get("provider", "")).strip().lower()
        if kind:
            declared_kinds.add(kind)
        if provider:
            declared_providers.add(provider)

    synthetic_kinds = {"synthetic", "synthetic_engineering", "demo"}
    real_kinds = {"real", "real_market", "terminal_export"}
    has_synthetic = bool(declared_kinds & synthetic_kinds)
    has_real = bool(declared_kinds & real_kinds)
    if has_synthetic and has_real:
        return DataSourceClassification(
            "mixed",
            "混合数据（不可用于可信回测）",
            "目录同时包含真实与合成来源标记，请拆分目录后再运行。",
        )
    if has_synthetic:
        return DataSourceClassification(
            "synthetic",
            "合成数据（仅工程验证）",
            "收益、Sharpe和品种排名不可解释为真实市场表现。",
        )
    if has_real:
        provider = "、".join(sorted(declared_providers)) or "已标记来源"
        return DataSourceClassification(
            "real",
            "真实行情（来源已标记）",
            f"来源：{provider}；真实性不等于覆盖期足够，仍须检查起止日期。",
        )

    normalized = str(resolved).replace("\\", "/").lower()
    parts = {part.lower() for part in resolved.parts}
    synthetic_markers = ("synthetic", "demo", "sample")
    if "domestic_15m" in parts or any(
        marker in normalized for marker in synthetic_markers
    ):
        return DataSourceClassification(
            "synthetic",
            "合成数据（仅工程验证）",
            "目录名表明这是演示/合成数据，不能据此判断策略盈利能力。",
        )
    if "pobo_real_15m" in parts or "pobo_real" in normalized:
        return DataSourceClassification(
            "real",
            "真实行情（博易缓存导入）",
            "来自行情终端缓存；必须在覆盖范围表中确认日期，短样本不等于三年可信回测。",
        )
    return DataSourceClassification(
        "unknown",
        "来源未标注（禁止视为真实数据）",
        "请通过博易导入生成来源清单，或为外部数据补充可审计的来源说明。",
    )


def scan_data_directory(path: str | Path, suffix: str) -> pd.DataFrame:
    """Return a lightweight coverage audit for bar CSV files in one directory."""

    directory = Path(path)
    if not directory.exists():
        raise FileNotFoundError(f"数据目录不存在：{directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"数据路径不是目录：{directory}")
    suffix = suffix.strip()
    if not suffix:
        raise ValueError("文件后缀不能为空，例如 _15m.csv。")
    files = sorted(directory.glob(f"*{suffix}"))
    if not files:
        raise FileNotFoundError(
            f"目录中没有匹配 *{suffix} 的文件：{directory}"
        )

    directory_classification = classify_data_source(directory)
    rows: list[dict[str, object]] = []
    for csv_path in files:
        symbol = csv_path.name[: -len(suffix)] if suffix else csv_path.stem
        try:
            header = pd.read_csv(csv_path, nrows=0)
            required = {"datetime", "symbol"}
            missing = required - set(header.columns)
            if missing:
                raise ValueError(f"缺少字段：{sorted(missing)}")
            frame = pd.read_csv(csv_path, usecols=["datetime", "symbol"])
            if frame.empty:
                raise ValueError("文件没有K线记录")
            timestamps = pd.to_datetime(frame["datetime"], errors="coerce")
            if timestamps.isna().any():
                raise ValueError(
                    f"有 {int(timestamps.isna().sum())} 条时间无法解析"
                )
            start = timestamps.min()
            end = timestamps.max()
            calendar_days = max(0, int((end - start).total_seconds() // 86400))
            file_classification = classify_data_source(csv_path)
            if file_classification.kind == "unknown":
                file_classification = directory_classification
            rows.append(
                {
                    "symbol": symbol,
                    "bars": len(frame),
                    "start": start.strftime("%Y-%m-%d %H:%M"),
                    "end": end.strftime("%Y-%m-%d %H:%M"),
                    "calendar_days": calendar_days,
                    "three_year_check": "约3年或以上" if calendar_days >= 1000 else "不足3年",
                    "data_kind": file_classification.label,
                    "status": "ok",
                    "error": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "symbol": symbol,
                    "bars": 0,
                    "start": "",
                    "end": "",
                    "calendar_days": 0,
                    "three_year_check": "无法判断",
                    "data_kind": directory_classification.label,
                    "status": "error",
                    "error": str(exc),
                }
            )
    return pd.DataFrame(rows)


def available_instruments_for_data(
    instruments: Iterable[Instrument], data_dir: str | Path, suffix: str
) -> list[Instrument]:
    """Return only universe instruments with a matching local bar file.

    The workbench deliberately keeps the full universe visible so a user can
    see what is missing, but one-click actions must never submit those missing
    symbols to a backtest or scan.
    """

    directory = Path(data_dir)
    normalized_suffix = suffix.strip()
    if not normalized_suffix or not directory.is_dir():
        return []
    return [
        instrument
        for instrument in instruments
        if (directory / f"{instrument.symbol}{normalized_suffix}").is_file()
    ]


def _has_ctp_sdk(adapter_spec: str = "") -> bool:
    adapter_module = adapter_spec.partition(":")[0].strip()
    if adapter_module:
        try:
            return importlib.util.find_spec(adapter_module) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            return False
    return any(
        importlib.util.find_spec(module_name) is not None
        for module_name in CTP_SDK_MODULE_CANDIDATES
    )


def assess_api_readiness(
    path: str | Path,
    *,
    sdk_available: bool | None = None,
    adapter_available: bool | None = None,
) -> ApiReadiness:
    """Inspect only non-sensitive API metadata; credentials are never returned."""

    config_path = Path(path)
    if not config_path.exists():
        return ApiReadiness(
            "missing_config",
            "配置缺失：尚不能连接API",
            (("配置文件", str(config_path)), ("真实交易", "强制关闭")),
            ("请选择存在的本地JSON配置或示例配置。",),
        )
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ApiReadiness(
            "missing_config",
            "配置不可读：尚不能连接API",
            (("配置文件", str(config_path)), ("真实交易", "强制关闭")),
            (f"JSON读取失败：{exc}",),
        )
    if not isinstance(payload, dict):
        return ApiReadiness(
            "missing_config",
            "配置格式错误：尚不能连接API",
            (("配置文件", str(config_path)), ("真实交易", "强制关闭")),
            ("API配置顶层必须是JSON对象。",),
        )

    gateway = str(payload.get("gateway", "")).strip().lower()
    enabled = payload.get("enabled", False) is True
    trade_front = str(
        payload.get("trade_front") or payload.get("front_addr") or ""
    ).strip()
    market_front = str(
        payload.get("market_front")
        or payload.get("market_data_addr")
        or ""
    ).strip()
    broker_id = str(payload.get("broker_id", "")).strip()
    adapter_spec = str(payload.get("adapter", "")).strip()
    placeholder_markers = ("your_", "your-", "example", "待填写")

    def usable(value: str) -> bool:
        lowered = value.lower()
        return bool(value) and not any(marker in lowered for marker in placeholder_markers)

    missing: list[str] = []
    if not gateway:
        missing.append("gateway")
    if gateway == "ctp":
        if not usable(trade_front):
            missing.append("trade_front/front_addr")
        if not usable(market_front):
            missing.append("market_front/market_data_addr")
        if not usable(broker_id):
            missing.append("broker_id")

    sdk_found = (
        _has_ctp_sdk(adapter_spec) if sdk_available is None else sdk_available
    )
    adapter_found = (
        bool(adapter_spec) and sdk_found
        if adapter_available is None
        else adapter_available
    )
    credential_reference_count = sum(
        bool(str(payload.get(key, "")).strip())
        for key in ("user_id_env", "password_env", "app_id_env", "auth_code_env")
    )
    direct_credential_count = sum(
        key in payload and bool(str(payload.get(key, "")).strip())
        for key in ("user_id", "password", "app_id", "auth_code")
    )
    credential_description = (
        f"配置了 {credential_reference_count} 个环境变量名；未读取变量值"
        if credential_reference_count
        else (
            f"检测到 {direct_credential_count} 个直接字段；值不显示、不保存"
            if direct_credential_count
            else "未配置；按要求暂停凭证接入"
        )
    )
    risk_limits = payload.get("risk_limits", {})
    if not isinstance(risk_limits, dict):
        risk_limits = {}
    report_confirmed = payload.get("programmatic_trading_report_confirmed") is True
    report_required = payload.get("programmatic_trading_report_required") is True
    safe_fields = (
        ("配置文件", str(config_path)),
        ("网关类型", gateway or "未填写"),
        ("期货公司", str(payload.get("broker_name", "未填写"))),
        ("Broker ID", broker_id or "未填写"),
        ("运行模式", str(payload.get("mode", "未填写"))),
        ("交易前置", trade_front or "未填写"),
        ("行情前置", market_front or "未填写"),
        ("API版本", str(payload.get("api_version", "未填写"))),
        ("SDK适配入口", adapter_spec or "未配置"),
        (
            "程序化交易报备",
            "已确认"
            if report_confirmed
            else ("需要且未确认" if report_required else "未确认（禁止实盘）"),
        ),
        ("配置enabled", "是（但工作台仍强制关闭）" if enabled else "否"),
        (
            "配置live_trading",
            "是（但工作台仍强制关闭）"
            if payload.get("live_trading_enabled") is True
            else "否",
        ),
        ("凭证", credential_description),
        ("CTP SDK模块", "已发现" if sdk_found else "缺失/未配置"),
        (
            "SDK适配器",
            "模块可定位（未实例化）" if adapter_found else "未装载",
        ),
        ("项目CTP网关层", "已实现；默认禁用并带运行时风控"),
        (
            "API单笔手数上限",
            str(risk_limits.get("max_order_volume", "配置未声明")),
        ),
        (
            "API最大持仓品种",
            str(risk_limits.get("max_open_symbols", "配置未声明")),
        ),
        (
            "API单品种保证金上限",
            str(risk_limits.get("max_symbol_margin_fraction", "配置未声明")),
        ),
        ("真实交易", "强制关闭；本页不能启用"),
    )

    problems: list[str] = []
    if missing:
        problems.append(f"配置缺项：{', '.join(missing)}")
    if gateway == "ctp" and not adapter_spec:
        problems.append("未配置券商CTP SDK适配入口（package.module:factory）。")
    elif gateway == "ctp" and not sdk_found:
        problems.append("已配置的CTP SDK适配模块无法导入。")
    elif gateway == "ctp" and not adapter_found:
        problems.append("CTP SDK适配入口尚未通过只读导入检查。")
    if gateway == "ctp":
        problems.append("凭证完整性/登录检查已按要求暂停，未读取任何密码值。")
    if enabled:
        problems.append("配置虽请求enabled，工作台仍强制禁止真实交易。")

    if missing:
        status_code = "missing_config"
        status = "配置缺项：API不可用"
    elif gateway == "mock":
        status_code = "mock"
        status = "Mock就绪：仅接口工程测试，不会连接券商"
    elif not enabled:
        status_code = "disabled"
        status = "Disabled：真实交易已关闭"
    elif gateway == "ctp" and (not sdk_found or not adapter_found):
        status_code = "ctp_sdk_missing"
        status = "CTP SDK适配器缺失/未配置：真实交易已阻止"
    else:
        status_code = "disabled"
        status = "就绪检查通过，但工作台仍强制关闭真实交易"
    return ApiReadiness(status_code, status, safe_fields, tuple(problems))

INTERVAL_LABELS = {
    "5分钟": 5,
    "15分钟": 15,
    "30分钟": 30,
    "60分钟（1小时）": 60,
    "120分钟（2小时）": 120,
    "240分钟（4小时）": 240,
}


class QuantWorkbench:
    def __init__(self, root: Tk, project_root: Path) -> None:
        self.root = root
        self.project_root = project_root
        self.root.title("国内期货量化策略工作台")
        self.root.geometry("1380x860")
        self.root.minsize(1080, 700)
        self.result = None
        self.scan_results = pd.DataFrame()
        self.scan_cancel_event = threading.Event()
        self.universe_instruments: list[Instrument] = []
        self.visible_instruments: list[Instrument] = []
        self._build_style()
        self._build_ui()
        self._load_universe()

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Metric.TLabel", font=("Consolas", 12, "bold"), foreground="#0b5d4b")
        style.configure(
            "RealData.TLabel",
            font=("Microsoft YaHei UI", 10, "bold"),
            foreground="#087f5b",
        )
        style.configure(
            "WarningData.TLabel",
            font=("Microsoft YaHei UI", 10, "bold"),
            foreground="#b45309",
        )
        style.configure(
            "Danger.TLabel",
            font=("Microsoft YaHei UI", 10, "bold"),
            foreground="#b42318",
        )

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="国内期货量化策略工作台", style="Title.TLabel").pack(side=LEFT)
        self.status = StringVar(value="就绪：请选择品种和 15 分钟数据后运行回测")
        ttk.Label(header, textvariable=self.status).pack(side=RIGHT)

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))
        self.settings_tab = ttk.Frame(self.tabs, padding=10)
        self.data_api_tab = ttk.Frame(self.tabs, padding=10)
        self.summary_tab = ttk.Frame(self.tabs, padding=10)
        self.positions_tab = ttk.Frame(self.tabs, padding=10)
        self.trades_tab = ttk.Frame(self.tabs, padding=10)
        self.pnl_tab = ttk.Frame(self.tabs, padding=10)
        self.curve_tab = ttk.Frame(self.tabs, padding=10)
        self.scan_tab = ttk.Frame(self.tabs, padding=10)
        self.comparison_tab = ttk.Frame(self.tabs, padding=10)
        self.optimization_tab = ttk.Frame(self.tabs, padding=10)
        for tab, title in [
            (self.settings_tab, "回测与策略"),
            (self.data_api_tab, "真实数据与API就绪"),
            (self.summary_tab, "回测摘要"),
            (self.positions_tab, "可视化持仓"),
            (self.trades_tab, "历史成交"),
            (self.pnl_tab, "历史盈亏"),
            (self.curve_tab, "总收益曲线"),
            (self.scan_tab, "全品种扫描"),
            (self.comparison_tab, "策略对比"),
            (self.optimization_tab, "参数优化"),
        ]:
            self.tabs.add(tab, text=title)

        self._build_settings()
        self._build_data_api()
        self._build_summary()
        self.positions_tree = self._tree(self.positions_tab)
        self.trades_tree = self._tree(self.trades_tab)
        self.pnl_tree = self._tree(self.pnl_tab)
        self._build_curve()
        self._build_scanner()
        self.comparison_status = StringVar(value="运行参数优化后显示封存样本外策略对比")
        ttk.Label(
            self.comparison_tab,
            textvariable=self.comparison_status,
            style="Metric.TLabel",
        ).pack(fill="x", anchor="w")
        self.comparison_tree = self._tree(self.comparison_tab)
        self._build_optimization()
        self._on_strategy_changed()
        self._update_data_source_badge()
        self._refresh_api_readiness()

    def _build_settings(self) -> None:
        left = ttk.LabelFrame(self.settings_tab, text="数据与品种", padding=10)
        left.pack(side=LEFT, fill="y", padx=(0, 10))
        right = ttk.LabelFrame(self.settings_tab, text="策略参数与账户风控", padding=10)
        right.pack(side=LEFT, fill=BOTH, expand=True)

        self.universe_var = StringVar(value="configs/universe_domestic_3y.json")
        real_data_dir = self.project_root / "data" / "pobo_real_15m"
        default_data_dir = (
            real_data_dir
            if real_data_dir.exists() and any(real_data_dir.glob("*_15m.csv"))
            else self.project_root / "data" / "domestic_15m"
        )
        self.data_dir_var = StringVar(value=self._display_path(default_data_dir))
        self.suffix_var = StringVar(value="_15m.csv")
        self._path_row(left, "品种池", self.universe_var, self._choose_universe)
        self._path_row(left, "数据目录", self.data_dir_var, self._choose_data_dir)
        self._entry_row(left, "文件后缀", self.suffix_var)
        self.data_source_badge = StringVar()
        self.data_source_badge_label = ttk.Label(
            left,
            textvariable=self.data_source_badge,
            style="Danger.TLabel",
            wraplength=280,
            justify=LEFT,
        )
        self.data_source_badge_label.pack(fill="x", anchor="w", pady=(7, 2))
        self.real_short_sample_button = ttk.Button(
            left,
            text="使用本机真实短样本（约8个月）",
            command=self._use_real_short_sample_data,
        )
        self.real_short_sample_button.pack(fill="x", pady=(2, 2))
        self.available_data_status = StringVar(
            value="当前目录可回测品种：正在读取品种池…"
        )
        ttk.Label(
            left,
            textvariable=self.available_data_status,
            wraplength=280,
            justify=LEFT,
        ).pack(fill="x", anchor="w", pady=(0, 2))
        self.quick_data_scan_button = ttk.Button(
            left,
            text="扫描数据覆盖范围",
            command=self._start_data_scan,
        )
        self.quick_data_scan_button.pack(fill="x", pady=(2, 4))
        ttk.Label(
            left,
            text="可多选；“有数据”才可回测，实盘连续合约需映射到当前可交易合约",
            wraplength=280,
            justify=LEFT,
        ).pack(anchor="w", pady=(7, 3))
        filter_row = ttk.Frame(left)
        filter_row.pack(fill="x", pady=(0, 4))
        self.symbol_search_var = StringVar()
        search = ttk.Entry(filter_row, textvariable=self.symbol_search_var, width=15)
        search.pack(side=LEFT, fill="x", expand=True)
        search.bind("<KeyRelease>", lambda _event: self._filter_universe())
        self.group_filter_var = StringVar(value="全部板块")
        self.group_filter = ttk.Combobox(
            filter_row,
            textvariable=self.group_filter_var,
            values=["全部板块"],
            state="readonly",
            width=14,
        )
        self.group_filter.pack(side=LEFT, padx=(4, 0))
        self.group_filter.bind("<<ComboboxSelected>>", lambda _event: self._filter_universe())
        self.symbol_list = __import__("tkinter").Listbox(
            left, selectmode="extended", width=31, height=21, exportselection=False
        )
        self.symbol_list.pack(fill=BOTH, expand=True)
        list_actions = ttk.Frame(left)
        list_actions.pack(fill="x", pady=(7, 0))
        ttk.Button(
            list_actions,
            text="全选有数据",
            command=self._select_available_symbols,
        ).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(list_actions, text="全选当前", command=lambda: self.symbol_list.selection_set(0, END)).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(list_actions, text="清除", command=lambda: self.symbol_list.selection_clear(0, END)).pack(side=LEFT, fill="x", expand=True, padx=3)
        ttk.Button(list_actions, text="刷新", command=self._load_universe).pack(side=LEFT, fill="x", expand=True)
        self.scan_button = ttk.Button(
            left,
            text="一键扫描当前可用品种收益",
            command=self._start_scan_all,
        )
        self.scan_button.pack(fill="x", pady=(7, 0))
        self.data_dir_var.trace_add(
            "write", lambda *_args: self._refresh_available_data_controls()
        )
        self.suffix_var.trace_add(
            "write", lambda *_args: self._refresh_available_data_controls()
        )

        self.strategy_var = StringVar(value=DEFAULT_STRATEGY_LABEL)
        strategy_row = ttk.Frame(right)
        strategy_row.pack(fill="x", pady=3)
        ttk.Label(strategy_row, text="策略", width=18).pack(side=LEFT)
        selector = ttk.Combobox(
            strategy_row,
            textvariable=self.strategy_var,
            values=list(STRATEGY_LABELS),
            state="readonly",
        )
        selector.pack(side=LEFT, fill="x", expand=True)
        selector.bind("<<ComboboxSelected>>", lambda _event: self._on_strategy_changed())

        period_row = ttk.LabelFrame(right, text="行情周期", padding=(8, 6))
        period_row.pack(fill="x", pady=(7, 3))
        self.source_interval_var = StringVar(value="15分钟")
        self.bar_interval_var = StringVar(value="15分钟")
        self.allow_short_var = BooleanVar(value=True)
        ttk.Label(period_row, text="源数据").pack(side=LEFT)
        ttk.Combobox(
            period_row,
            textvariable=self.source_interval_var,
            values=list(INTERVAL_LABELS),
            state="readonly",
            width=16,
        ).pack(side=LEFT, padx=(5, 16))
        ttk.Label(period_row, text="策略运行周期").pack(side=LEFT)
        ttk.Combobox(
            period_row,
            textvariable=self.bar_interval_var,
            values=list(INTERVAL_LABELS),
            state="readonly",
            width=18,
        ).pack(side=LEFT, padx=(5, 12))
        ttk.Button(
            period_row, text="15分钟", command=lambda: self._set_runtime_interval(15)
        ).pack(side=LEFT)
        ttk.Button(
            period_row,
            text="60分钟/1小时",
            command=lambda: self._set_runtime_interval(60),
        ).pack(side=LEFT, padx=5)
        ttk.Checkbutton(
            period_row, text="允许做空", variable=self.allow_short_var
        ).pack(side=RIGHT)
        self.period_note = StringVar()
        ttk.Label(right, textvariable=self.period_note, foreground="#526274").pack(
            fill="x", anchor="w", pady=(2, 0)
        )
        self.strategy_warning = StringVar()
        ttk.Label(
            right,
            textvariable=self.strategy_warning,
            style="Danger.TLabel",
            wraplength=920,
            justify=LEFT,
        ).pack(fill="x", anchor="w", pady=(2, 0))
        self.bar_interval_var.trace_add("write", lambda *_args: self._update_period_note())

        defaults = {
            "initial_cash": "5000000",
            "order_size": "0",
            "fast_window": "13",
            "slow_window": "169",
            "daily_fast_window": "13",
            "daily_slow_window": "45",
            "extreme_move_threshold": "0.20",
            "extreme_lookback_days": "120",
            "setup_valid_days": "10",
            "atr_window": "14",
            "atr_stop_buffer": "0.25",
            "reward_risk": "2.0",
            "trailing_atr_multiple": "2.5",
            "slope_lookback": "8",
            "pullback_lookback": "5",
            "min_pullback_closes": "2",
            "max_entry_distance_atr": "0.5",
            "partial_exit_size": "2",
            "break_even_trigger_r": "1.0",
            "ma_exit_buffer_atr": "0.1",
            "cooldown_bars": "8",
            "loss_pause_after": "3",
            "loss_pause_bars": "32",
            "max_margin_usage": "0.60",
            "max_symbol_margin_usage": "0.20",
            "max_symbol_exposure": "2.00",
            "max_open_positions": "5",
            "max_trade_risk": "0.005",
            "max_group_positions": "2",
            "daily_loss_stop": "0.02",
            "slippage_ticks": "1",
            "entry_window": "55",
            "exit_window": "20",
            "trend_window": "120",
            "momentum_window": "60",
            "volatility_window": "30",
            "target_annual_volatility": "0.15",
            "max_order_size": "5",
            "max_notional_fraction": "0.10",
            "momentum_threshold": "0.0",
            "annualization_factor": "4032",
            "macd_fast": "12",
            "macd_slow": "26",
            "macd_signal": "9",
            "divergence_lookback": "80",
            "divergence_pivot_radius": "2",
            "divergence_valid_bars": "32",
            "second_cross_window": "48",
            "atr_stop_multiple": "2.5",
            "partial_exit_fraction": "0.30",
            "position_equity_fraction": "0.60",
        }
        labels = {
            "initial_cash": "初始资金（元）",
            "order_size": "固定开仓手数（0=自动）",
            "fast_window": "短均线周期（K线）",
            "slow_window": "长均线周期（K线）",
            "daily_fast_window": "日线短均线",
            "daily_slow_window": "日线长均线",
            "extreme_move_threshold": "大周期极端幅度",
            "extreme_lookback_days": "极端幅度回看日",
            "setup_valid_days": "日线信号有效日",
            "atr_window": "ATR周期",
            "atr_stop_buffer": "ATR止损缓冲",
            "reward_risk": "保护止盈 R 倍数",
            "trailing_atr_multiple": "ATR 跟踪倍数",
            "slope_lookback": "长均线斜率回看",
            "pullback_lookback": "回踩识别窗口",
            "min_pullback_closes": "最少回踩收盘数",
            "max_entry_distance_atr": "最大追价距离ATR",
            "partial_exit_size": "2R部分止盈手数",
            "break_even_trigger_r": "保本触发R倍数",
            "ma_exit_buffer_atr": "均线退出缓冲ATR",
            "cooldown_bars": "离场冷却K线",
            "loss_pause_after": "连续止损次数",
            "loss_pause_bars": "连续止损暂停K线",
            "max_margin_usage": "账户总保证金上限",
            "max_symbol_margin_usage": "单品种保证金上限",
            "max_symbol_exposure": "单品种名义敞口上限",
            "max_open_positions": "最大持仓品种数",
            "max_trade_risk": "单笔止损风险上限",
            "max_group_positions": "同板块持仓上限",
            "daily_loss_stop": "账户单日止损",
            "slippage_ticks": "滑点（tick）",
            "entry_window": "突破入场窗口",
            "exit_window": "通道退出窗口",
            "trend_window": "长期趋势窗口",
            "momentum_window": "动量窗口",
            "volatility_window": "波动率窗口",
            "target_annual_volatility": "目标年化波动率",
            "max_order_size": "最大下单手数",
            "max_notional_fraction": "策略名义敞口比例",
            "momentum_threshold": "动量阈值",
            "annualization_factor": "年化周期数",
            "macd_fast": "MACD快线",
            "macd_slow": "MACD慢线",
            "macd_signal": "MACD信号线",
            "divergence_lookback": "背离回看K线",
            "divergence_pivot_radius": "背离拐点半径",
            "divergence_valid_bars": "背离有效K线",
            "second_cross_window": "二次交叉窗口",
            "atr_stop_multiple": "ATR初始止损倍数",
            "partial_exit_fraction": "部分退出比例（双均线=MA13）",
            "position_equity_fraction": "单品种保证金预算比例",
        }
        self.fields = {key: StringVar(value=value) for key, value in defaults.items()}
        parameter_groups = {
            "核心信号": [
                "order_size", "fast_window", "slow_window", "atr_window",
                "position_equity_fraction", "max_order_size",
            ],
            "退出管理": [
                "ma_exit_buffer_atr", "partial_exit_fraction",
                "atr_stop_buffer", "reward_risk", "partial_exit_size",
                "atr_stop_multiple",
                "break_even_trigger_r", "trailing_atr_multiple",
                "cooldown_bars", "loss_pause_after",
                "loss_pause_bars",
            ],
            "双周期参数": [
                "daily_fast_window", "daily_slow_window",
                "extreme_move_threshold", "extreme_lookback_days",
                "setup_valid_days", "macd_fast", "macd_slow", "macd_signal",
                "divergence_lookback", "divergence_pivot_radius",
                "divergence_valid_bars", "second_cross_window",
            ],
            "自适应参数": [
                "entry_window", "exit_window", "trend_window",
                "momentum_window", "volatility_window",
                "target_annual_volatility", "max_order_size",
                "max_notional_fraction", "momentum_threshold",
                "annualization_factor",
            ],
            "账户风控": [
                "initial_cash", "max_margin_usage", "max_symbol_margin_usage",
                "max_symbol_exposure", "max_open_positions", "max_trade_risk",
                "max_group_positions", "daily_loss_stop", "slippage_ticks",
            ],
        }
        parameter_tabs = ttk.Notebook(right)
        parameter_tabs.pack(fill=BOTH, expand=True, pady=(7, 4))
        for title, keys in parameter_groups.items():
            tab = ttk.Frame(parameter_tabs, padding=8)
            parameter_tabs.add(tab, text=title)
            for index, key in enumerate(keys):
                row, column = divmod(index, 2)
                ttk.Label(tab, text=labels[key], width=20).grid(
                    row=row, column=column * 2, sticky="w", padx=(0, 4), pady=6
                )
                ttk.Entry(tab, textvariable=self.fields[key], width=16).grid(
                    row=row,
                    column=column * 2 + 1,
                    sticky="ew",
                    padx=(0, 14),
                    pady=6,
                )
            tab.columnconfigure(1, weight=1)
            tab.columnconfigure(3, weight=1)

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(10, 5))
        self.run_button = ttk.Button(actions, text="运行共享账户回测", command=self._start_backtest)
        self.run_button.pack(side=LEFT)
        ttk.Button(actions, text="打开最近报告目录", command=self._open_reports).pack(side=LEFT, padx=8)
        self._update_period_note()

    def _build_data_api(self) -> None:
        introduction = ttk.Frame(self.data_api_tab)
        introduction.pack(fill="x", pady=(0, 8))
        self.data_source_detail = StringVar()
        self.data_source_detail_label = ttk.Label(
            introduction,
            textvariable=self.data_source_detail,
            style="Danger.TLabel",
            wraplength=1260,
            justify=LEFT,
        )
        self.data_source_detail_label.pack(fill="x", anchor="w")

        top = ttk.Frame(self.data_api_tab)
        top.pack(fill="x")
        importer = ttk.LabelFrame(
            top, text="博易 .his → 标准15分钟CSV（只读导入，不操作博易）", padding=10
        )
        importer.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 6))
        api = ttk.LabelFrame(
            top, text="券商API只读就绪检查（无登录、无下单）", padding=10
        )
        api.pack(side=LEFT, fill=BOTH, expand=True, padx=(6, 0))

        self.pobo_his_var = StringVar()
        self.pobo_name_table_var = StringVar()
        self.pobo_output_var = StringVar(value="data/pobo_real_15m/RB0_15m.csv")
        self.pobo_symbol_var = StringVar(value="RB0")
        self._data_api_path_row(
            importer,
            "博易 .his",
            self.pobo_his_var,
            self._choose_pobo_his,
        )
        self._data_api_path_row(
            importer,
            "NameTable",
            self.pobo_name_table_var,
            self._choose_pobo_name_table,
        )
        self._data_api_path_row(
            importer,
            "输出CSV",
            self.pobo_output_var,
            self._choose_pobo_output,
        )
        symbol_row = ttk.Frame(importer)
        symbol_row.pack(fill="x", pady=3)
        ttk.Label(symbol_row, text="品种映射", width=12).pack(side=LEFT)
        ttk.Entry(symbol_row, textvariable=self.pobo_symbol_var).pack(
            side=LEFT, fill="x", expand=True
        )
        ttk.Label(
            importer,
            text="示例：螺纹主力可映射为 RB0。导入会生成来源清单，原 .his 不会被修改。",
            foreground="#526274",
            wraplength=570,
            justify=LEFT,
        ).pack(fill="x", anchor="w", pady=(5, 3))
        import_actions = ttk.Frame(importer)
        import_actions.pack(fill="x", pady=(4, 0))
        self.pobo_import_button = ttk.Button(
            import_actions, text="导入真实行情", command=self._start_pobo_import
        )
        self.pobo_import_button.pack(side=LEFT)
        self.pobo_import_status = StringVar(value="尚未导入")
        ttk.Label(
            import_actions,
            textvariable=self.pobo_import_status,
            wraplength=430,
            justify=LEFT,
        ).pack(side=LEFT, padx=10)

        local_api = self.project_root / "configs" / "api.local.json"
        default_api = (
            local_api
            if local_api.exists()
            else self.project_root / "configs" / "api.changjiang.example.json"
        )
        self.api_config_var = StringVar(value=self._display_path(default_api))
        self._data_api_path_row(
            api, "API配置", self.api_config_var, self._choose_api_config
        )
        api_actions = ttk.Frame(api)
        api_actions.pack(fill="x", pady=3)
        ttk.Button(
            api_actions, text="刷新就绪状态", command=self._refresh_api_readiness
        ).pack(side=LEFT)
        self.live_trading_var = BooleanVar(value=False)
        ttk.Checkbutton(
            api_actions,
            text="真实交易：强制关闭",
            variable=self.live_trading_var,
            state="disabled",
        ).pack(side=RIGHT)
        self.api_status = StringVar(value="尚未检查")
        self.api_status_label = ttk.Label(
            api,
            textvariable=self.api_status,
            style="Danger.TLabel",
            wraplength=570,
            justify=LEFT,
        )
        self.api_status_label.pack(fill="x", anchor="w", pady=(4, 2))
        ttk.Label(
            api,
            text=(
                "安全边界：只显示网关、前置、Broker ID等非敏感字段；"
                "不会显示、读取环境变量值或保存账号/密码/AppID/AuthCode。"
            ),
            foreground="#526274",
            wraplength=570,
            justify=LEFT,
        ).pack(fill="x", anchor="w", pady=(0, 3))
        api_tree_frame = ttk.Frame(api)
        api_tree_frame.pack(fill=BOTH, expand=True)
        self.api_tree = ttk.Treeview(
            api_tree_frame,
            columns=("item", "value"),
            show="headings",
            height=8,
        )
        self.api_tree.heading("item", text="检查项")
        self.api_tree.heading("value", text="非敏感值")
        self.api_tree.column("item", width=130, anchor="w")
        self.api_tree.column("value", width=390, anchor="w")
        api_scroll = ttk.Scrollbar(
            api_tree_frame, orient=VERTICAL, command=self.api_tree.yview
        )
        self.api_tree.configure(yscrollcommand=api_scroll.set)
        self.api_tree.pack(side=LEFT, fill=BOTH, expand=True)
        api_scroll.pack(side=RIGHT, fill="y")
        self.api_problems = StringVar()
        ttk.Label(
            api,
            textvariable=self.api_problems,
            style="Danger.TLabel",
            wraplength=570,
            justify=LEFT,
        ).pack(fill="x", anchor="w", pady=(4, 0))

        coverage = ttk.LabelFrame(
            self.data_api_tab, text="数据目录覆盖范围审计", padding=8
        )
        coverage.pack(fill=BOTH, expand=True, pady=(10, 0))
        coverage_action = ttk.Frame(coverage)
        coverage_action.pack(fill="x")
        self.data_scan_button = ttk.Button(
            coverage_action,
            text="扫描当前数据目录",
            command=self._start_data_scan,
        )
        self.data_scan_button.pack(side=LEFT)
        self.data_scan_status = StringVar(
            value="尚未扫描；“真实”只表示来源，是否满足三年要求请看覆盖列。"
        )
        ttk.Label(
            coverage_action,
            textvariable=self.data_scan_status,
            wraplength=1040,
            justify=LEFT,
        ).pack(side=LEFT, padx=10)
        self.data_coverage_tree = self._tree(coverage)

    def _data_api_path_row(
        self, parent, label: str, variable: StringVar, command
    ) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=12).pack(side=LEFT)
        ttk.Entry(row, textvariable=variable).pack(
            side=LEFT, fill="x", expand=True
        )
        ttk.Button(row, text="…", width=3, command=command).pack(
            side=RIGHT, padx=(4, 0)
        )

    def _build_summary(self) -> None:
        self.summary_heading = StringVar(value="尚未运行回测")
        ttk.Label(
            self.summary_tab,
            textvariable=self.summary_heading,
            style="Metric.TLabel",
            wraplength=1250,
            justify=LEFT,
        ).pack(fill="x", anchor="w", pady=(0, 8))
        frame = ttk.Frame(self.summary_tab)
        frame.pack(fill=BOTH, expand=True)
        self.summary_tree = ttk.Treeview(
            frame,
            columns=("metric", "value"),
            show="headings",
        )
        self.summary_tree.heading("metric", text="指标")
        self.summary_tree.heading("value", text="数值")
        self.summary_tree.column("metric", width=300, minwidth=180, anchor="w")
        self.summary_tree.column("value", width=760, minwidth=240, anchor="w")
        scroll = ttk.Scrollbar(frame, orient=VERTICAL, command=self.summary_tree.yview)
        self.summary_tree.configure(yscrollcommand=scroll.set)
        self.summary_tree.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill="y")

    def _build_curve(self) -> None:
        self.curve_metrics = StringVar(value="尚未运行回测")
        ttk.Label(self.curve_tab, textvariable=self.curve_metrics, style="Metric.TLabel").pack(anchor="w")
        self.curve_canvas = Canvas(self.curve_tab, background="#fbfcfe", highlightthickness=1)
        self.curve_canvas.pack(fill=BOTH, expand=True, pady=(8, 0))
        self.curve_canvas.bind("<Configure>", lambda _event: self._draw_curve())

    def _build_scanner(self) -> None:
        action = ttk.Frame(self.scan_tab)
        action.pack(fill="x")
        self.scan_status = StringVar(value="尚未运行全品种扫描")
        ttk.Label(action, textvariable=self.scan_status, style="Metric.TLabel").pack(
            side=LEFT
        )
        self.scan_cancel_button = ttk.Button(
            action,
            text="停止扫描",
            command=self._cancel_scan,
            state="disabled",
        )
        self.scan_cancel_button.pack(side=RIGHT)
        self.scan_progress = ttk.Progressbar(
            self.scan_tab, mode="determinate", maximum=100
        )
        self.scan_progress.pack(fill="x", pady=(8, 4))
        controls = ttk.Frame(self.scan_tab)
        controls.pack(fill="x", pady=(0, 4))
        ttk.Button(
            controls,
            text="选择排名前5品种作为回测池",
            command=self._select_scan_top_five,
        ).pack(side=LEFT)
        self.scan_tree = self._tree(self.scan_tab)

    def _build_optimization(self) -> None:
        ttk.Label(
            self.optimization_tab,
            text="网格参数（JSON）。候选只用训练/验证期排名，最后 20% 数据封存为样本外测试。",
        ).pack(anchor="w")
        self.grid_text = Text(self.optimization_tab, height=10, font=("Consolas", 10))
        self.grid_text.pack(fill="x", pady=8)
        self.grid_text.insert(
            "1.0",
            json.dumps(
                {
                    "fast_window": [13],
                    "slow_window": [120, 169],
                    "atr_window": [14],
                    "ma_exit_buffer_atr": [0.05, 0.10, 0.20],
                    "partial_exit_fraction": [0.20, 0.30, 0.40],
                    "position_equity_fraction": [0.20, 0.60],
                    "order_size": [0],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        action = ttk.Frame(self.optimization_tab)
        action.pack(fill="x")
        self.optimize_button = ttk.Button(action, text="开始样本外参数优化", command=self._start_optimization)
        self.optimize_button.pack(side=LEFT)
        self.optimization_status = StringVar(value="尚未运行优化")
        ttk.Label(action, textvariable=self.optimization_status).pack(side=LEFT, padx=10)
        self.optimization_tree = self._tree(self.optimization_tab)

    def _path_row(self, parent, label: str, variable: StringVar, command) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=8).pack(side=LEFT)
        ttk.Entry(row, textvariable=variable, width=21).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(row, text="…", width=3, command=command).pack(side=RIGHT, padx=(4, 0))

    def _entry_row(self, parent, label: str, variable: StringVar) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=8).pack(side=LEFT)
        ttk.Entry(row, textvariable=variable).pack(side=LEFT, fill="x", expand=True)

    def _tree(self, parent) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill=BOTH, expand=True, pady=(6, 0))
        tree = ttk.Treeview(frame, show="headings")
        scroll = ttk.Scrollbar(frame, orient=VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill="y")
        return tree

    def _choose_universe(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if path:
            self.universe_var.set(self._display_path(Path(path)))
            self._load_universe()

    def _choose_data_dir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.data_dir_var.set(self._display_path(Path(path)))
            self._update_data_source_badge()
            self._filter_universe(select_defaults=True)
            self._refresh_available_data_controls()

    def _use_real_short_sample_data(self) -> None:
        """Select the imported Pobo cache without asking for credentials."""

        data_dir = self.project_root / "data" / "pobo_real_15m"
        if not data_dir.is_dir() or not any(data_dir.glob("*_15m.csv")):
            messagebox.showerror(
                "未找到真实短样本",
                "本机 data/pobo_real_15m 中没有 *_15m.csv 文件。\n\n"
                "请先在“真实数据与API就绪”页导入博易历史，或选择已有真实行情目录。",
            )
            return
        available = available_instruments_for_data(
            self.universe_instruments, data_dir, "_15m.csv"
        )
        if not available:
            messagebox.showerror(
                "没有可映射的真实短样本",
                "目录中虽然存在 CSV，但没有文件与当前品种池匹配。\n\n"
                "请检查品种代码映射和文件命名，例如 RB0_15m.csv。",
            )
            return
        self.data_dir_var.set(self._display_path(data_dir))
        self.suffix_var.set("_15m.csv")
        self.source_interval_var.set("15分钟")
        self._set_runtime_interval(15)
        self.group_filter_var.set("全部板块")
        self.symbol_search_var.set("")
        self._update_data_source_badge()
        self._filter_universe(select_defaults=True)
        available_count = len(available)
        self._refresh_available_data_controls()
        self.status.set(
            f"已切换到本机真实短样本：{available_count} 个可回测品种，"
            "默认选中前5个；仅约8个月，用于短样本工程测试。"
        )

    def _choose_pobo_his(self) -> None:
        path = filedialog.askopenfilename(
            title="选择博易5分钟历史文件",
            filetypes=[("Pobo history", "*.his"), ("All", "*.*")],
        )
        if not path:
            return
        source = Path(path)
        self.pobo_his_var.set(str(source))
        inferred_name_table = source.parent.parent / "NameTable.xml"
        if inferred_name_table.exists():
            self.pobo_name_table_var.set(str(inferred_name_table))

    def _choose_pobo_name_table(self) -> None:
        path = filedialog.askopenfilename(
            title="选择博易 NameTable.xml",
            filetypes=[("XML", "*.xml"), ("All", "*.*")],
        )
        if path:
            self.pobo_name_table_var.set(path)

    def _choose_pobo_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存标准15分钟CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialdir=self._resolve("data/pobo_real_15m"),
            initialfile=f"{self.pobo_symbol_var.get().strip() or 'RB0'}_15m.csv",
        )
        if path:
            self.pobo_output_var.set(self._display_path(Path(path)))

    def _choose_api_config(self) -> None:
        path = filedialog.askopenfilename(
            title="选择API JSON配置（仅检查非敏感字段）",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if path:
            self.api_config_var.set(self._display_path(Path(path)))
            self._refresh_api_readiness()

    @staticmethod
    def _classification_style(classification: DataSourceClassification) -> str:
        if classification.kind == "real":
            return "RealData.TLabel"
        if classification.kind == "unknown":
            return "WarningData.TLabel"
        return "Danger.TLabel"

    def _update_data_source_badge(self) -> None:
        classification = classify_data_source(
            self._resolve(self.data_dir_var.get())
        )
        style = self._classification_style(classification)
        self.data_source_badge.set(classification.label)
        self.data_source_badge_label.configure(style=style)
        self.data_source_detail.set(
            f"当前数据：{classification.label}｜{classification.detail}"
        )
        self.data_source_detail_label.configure(style=style)

    def _start_data_scan(self) -> None:
        data_dir = self._resolve(self.data_dir_var.get())
        suffix = self.suffix_var.get().strip()
        self.data_scan_button.configure(state="disabled")
        self.quick_data_scan_button.configure(state="disabled")
        self.data_scan_status.set(
            f"正在扫描 {data_dir}；只读取时间与品种列，不会修改行情文件…"
        )
        self.tabs.select(self.data_api_tab)
        threading.Thread(
            target=self._data_scan_job,
            args=(data_dir, suffix),
            daemon=True,
        ).start()

    def _data_scan_job(self, data_dir: Path, suffix: str) -> None:
        try:
            coverage = scan_data_directory(data_dir, suffix)
            self.root.after(
                0, lambda: self._show_data_scan(coverage, data_dir)
            )
        except Exception as exc:
            self.root.after(0, lambda: self._data_scan_failed(exc))

    def _show_data_scan(self, coverage: pd.DataFrame, data_dir: Path) -> None:
        self._fill_tree(self.data_coverage_tree, coverage)
        ok = coverage.loc[coverage["status"].eq("ok")]
        errors = int(coverage["status"].eq("error").sum())
        if ok.empty:
            summary = f"扫描到 {len(coverage)} 个文件，但没有可用K线；错误 {errors} 个。"
        else:
            earliest = min(ok["start"])
            latest = max(ok["end"])
            three_year = int(ok["three_year_check"].eq("约3年或以上").sum())
            min_days = int(ok["calendar_days"].min())
            summary = (
                f"{len(ok)}/{len(coverage)} 个文件可读；总体 {earliest} → {latest}；"
                f"最短覆盖 {min_days} 天；约满三年 {three_year} 个；错误 {errors} 个。"
            )
        self.data_scan_status.set(f"{summary} 目录：{data_dir}")
        self.data_scan_button.configure(state="normal")
        self.quick_data_scan_button.configure(state="normal")
        self._update_data_source_badge()

    def _data_scan_failed(self, exc: Exception) -> None:
        self.data_scan_button.configure(state="normal")
        self.quick_data_scan_button.configure(state="normal")
        self.data_scan_status.set(f"扫描失败：{exc}")
        messagebox.showerror(
            "数据覆盖扫描失败",
            f"无法完成数据目录审计：\n{exc}\n\n"
            "请检查：目录是否存在、文件后缀是否与实际文件一致、CSV是否包含datetime和symbol列。",
        )

    def _start_pobo_import(self) -> None:
        source = Path(self.pobo_his_var.get().strip())
        output_text = self.pobo_output_var.get().strip()
        symbol = self.pobo_symbol_var.get().strip()
        name_table_text = self.pobo_name_table_var.get().strip()
        if not source.exists() or not source.is_file():
            messagebox.showerror("博易导入参数错误", f".his 文件不存在：{source}")
            return
        if source.suffix.lower() != ".his":
            messagebox.showerror("博易导入参数错误", "输入文件必须是 .his 文件。")
            return
        if not output_text:
            messagebox.showerror("博易导入参数错误", "请选择输出CSV路径。")
            return
        output = self._resolve(output_text)
        if output.suffix.lower() != ".csv":
            messagebox.showerror("博易导入参数错误", "输出文件必须使用 .csv 后缀。")
            return
        if not symbol or any(character.isspace() for character in symbol):
            messagebox.showerror(
                "博易导入参数错误", "品种映射不能为空，且不能包含空格。"
            )
            return
        name_table = Path(name_table_text) if name_table_text else None
        if name_table is not None and not name_table.exists():
            messagebox.showerror(
                "博易导入参数错误", f"NameTable.xml 不存在：{name_table}"
            )
            return
        if output.exists() and not messagebox.askyesno(
            "确认覆盖CSV",
            f"输出文件已存在：\n{output}\n\n是否覆盖？原 .his 文件不会被修改。",
        ):
            return
        self.pobo_import_button.configure(state="disabled")
        self.pobo_import_status.set("正在只读解析并聚合15分钟K线…")
        threading.Thread(
            target=self._pobo_import_job,
            args=(source, output, symbol, name_table),
            daemon=True,
        ).start()

    def _pobo_import_job(
        self,
        source: Path,
        output: Path,
        symbol: str,
        name_table: Path | None,
    ) -> None:
        try:
            result = import_pobo_his(
                source,
                output,
                name_table_path=name_table,
                symbol=symbol,
                target_minutes=15,
            )
            source_manifest = output.with_suffix(output.suffix + ".source.json")
            source_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "data_kind": "real_market",
                        "provider": "pobo_local_cache",
                        "source_format": "PoboHis",
                        "source_path": str(source.resolve()),
                        "pb_code": result.instrument.pb_code,
                        "broker_code": result.instrument.br_code,
                        "instrument_name": result.instrument.name,
                        "mapped_symbol": result.symbol,
                        "source_interval_minutes": 5,
                        "output_interval_minutes": 15,
                        "source_bar_count": result.source_bar_count,
                        "output_bar_count": result.output_bar_count,
                        "imported_at": datetime.now().astimezone().isoformat(),
                        "warning": "终端来源已标记；仍需按覆盖范围、换月规则和数据质量判断是否可用于可信回测。",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.root.after(
                0,
                lambda: self._pobo_import_finished(result, output),
            )
        except Exception as exc:
            self.root.after(0, lambda: self._pobo_import_failed(exc))

    def _pobo_import_finished(self, result, output: Path) -> None:
        self.pobo_import_button.configure(state="normal")
        self.pobo_import_status.set(
            f"完成：{result.source_bar_count} 根5分钟 → "
            f"{result.output_bar_count} 根15分钟；{output}"
        )
        self.data_dir_var.set(self._display_path(output.parent))
        symbol = self.pobo_symbol_var.get().strip()
        if output.name.startswith(symbol):
            inferred_suffix = output.name[len(symbol) :]
            if inferred_suffix:
                self.suffix_var.set(inferred_suffix)
        self._update_data_source_badge()
        self._filter_universe(select_defaults=True)
        self._start_data_scan()
        messagebox.showinfo(
            "博易行情导入完成",
            f"已生成标准CSV：\n{output}\n\n"
            "下一步请查看覆盖范围。真实来源不代表已经满足三年可信回测要求。",
        )

    def _pobo_import_failed(self, exc: Exception) -> None:
        self.pobo_import_button.configure(state="normal")
        self.pobo_import_status.set(f"导入失败：{exc}")
        messagebox.showerror(
            "博易行情导入失败",
            f"无法导入 .his：\n{exc}\n\n"
            "请确认文件来自博易5分钟缓存、NameTable.xml与该市场目录匹配，并且缓存下载完整。",
        )

    def _refresh_api_readiness(self) -> None:
        readiness = assess_api_readiness(
            self._resolve(self.api_config_var.get())
        )
        self.live_trading_var.set(False)
        self.api_status.set(readiness.status)
        style = (
            "WarningData.TLabel"
            if readiness.status_code in {"disabled", "mock"}
            else "Danger.TLabel"
        )
        self.api_status_label.configure(style=style)
        self.api_tree.delete(*self.api_tree.get_children())
        for item, value in readiness.safe_fields:
            self.api_tree.insert("", END, values=(item, value))
        self.api_problems.set(
            "｜".join(readiness.problems)
            if readiness.problems
            else "没有结构性缺项；凭证接入仍按要求暂停。"
        )

    def _load_universe(self) -> None:
        try:
            universe = load_universe(self._resolve(self.universe_var.get()))
        except Exception as exc:
            self.status.set(f"品种池读取失败：{exc}")
            return
        self.universe_instruments = universe.instruments
        groups = ["全部板块", *sorted({item.group for item in universe.instruments})]
        self.group_filter.configure(values=groups)
        if self.group_filter_var.get() not in groups:
            self.group_filter_var.set("全部板块")
        self._filter_universe(select_defaults=True)
        self._refresh_available_data_controls()
        selected_count = len(self.symbol_list.curselection())
        self.status.set(
            f"已载入 {len(universe.instruments)} 个品种；"
            f"默认选中 {selected_count} 个已有行情品种"
        )

    def _current_available_instruments(self) -> list[Instrument]:
        return available_instruments_for_data(
            self.universe_instruments,
            self._resolve(self.data_dir_var.get()),
            self.suffix_var.get(),
        )

    def _refresh_available_data_controls(self) -> None:
        available_count = len(self._current_available_instruments())
        total_count = len(self.universe_instruments)
        if total_count:
            self.available_data_status.set(
                f"当前目录可回测：{available_count}/{total_count} 个品种；"
                "一键扫描会自动跳过缺失行情。"
            )
            self.scan_button.configure(
                text=f"一键扫描当前可用 {available_count} 个品种收益"
            )
        else:
            self.available_data_status.set("当前目录可回测：品种池尚未载入")
            self.scan_button.configure(text="一键扫描当前可用品种收益")

    def _filter_universe(self, select_defaults: bool = False) -> None:
        query = self.symbol_search_var.get().strip().lower()
        group = self.group_filter_var.get()
        visible = [
            item
            for item in self.universe_instruments
            if (group == "全部板块" or item.group == group)
            and (not query or query in item.symbol.lower() or query in item.name.lower())
        ]
        self.visible_instruments = visible
        available_symbols = {
            item.symbol for item in self._current_available_instruments()
        }
        self.symbol_list.delete(0, END)
        for instrument in visible:
            availability = "有数据" if instrument.symbol in available_symbols else "缺数据"
            self.symbol_list.insert(
                END,
                f"{instrument.symbol}  {instrument.name}  [{instrument.group}]  {availability}",
            )
        if select_defaults:
            available_indices = [
                index
                for index, instrument in enumerate(visible)
                if instrument.symbol in available_symbols
            ]
            for index in available_indices[:5]:
                self.symbol_list.selection_set(index)

    def _select_available_symbols(self) -> None:
        available_symbols = {
            item.symbol for item in self._current_available_instruments()
        }
        self.symbol_list.selection_clear(0, END)
        selected = 0
        for index, instrument in enumerate(self.visible_instruments):
            if instrument.symbol in available_symbols:
                self.symbol_list.selection_set(index)
                selected += 1
        self.status.set(f"已选中当前筛选范围内 {selected} 个有本机行情的品种")

    def _selected_symbols(self) -> list[str]:
        return [self.symbol_list.get(index).split()[0] for index in self.symbol_list.curselection()]

    def _on_strategy_changed(self) -> None:
        name = STRATEGY_LABELS[self.strategy_var.get()]
        if name == "dual_ma_pullback":
            self.strategy_warning.set(DUAL_MA_STRESS_WARNING)
        elif name == "adaptive_trend_v2":
            self.strategy_warning.set(
                "研究候选：强化版尚未通过真实近三年、多品种、成本压力门槛，"
                "当前默认推荐仍为旧自适应趋势。"
            )
        else:
            self.strategy_warning.set("")
        if name != "dual_ma_pullback":
            safe_risk_profile = {
                "max_margin_usage": "0.60",
                "max_symbol_margin_usage": "0.20",
                "max_symbol_exposure": "2.00",
                "max_trade_risk": "0.005",
            }
            for key, value in safe_risk_profile.items():
                self.fields[key].set(value)
        profiles = {
            "dual_ma_pullback": {
                "fast_window": "13",
                "slow_window": "169",
                "order_size": "0",
                "atr_window": "14",
                "ma_exit_buffer_atr": "0.1",
                "partial_exit_fraction": "0.30",
                "position_equity_fraction": "0.60",
                "max_order_size": "5",
                "max_notional_fraction": "10.0",
                "max_margin_usage": "0.60",
                "max_symbol_margin_usage": "0.60",
                "max_symbol_exposure": "10.0",
                "max_trade_risk": "0.03",
            },
            "dual_period_reversal": {
                "fast_window": "13",
                "slow_window": "45",
                "daily_fast_window": "13",
                "daily_slow_window": "45",
                "order_size": "5",
            },
            "dual_ma": {"fast_window": "5", "slow_window": "20", "order_size": "1"},
            "adaptive_trend": {
                "order_size": "1",
                "entry_window": "55",
                "exit_window": "20",
                "trend_window": "120",
                "momentum_window": "60",
            },
            "adaptive_trend_v2": {
                "order_size": "1",
                "max_order_size": "5",
                "entry_window": "55",
                "exit_window": "20",
                "trend_window": "120",
                "momentum_window": "60",
                "volatility_window": "30",
                "target_annual_volatility": "0.12",
                "atr_window": "20",
                "atr_stop_multiple": "2.5",
                "trailing_atr_multiple": "3.0",
            },
        }
        for key, value in profiles.get(name, {}).items():
            self.fields[key].set(value)
        if name == "dual_ma_pullback":
            self._set_runtime_interval(15)
        if name == "adaptive_trend_v2" and hasattr(self, "grid_text"):
            staged_grid = {
                "stages": [
                    {
                        "name": "structure",
                        "parameter_grid": {
                            "entry_window": [40, 55, 80],
                            "exit_window": [15, 20],
                            "trend_window": [100, 169],
                            "momentum_window": [40, 60],
                        },
                    },
                    {
                        "name": "risk",
                        "parameter_grid": {
                            "volatility_window": [20, 30],
                            "target_annual_volatility": [0.08, 0.12, 0.15],
                            "atr_stop_multiple": [2.0, 2.5, 3.0],
                            "trailing_atr_multiple": [2.5, 3.5],
                        },
                    },
                ]
            }
            self.grid_text.delete("1.0", END)
            self.grid_text.insert(
                "1.0", json.dumps(staged_grid, ensure_ascii=False, indent=2)
            )
        elif name == "dual_ma_pullback" and hasattr(self, "grid_text"):
            grid = {
                "fast_window": [13],
                "slow_window": [120, 169],
                "atr_window": [14],
                "ma_exit_buffer_atr": [0.05, 0.10, 0.20],
                "partial_exit_fraction": [0.20, 0.30, 0.40],
                "position_equity_fraction": [0.20, 0.60],
                "order_size": [0],
            }
            self.grid_text.delete("1.0", END)
            self.grid_text.insert(
                "1.0", json.dumps(grid, ensure_ascii=False, indent=2)
            )

    def _set_runtime_interval(self, minutes: int) -> None:
        label = next(
            (label for label, value in INTERVAL_LABELS.items() if value == minutes),
            None,
        )
        if label is None:
            raise ValueError(f"Unsupported runtime interval: {minutes}")
        self.bar_interval_var.set(label)
        self.fields["annualization_factor"].set(
            str(max(1, round(4032 * 15 / minutes)))
        )

    @staticmethod
    def _interval_minutes(label: str) -> int:
        try:
            return INTERVAL_LABELS[label]
        except KeyError as exc:
            raise ValueError(f"不支持的K线周期：{label}") from exc

    def _update_period_note(self) -> None:
        target = self._interval_minutes(self.bar_interval_var.get())
        strategy_name = STRATEGY_LABELS[self.strategy_var.get()]
        if strategy_name == "dual_ma_pullback":
            self.period_note.set(
                "本策略固定使用15分钟：MA169穿越入场，MA169±0.1ATR全退，"
                "MA13反向穿越按剩余仓位比例分批退出；60%为高风险压力测试上限。"
            )
        else:
            self.period_note.set(
                f"均线、ATR和信号窗口均按 {target} 分钟K线计算；"
                "较大周期信号更少、单次持仓通常更久。"
            )

    def _strategy_config(self) -> StrategyConfig:
        values = self.fields
        return StrategyConfig(
            name=STRATEGY_LABELS[self.strategy_var.get()],
            order_size=int(values["order_size"].get()),
            fast_window=int(values["fast_window"].get()),
            slow_window=int(values["slow_window"].get()),
            daily_fast_window=int(values["daily_fast_window"].get()),
            daily_slow_window=int(values["daily_slow_window"].get()),
            extreme_move_threshold=float(values["extreme_move_threshold"].get()),
            extreme_lookback_days=int(values["extreme_lookback_days"].get()),
            setup_valid_days=int(values["setup_valid_days"].get()),
            atr_window=int(values["atr_window"].get()),
            atr_stop_buffer=float(values["atr_stop_buffer"].get()),
            reward_risk=float(values["reward_risk"].get()),
            trailing_atr_multiple=float(values["trailing_atr_multiple"].get()),
            slope_lookback=int(values["slope_lookback"].get()),
            pullback_lookback=int(values["pullback_lookback"].get()),
            min_pullback_closes=int(values["min_pullback_closes"].get()),
            max_entry_distance_atr=float(values["max_entry_distance_atr"].get()),
            partial_exit_size=int(values["partial_exit_size"].get()),
            break_even_trigger_r=float(values["break_even_trigger_r"].get()),
            ma_exit_buffer_atr=float(values["ma_exit_buffer_atr"].get()),
            cooldown_bars=int(values["cooldown_bars"].get()),
            loss_pause_after=int(values["loss_pause_after"].get()),
            loss_pause_bars=int(values["loss_pause_bars"].get()),
            allow_short=bool(self.allow_short_var.get()),
            entry_window=int(values["entry_window"].get()),
            exit_window=int(values["exit_window"].get()),
            trend_window=int(values["trend_window"].get()),
            momentum_window=int(values["momentum_window"].get()),
            volatility_window=int(values["volatility_window"].get()),
            target_annual_volatility=float(
                values["target_annual_volatility"].get()
            ),
            max_order_size=int(values["max_order_size"].get()),
            max_notional_fraction=float(values["max_notional_fraction"].get()),
            momentum_threshold=float(values["momentum_threshold"].get()),
            annualization_factor=int(values["annualization_factor"].get()),
            macd_fast=int(values["macd_fast"].get()),
            macd_slow=int(values["macd_slow"].get()),
            macd_signal=int(values["macd_signal"].get()),
            divergence_lookback=int(values["divergence_lookback"].get()),
            divergence_pivot_radius=int(
                values["divergence_pivot_radius"].get()
            ),
            divergence_valid_bars=int(values["divergence_valid_bars"].get()),
            second_cross_window=int(values["second_cross_window"].get()),
            atr_stop_multiple=float(values["atr_stop_multiple"].get()),
            partial_exit_fraction=float(values["partial_exit_fraction"].get()),
            position_equity_fraction=float(
                values["position_equity_fraction"].get()
            ),
        )

    def _start_backtest(self) -> None:
        symbols = self._selected_symbols()
        if not symbols:
            messagebox.showwarning("未选择品种", "请至少选择一个可交易品种。")
            return
        data_dir = self._resolve(self.data_dir_var.get())
        suffix = self.suffix_var.get().strip()
        try:
            if not data_dir.exists() or not data_dir.is_dir():
                raise FileNotFoundError(f"数据目录不存在：{data_dir}")
            if not suffix:
                raise ValueError("文件后缀不能为空，例如 _15m.csv。")
            missing_files = [
                str(data_dir / f"{symbol}{suffix}")
                for symbol in symbols
                if not (data_dir / f"{symbol}{suffix}").exists()
            ]
            if missing_files:
                preview = "\n".join(missing_files[:8])
                remainder = len(missing_files) - 8
                if remainder > 0:
                    preview += f"\n…另有 {remainder} 个文件缺失"
                raise FileNotFoundError(
                    f"所选品种缺少行情文件：\n{preview}\n"
                    "请调整数据目录/后缀，或只选择已有数据的品种。"
                )
            source_minutes = self._interval_minutes(self.source_interval_var.get())
            target_minutes = self._interval_minutes(self.bar_interval_var.get())
            validate_intervals(source_minutes, target_minutes)
            strategy = self._strategy_config()
            if strategy.name == "dual_ma_pullback" and target_minutes != 15:
                raise ValueError("MA169穿越/MA13分批策略固定使用15分钟K线。")
            if strategy.fast_window >= strategy.slow_window:
                raise ValueError("短均线周期必须小于长均线周期。")
        except Exception as exc:
            messagebox.showerror(
                "回测启动检查失败",
                f"{exc}\n\n回测尚未启动，也不会产生任何委托。",
            )
            return
        classification = classify_data_source(data_dir)
        research_warnings: list[str] = []
        if classification.kind != "real":
            research_warnings.append(
                f"数据：{classification.label}。{classification.detail}"
            )
        if strategy.name == "dual_ma_pullback":
            research_warnings.append(DUAL_MA_STRESS_WARNING)
        if research_warnings and not messagebox.askyesno(
            "确认仅运行研究回测",
            "\n\n".join(research_warnings)
            + "\n\n是否继续？本操作只回测，不连接API、不下单。",
        ):
            return
        self.run_button.configure(state="disabled")
        self.status.set(
            f"正在读取数据并运行 {target_minutes} 分钟共享账户回测…"
        )
        run_values = {key: variable.get() for key, variable in self.fields.items()}
        symbol_groups = {
            item.symbol: item.group.split("-")[-1]
            for item in self.universe_instruments
            if item.symbol in symbols
        }
        threading.Thread(
            target=self._backtest_job,
            args=(
                symbols,
                source_minutes,
                target_minutes,
                strategy,
                run_values,
                data_dir,
                suffix,
                symbol_groups,
            ),
            daemon=True,
        ).start()

    def _backtest_job(
        self,
        symbols: list[str],
        source_minutes: int,
        target_minutes: int,
        config: StrategyConfig,
        run_values: dict[str, str],
        data_dir: Path,
        suffix: str,
        symbol_groups: dict[str, str],
    ) -> None:
        try:
            specs = load_contract_specs(self.project_root / "configs/contracts.csv")
            missing = sorted(set(symbols) - set(specs))
            if missing:
                raise ValueError(f"合约参数表缺少：{missing}")
            source_bars = {
                symbol: load_bars(data_dir / f"{symbol}{suffix}")
                for symbol in symbols
            }
            bars = {
                symbol: aggregate_bars(
                    symbol_bars,
                    target_minutes=target_minutes,
                    source_minutes=source_minutes,
                )
                for symbol, symbol_bars in source_bars.items()
            }
            initial_cash = float(run_values["initial_cash"])
            risk_cash = initial_cash / math.sqrt(len(symbols))
            strategies = {
                symbol: build_strategy(
                    config,
                    (
                        initial_cash
                        if config.name == "dual_ma_pullback"
                        else risk_cash
                    ),
                    specs[symbol].contract_multiplier,
                    float(run_values["max_symbol_exposure"]),
                    margin_rate=specs[symbol].margin_rate,
                    max_symbol_margin_usage=float(
                        run_values["max_symbol_margin_usage"]
                    ),
                    max_trade_risk=float(run_values["max_trade_risk"]),
                )
                for symbol in symbols
            }
            broker = SharedPortfolioBroker(
                initial_cash=initial_cash,
                contract_specs={symbol: specs[symbol] for symbol in symbols},
                risk_limits=PortfolioRiskLimits(
                    max_margin_usage=float(run_values["max_margin_usage"]),
                    max_symbol_exposure=float(run_values["max_symbol_exposure"]),
                    daily_loss_stop=float(run_values["daily_loss_stop"]),
                    max_symbol_margin_usage=float(
                        run_values["max_symbol_margin_usage"]
                    ),
                    max_open_positions=int(run_values["max_open_positions"]),
                    max_trade_risk=float(run_values["max_trade_risk"]),
                    max_group_positions=int(run_values["max_group_positions"]),
                ),
                slippage_ticks=int(run_values["slippage_ticks"]),
                symbol_groups=symbol_groups,
            )
            result = run_portfolio_backtest(bars, strategies, broker)
            data_classification = classify_data_source(data_dir)
            result.summary.update(
                {
                    "data_source_kind": data_classification.kind,
                    "data_source_label": data_classification.label,
                    "data_directory": str(data_dir),
                    "source_interval_minutes": source_minutes,
                    "bar_interval_minutes": target_minutes,
                    "source_bar_count": sum(map(len, source_bars.values())),
                    "aggregated_bar_count": sum(map(len, bars.values())),
                }
            )
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output = self.project_root / "reports" / "workbench" / stamp
            output.mkdir(parents=True, exist_ok=True)
            run_config = {
                "symbols": symbols,
                "data": {
                    "directory": str(data_dir),
                    "suffix": suffix,
                    "source_kind": data_classification.kind,
                    "source_label": data_classification.label,
                },
                "source_interval_minutes": source_minutes,
                "bar_interval_minutes": target_minutes,
                "strategy": config.__dict__,
                "risk": {
                    key: run_values[key]
                    for key in [
                        "initial_cash", "max_margin_usage",
                        "max_symbol_margin_usage", "max_symbol_exposure",
                        "max_open_positions", "max_trade_risk",
                        "max_group_positions", "daily_loss_stop", "slippage_ticks",
                    ]
                },
            }
            (output / "运行配置.json").write_text(
                json.dumps(run_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            chinese_summary_frame(result.summary).to_csv(
                output / "回测摘要.csv", index=False, encoding="utf-8-sig"
            )
            write_chinese_csv(result.equity_curve, output / "账户权益曲线.csv")
            write_chinese_csv(result.trades, output / "历史成交.csv")
            write_chinese_csv(result.rejections, output / "拒单记录.csv")
            write_chinese_csv(self._pnl_frame(result.trades), output / "历史盈亏.csv")
            write_chinese_csv(self._positions_frame(result.equity_curve), output / "可视化持仓.csv")
            self.root.after(0, lambda: self._show_result(result, output))
        except Exception as exc:
            self.root.after(0, lambda: self._job_error("回测失败", exc))

    def _start_scan_all(self) -> None:
        universe_instruments = list(self.universe_instruments)
        if not universe_instruments:
            messagebox.showwarning("品种池为空", "当前品种池没有可扫描品种。")
            return
        data_dir = self._resolve(self.data_dir_var.get())
        suffix = self.suffix_var.get().strip()
        try:
            if not data_dir.exists() or not data_dir.is_dir():
                raise FileNotFoundError(f"数据目录不存在：{data_dir}")
            if not suffix:
                raise ValueError("文件后缀不能为空，例如 _15m.csv。")
            source_minutes = self._interval_minutes(self.source_interval_var.get())
            target_minutes = self._interval_minutes(self.bar_interval_var.get())
            validate_intervals(source_minutes, target_minutes)
            strategy = self._strategy_config()
            if strategy.name == "dual_ma_pullback" and target_minutes != 15:
                raise ValueError("MA169穿越/MA13分批策略固定使用15分钟K线。")
            if strategy.fast_window >= strategy.slow_window:
                raise ValueError("短均线周期必须小于长均线周期。")
            instruments = available_instruments_for_data(
                universe_instruments, data_dir, suffix
            )
            if not instruments:
                raise FileNotFoundError(
                    "当前数据目录中没有与品种池匹配的本地行情文件。\n"
                    "请点击“使用本机真实短样本（约8个月）”，或检查数据目录和文件后缀。"
                )
        except Exception as exc:
            messagebox.showerror("全品种扫描启动失败", str(exc))
            return
        available_symbols = {instrument.symbol for instrument in instruments}
        skipped_symbols = [
            instrument.symbol
            for instrument in universe_instruments
            if instrument.symbol not in available_symbols
        ]
        classification = classify_data_source(data_dir)
        research_warnings: list[str] = []
        if classification.kind != "real":
            research_warnings.append(
                f"数据：{classification.label}。{classification.detail}"
            )
        if strategy.name == "dual_ma_pullback":
            research_warnings.append(DUAL_MA_STRESS_WARNING)
        if research_warnings and not messagebox.askyesno(
            "确认仅运行研究扫描",
            "\n\n".join(research_warnings)
            + "\n\n是否继续？结果不会被标记为真实可信收益。",
        ):
            return

        run_values = {key: variable.get() for key, variable in self.fields.items()}
        self.scan_cancel_event.clear()
        self.scan_button.configure(state="disabled")
        self.scan_cancel_button.configure(state="normal")
        self.scan_progress.configure(value=0, maximum=len(instruments))
        skipped_note = (
            f" · 已跳过 {len(skipped_symbols)} 个缺失行情品种"
            if skipped_symbols
            else ""
        )
        self.scan_status.set(
            f"准备扫描 {len(instruments)} 个品种 · {target_minutes}分钟 · "
            f"{classification.label}{skipped_note}"
        )
        self.status.set(
            f"正在运行 {len(instruments)} 个可用行情品种的独立收益扫描…{skipped_note}"
        )
        self.tabs.select(self.scan_tab)
        threading.Thread(
            target=self._scan_all_job,
            args=(
                instruments,
                source_minutes,
                target_minutes,
                strategy,
                run_values,
                data_dir,
                suffix,
                skipped_symbols,
            ),
            daemon=True,
        ).start()

    def _scan_all_job(
        self,
        instruments,
        source_minutes: int,
        target_minutes: int,
        strategy: StrategyConfig,
        run_values: dict[str, str],
        data_dir: Path,
        suffix: str,
        skipped_symbols: list[str],
    ) -> None:
        try:
            specs = load_contract_specs(self.project_root / "configs/contracts.csv")
            risk_limits = PortfolioRiskLimits(
                max_margin_usage=float(run_values["max_margin_usage"]),
                max_symbol_exposure=float(run_values["max_symbol_exposure"]),
                daily_loss_stop=float(run_values["daily_loss_stop"]),
                max_symbol_margin_usage=float(
                    run_values["max_symbol_margin_usage"]
                ),
                max_open_positions=int(run_values["max_open_positions"]),
                max_trade_risk=float(run_values["max_trade_risk"]),
                max_group_positions=int(run_values["max_group_positions"]),
            )

            def progress(index: int, total: int, symbol: str) -> None:
                self.root.after(
                    0,
                    lambda i=index, t=total, s=symbol: self._update_scan_progress(
                        i, t, s
                    ),
                )

            ranking = scan_instruments(
                instruments,
                data_dir=data_dir,
                suffix=suffix,
                source_interval_minutes=source_minutes,
                bar_interval_minutes=target_minutes,
                strategy_config=strategy,
                initial_cash=float(run_values["initial_cash"]),
                max_symbol_exposure=float(run_values["max_symbol_exposure"]),
                risk_limits=risk_limits,
                slippage_ticks=int(run_values["slippage_ticks"]),
                contract_specs=specs,
                progress=progress,
                cancelled=self.scan_cancel_event.is_set,
            )
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output = self.project_root / "reports" / "full_universe_scan" / stamp
            output.mkdir(parents=True, exist_ok=True)
            ranking.to_csv(
                output / "scan_results.csv", index=False, encoding="utf-8-sig"
            )
            write_chinese_csv(ranking, output / "全品种收益排名.csv")
            scan_config = {
                "data": {
                    "directory": str(data_dir),
                    "suffix": suffix,
                    "source_kind": classify_data_source(data_dir).kind,
                    "source_label": classify_data_source(data_dir).label,
                },
                "source_interval_minutes": source_minutes,
                "bar_interval_minutes": target_minutes,
                "universe_instrument_count": len(instruments) + len(skipped_symbols),
                "available_instrument_count": len(instruments),
                "skipped_missing_data_symbols": skipped_symbols,
                "strategy": strategy.__dict__,
                "risk": run_values,
            }
            (output / "扫描配置.json").write_text(
                json.dumps(scan_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            was_cancelled = self.scan_cancel_event.is_set()
            self.root.after(
                0,
                lambda: self._show_scan_results(
                    ranking,
                    output,
                    was_cancelled,
                    classify_data_source(data_dir).label,
                    len(skipped_symbols),
                ),
            )
        except Exception as exc:
            self.root.after(0, lambda: self._scan_failed(exc))

    def _update_scan_progress(self, index: int, total: int, symbol: str) -> None:
        self.scan_progress.configure(value=index, maximum=total)
        self.scan_status.set(f"扫描中 {index}/{total} · 当前 {symbol}")

    def _cancel_scan(self) -> None:
        self.scan_cancel_event.set()
        self.scan_cancel_button.configure(state="disabled")
        self.scan_status.set("正在停止扫描；当前品种结束后保存已有结果…")

    def _show_scan_results(
        self,
        ranking: pd.DataFrame,
        output: Path,
        was_cancelled: bool,
        data_label: str,
        skipped_missing_count: int = 0,
    ) -> None:
        self.scan_results = ranking
        display = ranking.copy()
        for column in [
            "total_return",
            "annualized_return",
            "max_drawdown",
            "max_margin_usage_observed",
        ]:
            if column in display:
                display[column] = display[column].map(
                    lambda value: "—" if pd.isna(value) else f"{float(value):.2%}"
                )
        for column in ["sharpe", "calmar"]:
            if column in display:
                display[column] = display[column].map(
                    lambda value: "—" if pd.isna(value) else f"{float(value):.3f}"
                )
        if "final_equity" in display:
            display["final_equity"] = display["final_equity"].map(
                lambda value: "—" if pd.isna(value) else f"{float(value):,.2f}"
            )
        columns = [
            "rank", "symbol", "name", "group", "total_return",
            "annualized_return", "max_drawdown", "sharpe", "calmar",
            "trade_count", "rejected_order_count",
            "max_margin_usage_observed", "final_equity", "status", "error",
        ]
        self._fill_tree(
            self.scan_tree,
            display[[column for column in columns if column in display]],
        )
        successful = int(ranking["status"].eq("ok").sum()) if not ranking.empty else 0
        failed = len(ranking) - successful
        prefix = "扫描已停止" if was_cancelled else "扫描完成"
        skipped_note = (
            f" · 已跳过缺失行情 {skipped_missing_count}"
            if skipped_missing_count
            else ""
        )
        self.scan_status.set(
            f"{prefix} · {data_label} · 成功 {successful} · 失败 {failed}"
            f"{skipped_note} · 结果：{output}"
        )
        self.scan_progress.configure(value=len(ranking))
        self.scan_button.configure(state="normal")
        self.scan_cancel_button.configure(state="disabled")
        self.status.set(f"全品种扫描结果：{output}")
        self.tabs.select(self.scan_tab)

    def _scan_failed(self, exc: Exception) -> None:
        self.scan_button.configure(state="normal")
        self.scan_cancel_button.configure(state="disabled")
        self.scan_status.set(f"扫描失败：{exc}")
        self.status.set(str(exc))
        messagebox.showerror(
            "全品种扫描失败",
            f"{exc}\n\n已完成的品种不会被当作完整全市场结果。"
            "请先在“真实数据与API就绪”页检查目录、后缀和覆盖范围。",
        )

    def _select_scan_top_five(self) -> None:
        if self.scan_results.empty:
            messagebox.showinfo("没有扫描结果", "请先运行全品种扫描。")
            return
        symbols = self.scan_results.loc[
            self.scan_results["status"].eq("ok"), "symbol"
        ].head(5).tolist()
        self.group_filter_var.set("全部板块")
        self.symbol_search_var.set("")
        self._filter_universe()
        self.symbol_list.selection_clear(0, END)
        selected = set(symbols)
        for index in range(self.symbol_list.size()):
            if self.symbol_list.get(index).split()[0] in selected:
                self.symbol_list.selection_set(index)
        self.status.set(f"已选择扫描排名前 {len(symbols)} 个品种：{','.join(symbols)}")
        self.tabs.select(self.settings_tab)

    def _show_result(self, result, output: Path) -> None:
        self.result = result
        summary = result.summary
        data_label = str(summary.get("data_source_label", "来源未记录"))
        self.summary_heading.set(
            f"{data_label}｜完整回测摘要    结果目录：{output}"
        )
        self.summary_tree.delete(*self.summary_tree.get_children())
        for key, value in summary.items():
            self.summary_tree.insert(
                "",
                END,
                values=(chinese_column_name(key), self._format_summary_value(key, value)),
            )
        self.curve_metrics.set(
            f"总收益 {summary.get('total_return', 0):.2%}   最大回撤 {summary.get('max_drawdown', 0):.2%}   "
            f"Sharpe {summary.get('sharpe', 0):.3f}   结果目录 {output}"
        )
        self._fill_tree(self.trades_tree, result.trades)
        self._fill_tree(self.pnl_tree, self._pnl_frame(result.trades))
        positions = self._positions_frame(result.equity_curve)
        self._fill_tree(self.positions_tree, positions)
        self._draw_curve()
        self.run_button.configure(state="normal")
        self.status.set(f"回测完成：{output}")
        self.tabs.select(self.summary_tab)

    @staticmethod
    def _format_summary_value(key: str, value: object) -> str:
        if value is None:
            return "—"
        if key in {
            "total_return",
            "annualized_return",
            "annualized_volatility",
            "max_drawdown",
            "max_margin_usage_observed",
            "gross_win_rate",
        }:
            return f"{float(value):.2%}"
        if key in {
            "initial_cash",
            "final_cash",
            "final_equity",
            "commission_total",
            "slippage_cost_total",
            "realized_pnl_before_commission",
        }:
            return f"{float(value):,.2f}"
        if isinstance(value, float):
            return f"{value:,.4f}"
        return str(chinese_value(value))

    def _positions_frame(self, curve: pd.DataFrame) -> pd.DataFrame:
        if curve.empty or "positions" not in curve.columns:
            return pd.DataFrame(columns=["symbol", "position", "status"])
        positions = json.loads(str(curve.iloc[-1]["positions"]))
        if not positions:
            return pd.DataFrame([{"symbol": "—", "position": 0, "status": "回测结束已按规则平仓"}])
        return pd.DataFrame(
            [{"symbol": symbol, "position": quantity, "status": "持仓"} for symbol, quantity in positions.items()]
        )

    def _pnl_frame(self, trades: pd.DataFrame) -> pd.DataFrame:
        pnl = trades.copy()
        if not pnl.empty:
            pnl = pnl[pnl["realized_pnl"].astype(float) != 0].copy()
            pnl["net_realized_pnl"] = (
                pnl["realized_pnl"].astype(float) - pnl["commission"].astype(float)
            )
            wanted = [
                "datetime",
                "symbol",
                "quantity",
                "price",
                "realized_pnl",
                "commission",
                "net_realized_pnl",
                "reason",
            ]
            pnl = pnl[[column for column in wanted if column in pnl.columns]]
        return pnl

    def _fill_tree(self, tree: ttk.Treeview, frame: pd.DataFrame) -> None:
        tree.delete(*tree.get_children())
        columns = [str(column) for column in frame.columns]
        tree.configure(columns=columns)
        for column in columns:
            heading = chinese_column_name(column)
            tree.heading(column, text=heading)
            tree.column(column, width=min(max(95, len(heading) * 18), 360), anchor="center")
        for row in frame.head(5000).itertuples(index=False, name=None):
            values = [self._format_cell(value) for value in row]
            tree.insert("", END, values=values)

    def _draw_curve(self) -> None:
        canvas = self.curve_canvas
        canvas.delete("all")
        if self.result is None or self.result.equity_curve.empty:
            canvas.create_text(400, 250, text="运行回测后显示总收益曲线", fill="#687386")
            return
        values = self.result.equity_curve["equity"].astype(float).tolist()
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 200)
        pad = 52
        plot_w, plot_h = width - 2 * pad, height - 2 * pad
        low, high = min(values), max(values)
        span = max(high - low, 1e-9)
        if len(values) > plot_w:
            step = max(1, len(values) // int(plot_w))
            values = values[::step]
        points: list[float] = []
        for index, value in enumerate(values):
            x = pad + index / max(len(values) - 1, 1) * plot_w
            y = pad + (high - value) / span * plot_h
            points.extend([x, y])
        canvas.create_line(pad, pad, pad, height - pad, fill="#aab4c3")
        canvas.create_line(pad, height - pad, width - pad, height - pad, fill="#aab4c3")
        if len(points) >= 4:
            canvas.create_line(*points, fill="#087f5b", width=2, smooth=True)
        canvas.create_text(pad - 5, pad, text=f"{high:,.0f}", anchor="e", fill="#46556a")
        canvas.create_text(pad - 5, height - pad, text=f"{low:,.0f}", anchor="e", fill="#46556a")
        canvas.create_text(width / 2, 18, text="共享账户权益曲线（已扣手续费与滑点）", fill="#26364a")

    def _start_optimization(self) -> None:
        symbols = self._selected_symbols()
        if not symbols:
            messagebox.showwarning("未选择品种", "请先在回测页选择优化品种。")
            return
        data_dir = self._resolve(self.data_dir_var.get())
        suffix = self.suffix_var.get().strip()
        try:
            if not data_dir.exists() or not data_dir.is_dir():
                raise FileNotFoundError(f"数据目录不存在：{data_dir}")
            if not suffix:
                raise ValueError("文件后缀不能为空，例如 _15m.csv。")
            missing_files = [
                str(data_dir / f"{symbol}{suffix}")
                for symbol in symbols
                if not (data_dir / f"{symbol}{suffix}").is_file()
            ]
            if missing_files:
                preview = "\n".join(missing_files[:8])
                remainder = len(missing_files) - 8
                if remainder > 0:
                    preview += f"\n…另有 {remainder} 个文件缺失"
                raise FileNotFoundError(
                    f"所选优化品种缺少行情文件：\n{preview}\n"
                    "请点击“全选有数据”，或调整数据目录和文件后缀。"
                )
            grid = json.loads(self.grid_text.get("1.0", END))
            if not isinstance(grid, dict) or not grid:
                raise ValueError("参数网格必须是非空 JSON 对象")
            source_minutes = self._interval_minutes(self.source_interval_var.get())
            target_minutes = self._interval_minutes(self.bar_interval_var.get())
            validate_intervals(source_minutes, target_minutes)
            strategy_config = self._strategy_config()
            if (
                strategy_config.name == "dual_ma_pullback"
                and target_minutes != 15
            ):
                raise ValueError("MA169穿越/MA13分批策略固定使用15分钟K线。")
        except Exception as exc:
            messagebox.showerror("参数网格错误", str(exc))
            return
        self.optimize_button.configure(state="disabled")
        self.optimization_status.set("优化运行中；候选较多时需要较长时间…")
        run_values = {key: variable.get() for key, variable in self.fields.items()}
        threading.Thread(
            target=self._optimization_job,
            args=(
                symbols,
                grid,
                strategy_config,
                run_values,
                source_minutes,
                target_minutes,
                self._resolve(self.universe_var.get()),
                data_dir,
                suffix,
            ),
            daemon=True,
        ).start()

    def _optimization_job(
        self,
        symbols: list[str],
        grid: dict[str, list[object]],
        strategy_config: StrategyConfig,
        run_values: dict[str, str],
        source_minutes: int,
        target_minutes: int,
        source_universe_path: Path,
        data_dir: Path,
        suffix: str,
    ) -> None:
        try:
            output = self.project_root / "reports" / "workbench_optimization"
            output.mkdir(parents=True, exist_ok=True)
            universe = load_universe(source_universe_path)
            selected = [item for item in universe.instruments if item.symbol in symbols]
            universe_path = output / "selected_universe.json"
            universe_path.write_text(
                json.dumps(
                    {
                        "start": universe.start,
                        "end": universe.end,
                        "instruments": [item.__dict__ for item in selected],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            strategy = {
                "name": strategy_config.name,
                **strategy_parameters(strategy_config),
            }
            baseline_strategy = strategy
            if strategy_config.name == "adaptive_trend_v2":
                baseline_strategy = {
                    "name": "adaptive_trend",
                    "entry_window": 55,
                    "exit_window": 20,
                    "trend_window": 120,
                    "momentum_window": 60,
                    "volatility_window": 30,
                    "target_annual_volatility": 0.12,
                    "order_size": 1,
                    "max_order_size": 5,
                    "max_notional_fraction": 0.10,
                    "momentum_threshold": 0.0,
                    "allow_short": True,
                    "annualization_factor": int(
                        run_values["annualization_factor"]
                    ),
                }
            config = {
                "initial_cash": float(run_values["initial_cash"]),
                "commission_rate": 0.00012,
                "slippage_ticks": int(run_values["slippage_ticks"]),
                "tick_size": 1.0,
                "contract_multiplier": 10,
                "margin_rate": 0.12,
                "max_margin_usage": float(run_values["max_margin_usage"]),
                "max_symbol_exposure": float(run_values["max_symbol_exposure"]),
                "max_symbol_margin_usage": float(run_values["max_symbol_margin_usage"]),
                "max_open_positions": int(run_values["max_open_positions"]),
                "max_trade_risk": float(run_values["max_trade_risk"]),
                "max_group_positions": int(run_values["max_group_positions"]),
                "daily_loss_stop": float(run_values["daily_loss_stop"]),
                "strategy": baseline_strategy,
                "data": {
                    "symbol": symbols[0],
                    "path": "unused.csv",
                    "source_interval_minutes": source_minutes,
                    "bar_interval_minutes": target_minutes,
                },
                "contracts": {"path": "configs/contracts.csv"},
                "report": {"path": "reports/unused.csv"},
            }
            config_path = output / "base_config.json"
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            staged = strategy_config.name == "adaptive_trend_v2" and "stages" in grid
            strategy_search = (
                {
                    "name": strategy["name"],
                    "fixed_parameters": strategy_parameters(strategy_config),
                    "stages": grid["stages"],
                }
                if staged
                else {"name": strategy["name"], "parameter_grid": grid}
            )
            objective = (
                {
                    "metric": "enhanced_robust_score",
                    "selection_method": "min_train_validation",
                    "min_trades": 10,
                    "min_trades_per_instrument": 3,
                    "min_median_instrument_return": 0.0,
                    "min_positive_instrument_ratio": 0.60,
                    "max_rejection_rate": 0.10,
                }
                if staged
                else {
                    "metric": "robust_score",
                    "selection_method": "min_train_validation",
                    "min_trades": 2,
                    "drawdown_penalty": 0.5,
                }
            )
            optimization = {
                "strategy": strategy_search,
                "split": {
                    "train_fraction": 0.6,
                    "validation_fraction": 0.2,
                    "min_bars_per_phase": 250 if target_minutes >= 60 else 1000,
                },
                "objective": objective,
                "sensitivity": {
                    "commission_multipliers": [1.5, 2.0] if staged else [1.0, 1.5, 2.0],
                    "slippage_ticks": [2, 3] if staged else [1, 2, 3],
                },
                "comparisons": (
                    [
                        {
                            "name": "dual_ma_pullback",
                            "parameters": {
                                "fast_window": 13,
                                "slow_window": 169,
                                "order_size": 5,
                                "allow_short": True,
                            },
                        }
                    ]
                    if staged
                    else []
                ),
                "research_data_label": (
                    "synthetic_engineering"
                    if "domestic_15m" in str(data_dir).lower()
                    else "user_supplied_or_unspecified"
                ),
            }
            optimization_path = output / "optimization_request.json"
            optimization_path.write_text(json.dumps(optimization, indent=2), encoding="utf-8")
            paths = optimize_strategy(
                config_path,
                optimization_path,
                universe_path,
                data_dir,
                output,
                suffix=suffix,
                project_root=self.project_root,
            )
            for path in paths.values():
                if path.suffix.lower() == ".csv" and path.exists():
                    write_chinese_companion(pd.read_csv(path), path)
            ranking = pd.read_csv(paths["candidate_ranking"])
            self.root.after(0, lambda: self._show_optimization(ranking, paths))
        except Exception as exc:
            self.root.after(0, lambda: self._job_error("参数优化失败", exc, optimization=True))

    def _show_optimization(self, ranking: pd.DataFrame, paths: dict[str, Path]) -> None:
        columns = [
            column
            for column in ["rank", "selection_score", "train_score", "validation_score", "parameters", "status", "error"]
            if column in ranking.columns
        ]
        self._fill_tree(self.optimization_tree, ranking[columns].head(200))
        self.optimize_button.configure(state="normal")
        self.optimization_status.set(f"优化完成；最优参数与样本外报告：{paths['optimization_report']}")
        if "strategy_comparison" in paths:
            comparison = pd.read_csv(paths["strategy_comparison"])
            for column in [
                "total_return", "annualized_return", "max_drawdown",
                "median_instrument_return", "positive_instrument_ratio",
                "rejection_rate",
            ]:
                if column in comparison:
                    comparison[column] = comparison[column].map(
                        lambda value: "—" if pd.isna(value) else f"{float(value):.2%}"
                    )
            wanted = [
                "scenario", "strategy_name", "total_return",
                "annualized_return", "max_drawdown", "sharpe", "calmar",
                "median_instrument_return", "positive_instrument_ratio",
                "trade_count", "rejected_order_count", "rejection_rate",
            ]
            self._fill_tree(
                self.comparison_tree,
                comparison[[column for column in wanted if column in comparison]],
            )
            self.comparison_status.set(
                f"封存样本外策略对比 · 不参与选参 · {paths['strategy_comparison']}"
            )

    def _job_error(self, title: str, exc: Exception, optimization: bool = False) -> None:
        self.run_button.configure(state="normal")
        self.optimize_button.configure(state="normal")
        guidance = (
            "请检查数据目录/文件后缀/所选品种、合约参数和K线周期。"
            "本次任务已停止，没有连接API，也没有产生真实委托。"
        )
        if optimization:
            self.optimization_status.set(f"{title}：{exc}")
        self.status.set(f"{title}：{exc}")
        messagebox.showerror(title, f"{exc}\n\n{guidance}")

    def _open_reports(self) -> None:
        path = self.project_root / "reports"
        path.mkdir(exist_ok=True)
        __import__("os").startfile(path)

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.project_root))
        except ValueError:
            return str(path)

    @staticmethod
    def _format_cell(value: object) -> str:
        if isinstance(value, float):
            return f"{value:.6f}"
        return str(chinese_value(value))


def launch_dashboard(project_root: str | Path | None = None) -> None:
    root = Tk()
    QuantWorkbench(root, Path(project_root or Path.cwd()))
    root.mainloop()


if __name__ == "__main__":
    launch_dashboard()
