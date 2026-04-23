"""akshare-stock 共享 helpers — 中文 A 股 / 港股 / 美股数据获取封装。

## 典型用法

```python
from _akshare_helpers import *   # bash 工具已注入 PYTHONPATH

# 实时行情
df = get_stock_realtime("000001")                 # 平安银行
print(df[["名称", "最新价", "涨跌幅"]])

# K 线历史
df = get_stock_history("000001", "2024-01-01", "2025-01-01", freq="daily")
save_as(df, "workspace/pa_2024.csv")

# 财报摘要（给 LLM 用的 markdown）
md = summarize_financials("000001")
open("workspace/pa_financial.md", "w").write(md)

# 板块资金流
df = get_sector_flow(period="today")
df.head(10).to_json("workspace/sector_top10.json", orient="records", force_ascii=False)
```

## 可用函数

基础:
    get_stock_realtime(symbol)              实时行情（单股）
    get_stock_history(symbol, start, end, freq="daily")  K 线
    get_stock_info(symbol)                  基本信息 + 财务概览
    get_financial_report(symbol, year=None) 三表 (资产/利润/现金流)

市场/板块:
    get_sector_quote()                      全市场板块排行
    get_sector_flow(period="today")         板块资金流
    get_index_history(index, start, end)    指数历史
    get_top_list(date=None)                 龙虎榜

交易/融资:
    get_margin_detail(symbol=None)          两融数据
    screen_stocks(filters)                  条件筛选

工具:
    save_as(df, path)                       自动判断扩展名保存
    summarize_financials(symbol)            财报 → markdown 摘要

## 合规声明

**本 skill 返回的数据仅供学术研究和学习，不构成任何投资建议。**
**所有市场操作的盈亏由用户自行承担。**
Agent 在生成报告 / 回答时必须在显著位置保留这条免责声明。

## 错误处理原则

- AkShare 是爬虫性质，目标网站变化会导致间歇失败 → 每个函数内置 1 次静默重试
- 连续失败 → 抛 `AkShareError`，agent 应停下报告用户，不无限重试
- `symbol` 标准: A 股纯 6 位（"000001"），不加交易所前缀；港股 5 位（"00700"）
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

__all__ = [
    # data fetch
    "get_stock_realtime",
    "get_stock_history",
    "get_stock_info",
    "get_financial_report",
    "get_sector_quote",
    "get_sector_flow",
    "get_index_history",
    "get_top_list",
    "get_margin_detail",
    "screen_stocks",
    # utils
    "save_as",
    "summarize_financials",
    # charts (output PNG; chat renders inline via FileCard)
    "plot_kline",
    "plot_price_ma",
    "plot_volume",
    "plot_sector_heatmap",
    # analysis template
    "build_analysis_report",
    # error type
    "AkShareError",
]


class AkShareError(RuntimeError):
    """Raised when akshare call fails after retry. Agent should stop and
    report to user rather than burn retries."""


# ── lazy import ────────────────────────────────────────────────────
# akshare 首次 import 较重（~1.5s），每次脚本只 import 一次

_ak = None
_pd = None


def _ak_module():
    global _ak, _pd
    if _ak is not None:
        return _ak
    try:
        import akshare as ak
        import pandas as pd
    except ImportError as e:
        raise AkShareError(
            f"akshare 未安装 (pip install akshare pandas): {e}"
        ) from e
    _ak = ak
    _pd = pd
    return _ak


def _pd_module():
    if _pd is None:
        _ak_module()
    return _pd


def _with_retry(fn, *args, retries: int = 2, backoff_s: float = 1.5, **kwargs):
    """Call `fn(*args, **kwargs)` with silent retry. Raises AkShareError on final failure.
    Default 2 retries (total 3 attempts) with exponential backoff; the full-market
    snapshot endpoints are the most flake-prone (large payload, remote connection
    resets) so giving them multiple tries significantly improves success rate."""
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:   # noqa: BLE001 — akshare raises varied types
            last = e
            if attempt < retries:
                time.sleep(backoff_s * (attempt + 1))
                continue
    raise AkShareError(
        f"{getattr(fn, '__name__', 'akshare_call')} 连续 {retries + 1} "
        f"次失败: {last}"
    ) from last


# ── data fetchers ───────────────────────────────────────────────────


def get_stock_realtime(symbol: str):
    """单股实时行情. 返回 DataFrame (1 行)."""
    ak = _ak_module()

    def _impl():
        # A 股实时（新浪源）
        df = ak.stock_zh_a_spot_em()  # 全市场快照
        if df is None or df.empty:
            raise RuntimeError("实时快照为空")
        hit = df[df["代码"] == str(symbol)]
        if hit.empty:
            raise RuntimeError(f"未找到 {symbol}（A 股 6 位代码？）")
        return hit.reset_index(drop=True)

    return _with_retry(_impl)


def get_stock_history(symbol: str, start: str, end: str,
                      freq: str = "daily", adjust: str = "qfq"):
    """历史 K 线. freq: daily | weekly | monthly | 60 (60 分钟) | 5 | 15 | 30.
    adjust: qfq (前复权) | hfq (后复权) | '' (不复权)."""
    ak = _ak_module()
    period_map = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}
    period = period_map.get(freq, freq)  # 分钟线直接用字符串
    start_d = str(start).replace("-", "")
    end_d = str(end).replace("-", "")
    return _with_retry(
        ak.stock_zh_a_hist,
        symbol=str(symbol), period=period,
        start_date=start_d, end_date=end_d, adjust=adjust,
    )


def get_stock_info(symbol: str) -> dict:
    """股票基本信息（公司名/行业/上市日期/总股本/流通股本/...）."""
    ak = _ak_module()

    def _impl():
        df = ak.stock_individual_info_em(symbol=str(symbol))
        if df is None or df.empty:
            raise RuntimeError(f"未找到 {symbol} 基本信息")
        # akshare 返回 [{item, value}, ...] 格式，转成 dict
        return {str(r["item"]): r["value"] for _, r in df.iterrows()}

    return _with_retry(_impl)


def get_financial_report(symbol: str, year: Optional[int] = None) -> dict:
    """返回 {balance_sheet, income_statement, cash_flow} 三张表 (DataFrame).
    year 为 None 取全部历史；传具体年份（如 2024）仅返回该年报."""
    ak = _ak_module()

    def _impl():
        out = {}
        # 资产负债表
        bs = ak.stock_balance_sheet_by_report_em(symbol=str(symbol))
        # 利润表
        is_ = ak.stock_profit_sheet_by_report_em(symbol=str(symbol))
        # 现金流量表
        cf = ak.stock_cash_flow_sheet_by_report_em(symbol=str(symbol))
        if year is not None:
            yr = str(year)
            for name, df in (("bs", bs), ("is", is_), ("cf", cf)):
                if df is not None and "REPORT_DATE" in df.columns:
                    df = df[df["REPORT_DATE"].astype(str).str.startswith(yr)]
            out = {"balance_sheet": bs, "income_statement": is_, "cash_flow": cf}
        else:
            out = {"balance_sheet": bs, "income_statement": is_, "cash_flow": cf}
        return out

    return _with_retry(_impl)


def get_sector_quote():
    """全市场板块（行业板块）实时排行."""
    ak = _ak_module()
    return _with_retry(ak.stock_board_industry_name_em)


def get_sector_flow(period: str = "today"):
    """板块资金流. period: today | 3day | 5day | 10day."""
    ak = _ak_module()
    period_map = {
        "today": "今日",
        "3day": "3日",
        "5day": "5日",
        "10day": "10日",
    }
    ak_period = period_map.get(period, period)
    return _with_retry(ak.stock_sector_fund_flow_rank, indicator=ak_period, sector_type="行业资金流")


def get_index_history(index: str, start: str, end: str):
    """指数历史 K 线. index 用代码: sh000001 (上证), sz399001 (深证), sh000300 (沪深300), sh000905 (中证500)."""
    ak = _ak_module()
    start_d = str(start).replace("-", "")
    end_d = str(end).replace("-", "")
    return _with_retry(
        ak.stock_zh_index_daily_em,
        symbol=str(index),
        start_date=start_d, end_date=end_d,
    )


def get_top_list(date: Optional[str] = None):
    """龙虎榜. date 格式 YYYYMMDD，None 则取最近一个交易日."""
    ak = _ak_module()
    if date is None:
        from datetime import datetime, timedelta
        # Best-effort: 往前找 5 天内第一个非空结果
        for i in range(5):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            try:
                df = _with_retry(ak.stock_lhb_detail_em, start_date=d, end_date=d)
                if df is not None and not df.empty:
                    return df
            except AkShareError:
                continue
        raise AkShareError("最近 5 天龙虎榜均无数据")
    return _with_retry(
        ak.stock_lhb_detail_em,
        start_date=str(date), end_date=str(date),
    )


def get_margin_detail(symbol: Optional[str] = None):
    """两融明细. symbol=None 返回全市场汇总，指定则返回该股两融走势."""
    ak = _ak_module()
    if symbol is None:
        return _with_retry(ak.stock_margin_underlying_info_szse, date=None)
    return _with_retry(ak.stock_margin_detail_szse, date=None)


def screen_stocks(filters: dict) -> Any:
    """简单条件筛选. filters 示例: {"pe_max": 20, "pb_max": 2, "pe_min": 5}.

    基于 get_stock_realtime 的全市场快照做过滤，不是后端查询；股票池小的
    场景够用，全市场 5000+ 股票约 2-3 秒。
    """
    ak = _ak_module()
    pd = _pd_module()
    df = _with_retry(ak.stock_zh_a_spot_em)
    if df is None or df.empty:
        raise AkShareError("行情快照为空，筛选中止")
    out = df.copy()
    # 列名映射（akshare 返回中文）
    col = {"pe": "市盈率-动态", "pb": "市净率",
           "mktcap": "总市值", "price": "最新价"}
    for field, op_suffix in (("pe", "_max"), ("pb", "_max"),
                             ("mktcap", "_min"), ("price", "_min")):
        for key_suffix in ("_max", "_min"):
            full_key = field + key_suffix
            if full_key in filters:
                cn = col.get(field)
                if cn not in out.columns:
                    continue
                val = float(filters[full_key])
                if key_suffix == "_max":
                    out = out[out[cn] <= val]
                else:
                    out = out[out[cn] >= val]
    return out.reset_index(drop=True)


# ── utils ───────────────────────────────────────────────────────────


def save_as(df, path: str) -> str:
    """DataFrame 存盘，根据扩展名自动选格式. 返回最终绝对路径."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower()
    if ext == ".csv":
        df.to_csv(p, index=False)
    elif ext == ".json":
        df.to_json(p, orient="records", force_ascii=False, indent=2)
    elif ext in (".xlsx", ".xls"):
        df.to_excel(p, index=False)
    elif ext in (".md", ".markdown"):
        p.write_text(df.to_markdown(index=False))
    else:
        df.to_csv(p.with_suffix(".csv"), index=False)
        p = p.with_suffix(".csv")
    return str(p.resolve())


def summarize_financials(symbol: str) -> str:
    """财报 → markdown 摘要，给 LLM 做后续分析. 包含免责声明。"""
    info = get_stock_info(symbol)
    rpt = get_financial_report(symbol)
    name = info.get("股票简称", symbol)
    lines = [
        f"# {name} ({symbol}) 财务摘要",
        "",
        "> ⚠️ 本数据仅供学术研究，不构成投资建议。",
        "",
        "## 基本信息",
    ]
    for k in ("股票简称", "总股本", "流通股", "总市值", "流通市值",
              "行业", "上市时间"):
        if k in info:
            lines.append(f"- **{k}**: {info[k]}")
    lines.append("")
    lines.append("## 财报数据")
    for label, key in (("资产负债表", "balance_sheet"),
                        ("利润表", "income_statement"),
                        ("现金流量表", "cash_flow")):
        df = rpt.get(key)
        if df is None or df.empty:
            lines.append(f"### {label}: (无数据)")
            continue
        lines.append(f"### {label}（最近 4 期）")
        # 取最近 4 期，列不超过 8
        show = df.head(4)
        keep_cols = [c for c in show.columns if "REPORT" in c.upper()
                      or any(kw in c for kw in ("营业", "利润", "资产",
                                                  "负债", "经营", "现金"))][:8]
        if keep_cols:
            show = show[keep_cols]
        lines.append("```")
        lines.append(show.to_string(index=False))
        lines.append("```")
    return "\n".join(lines)


# ── charts ──────────────────────────────────────────────────────────
#
# 所有图表函数都保存 PNG 到 workspace，chat 前端 FileCard 自动内联渲染。
# 调用方需先 `from _akshare_helpers import *`，图表会自动适配中文字体。


def _setup_matplotlib_cn():
    """Configure matplotlib for Chinese labels. Detects actually-installed
    CJK fonts via font_manager (not just rcParams set), otherwise matplotlib
    silently falls back to DejaVu Sans and glyphs render as boxes."""
    import matplotlib
    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    # All candidate CJK fonts across platforms
    candidates = [
        "PingFang SC", "PingFang TC", "Heiti SC", "Hiragino Sans GB",
        "STHeiti", "Arial Unicode MS",                    # macOS
        "Microsoft YaHei", "SimHei", "SimSun", "Microsoft JhengHei",  # Windows
        "Noto Sans CJK SC", "Noto Sans CJK TC", "WenQuanYi Zen Hei",
        "WenQuanYi Micro Hei", "Source Han Sans SC",      # Linux
    ]
    installed = {f.name for f in fm.fontManager.ttflist}
    picked = [c for c in candidates if c in installed]
    if picked:
        # Put detected CJK fonts FIRST so matplotlib uses them
        plt.rcParams["font.sans-serif"] = picked + plt.rcParams["font.sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def plot_kline(df, out_path: str, title: str = "") -> str:
    """绘制 K 线图. df 需含列: 日期/开盘/收盘/最高/最低 (akshare 标准格式).
    返回保存路径（自动加 .png 后缀）."""
    plt = _setup_matplotlib_cn()
    import matplotlib.pyplot as _plt
    from matplotlib.patches import Rectangle

    p = Path(out_path)
    if p.suffix.lower() != ".png":
        p = p.with_suffix(".png")
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = _plt.subplots(figsize=(14, 6))
    for i, (_, row) in enumerate(df.iterrows()):
        o, c, h, l = row["开盘"], row["收盘"], row["最高"], row["最低"]
        color = "#e74c3c" if c >= o else "#27ae60"
        # wick
        ax.plot([i, i], [l, h], color=color, linewidth=0.7)
        # body
        ax.add_patch(Rectangle(
            (i - 0.3, min(o, c)), 0.6, abs(c - o) or 0.01,
            facecolor=color, edgecolor=color,
        ))
    ax.set_xlim(-1, len(df))
    # X 轴刻度（每 N 条抽一个日期）
    step = max(1, len(df) // 10)
    ticks = list(range(0, len(df), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(df.iloc[i]["日期"])[:10] for i in ticks],
                       rotation=45, ha="right")
    ax.set_title(title or "K 线图", fontsize=14)
    ax.set_ylabel("价格")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=110)
    _plt.close(fig)
    return str(p.resolve())


def plot_price_ma(df, out_path: str, ma_windows=(5, 20, 60),
                   title: str = "") -> str:
    """收盘价 + 均线图. df 需含 日期/收盘."""
    plt = _setup_matplotlib_cn()
    import matplotlib.pyplot as _plt

    p = Path(out_path)
    if p.suffix.lower() != ".png":
        p = p.with_suffix(".png")
    p.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = _plt.subplots(figsize=(14, 5))
    ax.plot(df["日期"], df["收盘"], label="收盘价", linewidth=1.3, color="#2c3e50")
    for w in ma_windows:
        if len(df) >= w:
            ma = df["收盘"].rolling(w).mean()
            ax.plot(df["日期"], ma, label=f"MA{w}", linewidth=1.0, alpha=0.85)
    ax.legend(loc="best")
    ax.set_title(title or "价格 & 均线", fontsize=14)
    ax.grid(alpha=0.3)
    step = max(1, len(df) // 10)
    ax.set_xticks(df["日期"].iloc[::step])
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    fig.savefig(p, dpi=110)
    _plt.close(fig)
    return str(p.resolve())


def plot_volume(df, out_path: str, title: str = "") -> str:
    """成交量柱状图. df 需含 日期/成交量/开盘/收盘."""
    plt = _setup_matplotlib_cn()
    import matplotlib.pyplot as _plt
    p = Path(out_path)
    if p.suffix.lower() != ".png":
        p = p.with_suffix(".png")
    p.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = _plt.subplots(figsize=(14, 3.5))
    colors = ["#e74c3c" if r["收盘"] >= r["开盘"] else "#27ae60"
              for _, r in df.iterrows()]
    ax.bar(range(len(df)), df["成交量"], color=colors, width=0.8)
    step = max(1, len(df) // 10)
    ticks = list(range(0, len(df), step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(df.iloc[i]["日期"])[:10] for i in ticks],
                       rotation=45, ha="right")
    ax.set_title(title or "成交量", fontsize=13)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=110)
    _plt.close(fig)
    return str(p.resolve())


def plot_sector_heatmap(df, out_path: str, top_n: int = 30,
                         title: str = "") -> str:
    """板块涨跌幅水平条. df 来自 get_sector_quote() 或 get_sector_flow()."""
    plt = _setup_matplotlib_cn()
    import matplotlib.pyplot as _plt
    p = Path(out_path)
    if p.suffix.lower() != ".png":
        p = p.with_suffix(".png")
    p.parent.mkdir(parents=True, exist_ok=True)

    # 猜列名
    name_col = next((c for c in df.columns if "名称" in c or "板块" in c), df.columns[0])
    pct_col = next((c for c in df.columns if "涨跌" in c or "涨跌幅" in c), None)
    if pct_col is None:
        # 资金流场景
        pct_col = next((c for c in df.columns if "净额" in c or "净流入" in c), df.columns[1])

    show = df[[name_col, pct_col]].head(top_n).copy()
    show = show.sort_values(pct_col)

    fig, ax = _plt.subplots(figsize=(10, max(6, top_n * 0.25)))
    colors = ["#e74c3c" if v > 0 else "#27ae60" for v in show[pct_col]]
    ax.barh(show[name_col], show[pct_col], color=colors)
    ax.set_title(title or f"板块 {pct_col} TOP{top_n}", fontsize=13)
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(p, dpi=110)
    _plt.close(fig)
    return str(p.resolve())


# ── analysis report template ────────────────────────────────────────


_DISCLAIMER = (
    "⚠️ 以下所有内容基于历史公开数据分析得出，**仅供学习研究**，"
    "**不构成任何投资建议**。市场有风险，投资需谨慎，任何实际操作"
    "的盈亏由您自行承担。"
)


def build_analysis_report(symbol: str, *,
                           history_days: int = 180,
                           workspace_dir: str = ".",
                           out_md: str = "") -> dict:
    """一站式股票分析: 拉数据 + 出 3 张图 + markdown 报告.

    返回 {md_path, kline_png, ma_png, vol_png, observations: {...}}
    图表和 md 都写进 workspace，chat 前端会自动内联显示 PNG.

    结构化 observations 里是**可观察事实**（MA 多头排列 / 量能背离 /
    市盈率百分位等），**不**做买/卖/持仓推荐。Agent 组装成自然语言
    呈现时必须保留 DISCLAIMER。
    """
    from datetime import datetime, timedelta
    pd = _pd_module()
    ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=history_days)).strftime("%Y-%m-%d")

    info = get_stock_info(symbol)
    realtime = get_stock_realtime(symbol)
    hist = get_stock_history(symbol, start, end, freq="daily")

    name = info.get("股票简称", symbol)

    # Charts
    kline_p = plot_kline(hist, str(ws / f"{symbol}_kline.png"),
                          title=f"{name} ({symbol}) K 线 · {start} → {end}")
    ma_p = plot_price_ma(hist, str(ws / f"{symbol}_ma.png"),
                          title=f"{name} 价格 & 均线")
    vol_p = plot_volume(hist, str(ws / f"{symbol}_vol.png"),
                        title=f"{name} 成交量")

    # Structured observations (facts only, no recommendation)
    obs: dict = {"symbol": symbol, "name": name}
    try:
        last = hist.iloc[-1]
        obs["latest_close"] = float(last["收盘"])
        obs["latest_change_pct"] = float(last.get("涨跌幅", 0) or 0)
        # 50d / 200d percentile
        if len(hist) >= 60:
            pct_60 = (hist["收盘"] < obs["latest_close"]).sum() / len(hist)
            obs["price_percentile_in_window"] = round(pct_60 * 100, 1)
        # MA alignment
        for w in (5, 20, 60):
            if len(hist) >= w:
                obs[f"ma{w}"] = round(float(hist["收盘"].iloc[-w:].mean()), 2)
        if all(f"ma{w}" in obs for w in (5, 20, 60)):
            if obs["ma5"] > obs["ma20"] > obs["ma60"]:
                obs["ma_alignment"] = "多头排列（MA5 > MA20 > MA60）"
            elif obs["ma5"] < obs["ma20"] < obs["ma60"]:
                obs["ma_alignment"] = "空头排列（MA5 < MA20 < MA60）"
            else:
                obs["ma_alignment"] = "均线纠缠（无明确趋势）"
        # Volume trend
        if len(hist) >= 10:
            recent_vol = hist["成交量"].iloc[-5:].mean()
            prior_vol = hist["成交量"].iloc[-10:-5].mean()
            if prior_vol > 0:
                obs["volume_change_5v5_pct"] = round(
                    (recent_vol / prior_vol - 1) * 100, 1)
    except Exception as e:
        obs["_warn"] = f"指标计算失败: {e}"

    # Markdown report
    md_lines = [
        f"# {name} ({symbol}) 分析报告",
        "",
        _DISCLAIMER,
        "",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 1. 基本信息",
    ]
    for k in ("股票简称", "行业", "上市时间", "总股本",
               "总市值", "流通市值"):
        if k in info:
            md_lines.append(f"- **{k}**: {info[k]}")

    md_lines += [
        "",
        "## 2. 实时行情",
        f"- 最新价: {realtime.iloc[0].get('最新价', '?')}",
        f"- 涨跌幅: {realtime.iloc[0].get('涨跌幅', '?')}%",
        f"- 成交额: {realtime.iloc[0].get('成交额', '?')}",
        f"- 市盈率-动态: {realtime.iloc[0].get('市盈率-动态', '?')}",
        f"- 市净率: {realtime.iloc[0].get('市净率', '?')}",
        "",
        "## 3. 技术面观察（基于过去 "
        f"{history_days} 天）",
    ]
    for k, v in obs.items():
        if k in ("symbol", "name"):
            continue
        md_lines.append(f"- **{k}**: {v}")

    md_lines += [
        "",
        "## 4. 图表",
        f"![K 线]({Path(kline_p).name})",
        "",
        f"![价格 & 均线]({Path(ma_p).name})",
        "",
        f"![成交量]({Path(vol_p).name})",
        "",
        "## 5. 分析结论（观察，非建议）",
        "> Agent 应基于上述数据给出**结构化观察**，不给出具体买/卖/持仓建议。",
        "> 建议以「关注点 / 风险 / 机会」三段式组织，每条标明数据依据。",
        "",
        "---",
        _DISCLAIMER,
    ]
    md_text = "\n".join(md_lines)
    md_p = Path(out_md) if out_md else ws / f"{symbol}_analysis.md"
    md_p.parent.mkdir(parents=True, exist_ok=True)
    md_p.write_text(md_text, encoding="utf-8")

    return {
        "md_path": str(md_p.resolve()),
        "kline_png": kline_p,
        "ma_png": ma_p,
        "vol_png": vol_p,
        "observations": obs,
        "disclaimer": _DISCLAIMER,
    }


# ── self-test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print("akshare-stock helpers self-test")
    print("=" * 40)
    try:
        info = get_stock_info("000001")
        print(f"✓ get_stock_info('000001'): {info.get('股票简称', '?')}"
              f" in {info.get('行业', '?')}")
    except AkShareError as e:
        print(f"✗ get_stock_info: {e}")
        sys.exit(1)
    try:
        df = get_stock_realtime("000001")
        price = df.iloc[0].get("最新价", "?")
        pct = df.iloc[0].get("涨跌幅", "?")
        print(f"✓ get_stock_realtime('000001'): 最新价={price} 涨跌={pct}%")
    except AkShareError as e:
        print(f"✗ get_stock_realtime: {e}")
    print("=" * 40)
    print("done")
