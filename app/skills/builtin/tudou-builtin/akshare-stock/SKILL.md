---
name: akshare-stock
description: Use when the user asks about Chinese A-share / 港股 / 美股 stocks — 股票行情、K 线、财报、板块资金流、龙虎榜、两融、指数、或要求"分析/研究/查"某只股票。Wraps the AkShare library via `_akshare_helpers.py` with chart generation + structured analysis report template. Output includes charts (PNG inline in chat) and observation-style analysis (NOT investment advice). Triggers: 分析股票, 看下XXX, 查一下股价, 沪深300, 板块资金流, 龙虎榜, 财报摘要.
applicable_roles:
  - "researcher"
  - "analyst"
  - "general"
scenarios:
  - "个股技术面 / 基本面分析"
  - "板块 / 指数研究"
  - "投资组合观察报告"
metadata:
  source: tudou-builtin
  license: Apache-2.0
  tier: official
---

# akshare-stock — 中文市场数据 + 股票分析

## ⚠️ 防幻觉 — 先读这一段

**这个 skill 的实际目录内容**（下面不在列表的文件都不存在）:

- `SKILL.md` — 本文档
- `_akshare_helpers.py` — 封装库（import 用）

**没有**任何 `ak_cli.sh` / `stock_query.py` / `analyze.sh` 类 one-shot 脚本，不要 `ls` / `find` 找。工作流就是：**你自己写 python 脚本**，从 `_akshare_helpers` 导入所需函数。

## 🚨 合规铁律（必须遵守，每次输出都检查）

1. **不构成投资建议**：任何分析报告必须在**显著位置**保留免责声明（`_akshare_helpers.build_analysis_report` 已自动生成，不要删）
2. **不做买/卖/持仓推荐**：禁止输出"建议买入"/"建议持有"/"止盈 X 元"这类具体操作指令
3. **可以输出**：结构化**观察**（多头排列 / 价格百分位 / 量能异常），以"关注点 / 风险 / 机会"三段式组织
4. **数据时效**：必须注明数据日期，用户根据最新信息自行判断
5. 用户追问"该不该买"类问题 → 重申免责 + 给结构化观察 + 建议咨询持证顾问

---

## 工作流（5 步，不跳步）

### 1. 写 python 脚本到 workspace

```python
# workspace/analyze_stock.py
from _akshare_helpers import *

# 一站式分析（最常用）—— 自动出 3 张图 + markdown 报告
result = build_analysis_report(
    symbol="000001",            # 平安银行
    history_days=180,
    workspace_dir=".",          # 当前工作目录（$AGENT_WORKSPACE）
)
print("md:", result["md_path"])
print("k_line:", result["kline_png"])
print("observations:", result["observations"])
```

### 2. bash 跑

```bash
cd "$AGENT_WORKSPACE"
python -m py_compile workspace/analyze_stock.py || echo "SYNTAX ERROR"
python workspace/analyze_stock.py 2>&1
```

### 3. Chat 里自动显示图表

脚本产生的 `.png` 文件自动作为 FileCard 附件显示在 chat 气泡下面，用户一眼就能看到 K 线、均线、成交量三张图。**不需要**额外 upload 动作。

### 4. Agent 基于 observations 组织自然语言

从 `result["observations"]` 里拿到客观指标（`ma_alignment`, `price_percentile_in_window`, `volume_change_5v5_pct` 等），在 chat 里按**三段式**组织：

```
## 关注点
- 当前价格处于过去 180 天的 85% 分位（相对高位）
- 均线呈多头排列（MA5 > MA20 > MA60）
- 成交量相比前 5 日放大 23%

## 风险
- 价格已接近年内高点，回调风险
- 市盈率（动态）38 倍，显著高于同行业均值

## 机会
- 行业板块近 5 日资金净流入 12 亿，热度提升
- Q3 财报净利润同比 +18%，好于一致预期

---
⚠️ 以上仅为数据观察，不构成投资建议。
```

### 5. 若用户要更深分析，再调其他 API

| 用户问 | 调什么 |
|---|---|
| "这只股票历史最大回撤？" | `get_stock_history` → 算 drawdown |
| "同行业其他股票对比？" | `get_sector_quote` + 筛选同板块 |
| "今天龙虎榜谁进了？" | `get_top_list()` |
| "两融余额走势？" | `get_margin_detail(symbol)` |
| "帮我筛市盈率小于 20 的股票" | `screen_stocks({"pe_max": 20, "pe_min": 5})` |
| "看下沪深 300 这个月表现" | `get_index_history("sh000300", ...)` |
| "哪个行业板块资金流入最多？" | `get_sector_flow("today")` + `plot_sector_heatmap` |

---

## 可用函数速查（13 个）

### 数据获取

```python
get_stock_realtime(symbol)              # 实时行情（1 条）
get_stock_history(symbol, start, end,
                   freq="daily", adjust="qfq")  # K 线
get_stock_info(symbol)                  # 基本信息 dict
get_financial_report(symbol, year=None) # 三表 DataFrame
get_sector_quote()                      # 全板块实时排行
get_sector_flow(period="today")         # 板块资金流
get_index_history(index, start, end)    # 指数 K 线
get_top_list(date=None)                 # 龙虎榜
get_margin_detail(symbol=None)          # 两融明细
screen_stocks(filters)                  # 条件筛选
```

### 图表（保存 PNG 到 workspace，chat 自动渲染）

```python
plot_kline(df, out_path, title="")
plot_price_ma(df, out_path, ma_windows=(5, 20, 60), title="")
plot_volume(df, out_path, title="")
plot_sector_heatmap(df, out_path, top_n=30, title="")
```

### 工具

```python
save_as(df, path)                       # 自动 csv/json/xlsx/md
summarize_financials(symbol) -> str     # 财报 → markdown
build_analysis_report(symbol, ...)      # 一站式: 数据 + 图 + md
```

---

## Symbol 格式约定

| 市场 | 格式 | 示例 |
|---|---|---|
| A 股 | 6 位数字，**不加**交易所前缀 | `"000001"` (平安银行) |
| 港股 | 5 位数字 | `"00700"` (腾讯) |
| 指数 | 含前缀 | `"sh000001"` (上证), `"sh000300"` (沪深 300), `"sh000905"` (中证 500) |

---

## 常见坑

1. **首次 import akshare 较慢**（~1.5s）—— 正常，之后缓存
2. **AkShare 是爬虫**，目标网站改版会导致间歇失败 → helper 内置 1 次重试，还失败抛 `AkShareError`，**停下报告用户**，不要无限重试
3. **中文字体**：helper 自动 fallback 到系统中文字体（PingFang SC / Microsoft YaHei / Heiti SC）。如果图里中文显示为方块，说明系统无中文字体，提示用户安装
4. **`screen_stocks` 不是后端筛选**，是拉全市场快照后在内存过滤，股票池 5000+ 时约 2-3s
5. **周末/节假日**：龙虎榜 / 实时行情可能为空，helper 对龙虎榜做了"往前找 5 天"fallback，其他函数需 agent 处理空结果

---

## 示例：完整一次任务

```python
# workspace/full_report.py
from _akshare_helpers import *

# Step 1: 一站式分析
r = build_analysis_report("000001", history_days=180, workspace_dir=".")

# Step 2: 查同行业（银行板块）资金流
flow = get_sector_flow("today")
banks = flow[flow["名称"].str.contains("银行")]
banks.head(5).to_json("sector_banks.json",
                       orient="records", force_ascii=False)

# Step 3: 板块热力图
plot_sector_heatmap(flow, "sector_flow.png", top_n=20,
                     title="今日行业资金流 TOP 20")

print("DONE:")
print(" - md:", r["md_path"])
print(" - K 线:", r["kline_png"])
print(" - 均线:", r["ma_png"])
print(" - 成交量:", r["vol_png"])
print(" - 板块热力图: sector_flow.png")
print(" - 银行板块资金流: sector_banks.json")
```

Agent 跑完上面脚本后，chat 里会自动出现 4 张图 + 1 份 md 报告 + 1 份银行板块资金流 JSON。Agent 在 chat 里用"关注点 / 风险 / 机会"三段式组织分析（带免责声明），用户就能完整看到数据 + 图 + 分析。
