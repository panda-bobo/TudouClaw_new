---
name: pptx-author
description: Use when the user asks you to produce a PowerPoint (.pptx) file — presentation, slide deck, report, 产品介绍, 市场分析, 路演, 汇报, 会议纪要, PPT. Write a python-pptx script, run it with bash, and verify the output slide-by-slide. This replaces the declarative create_pptx_advanced tool (which has a silent-blank-slide failure mode). Triggers: 生成PPT, 生成pptx, 做一份PPT, slide deck, presentation, 幻灯片.
metadata:
  source: tudou-builtin
  license: Apache-2.0
  tier: official
---

# pptx-author — 用 python-pptx 脚本生成 PPT（替代 create_pptx_advanced）

## 为什么用这个 skill，不用 create_pptx_advanced

`create_pptx_advanced(slides=[{layout: {...}}])` 是一个 declarative 工具：你传 JSON spec，它调 18 个预设 layout 函数之一渲染。问题：

- **silent blank slide**：当 spec 格式有细微错误（字段缺失、嵌套结构不对、类型错选），对应 layout 函数抛异常，工具**只 print 到 stderr**然后继续，那一页就是 0 shape 的空白页。你看不到错误。用户看到的就是"为什么中间几页是空的"。
- **表达力有限**：18 个固定模板之外的任何变化（比如把柱状图放左边、文字放右边，加一个渐变背景带数字标注，做个 2x3 的混合卡片）都做不出来。
- **迭代不可见**：出问题无法调试，只能重生成。

**python-pptx 脚本路径**不会有这些问题：

- 脚本 crash → Bash 退出码非 0 → 你立刻看到 traceback → 改一行重跑
- 所有 python-pptx 能做的（形状、渐变、图表、表格、图片、主题、动画）你都能做
- 每页长什么样是你在代码里**直接控制**的，不依赖任何中间 DSL

**铁律**：需要生成 .pptx？→ 优先用这个 skill 的脚本路径；不要调 `create_pptx_advanced`。

---

## 工作流（四步，不要跳步）

### 1. 先明确结构，再写代码

列一个 slide plan（不用写 JSON，自然语言即可），把每页的作用、核心信息、视觉构图想清楚：

```
1. 封面 — 标题 / 副标题 / 日期
2. 目录 — 5-7 个章节
3. 市场概况 — 左文 + 右 KPI 卡片 × 3
4. 竞争对比 — 4 列对比表
5. 趋势图 — 折线图 + 文字标注
6. 行动项 — 3 个大卡片
7. 总结 — 全屏标语 + 联系方式
```

然后**按这个 plan 写一段 python 脚本**，一页一函数（`def slide_cover(prs):` / `def slide_toc(prs):` …），最后主程序按顺序调用。

### 2. 写脚本到工作目录

```bash
# Write to sandbox — use write_file tool, NOT bash heredoc
# The script path: $AGENT_WORKSPACE/build_report.py (or project shared dir)
```

脚本模板见下面 "Reference scripts" 章节，直接抄改即可。

### 3. 跑脚本，立刻看 stderr

```bash
cd "$AGENT_WORKSPACE"
python build_report.py 2>&1
```

- 退出码 0 且无 `Error` / `Traceback` → 继续
- 有 traceback → 看最后一行报错 → 定位行号 → 改一行 → **不要整个重写**（改错的地方，保留其他）

### 4. 逐页验证——这一步不可省

```bash
python - <<'PY'
from pptx import Presentation
p = Presentation("/abs/path/to/out.pptx")
for i, s in enumerate(p.slides, 1):
    shapes = list(s.shapes)
    texts = [sh.text_frame.text[:40] for sh in shapes
             if sh.has_text_frame and sh.text_frame.text.strip()]
    flag = "BLANK" if len(shapes) == 0 else ("THIN" if len(shapes) < 3 else "OK")
    print(f"  {i:2d}: {len(shapes):2d} shapes [{flag}]  {texts[:2]}")
PY
```

**出现任何 `BLANK` 行都算失败**——回到第 3 步，在脚本里定位那一页的函数，修好，重跑。不要交付带空页的 pptx。

---

## python-pptx cheatsheet（你需要的 80%）

```python
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

prs = Presentation()
prs.slide_width  = Inches(13.333)   # 16:9
prs.slide_height = Inches(7.5)

SW, SH = prs.slide_width, prs.slide_height
blank = prs.slide_layouts[6]        # blank layout — no placeholders

def hex_color(s):                    # "#2563EB" -> RGBColor(0x25, 0x63, 0xEB)
    s = s.lstrip("#")
    return RGBColor(int(s[0:2],16), int(s[2:4],16), int(s[4:6],16))

THEME = {
    "bg":        hex_color("#0F172A"),
    "fg":        hex_color("#F8FAFC"),
    "accent":    hex_color("#22D3EE"),
    "muted":     hex_color("#94A3B8"),
    "card_bg":   hex_color("#1E293B"),
    "ok":        hex_color("#22C55E"),
    "warn":      hex_color("#F59E0B"),
}
FONT = "Microsoft YaHei"   # 中文 deck 用这个;英文可换 Inter / Arial
```

### 背景 & 文本

```python
def set_bg(slide, color):
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = color

def add_text(slide, x, y, w, h, text, *, size=18, bold=False,
             color=None, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.05)
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    return tb
```

### 卡片（圆角矩形 + 文字）

```python
def add_card(slide, x, y, w, h, title, body, *, fill=None, accent=None):
    card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    card.fill.solid()
    card.fill.fore_color.rgb = fill or THEME["card_bg"]
    card.line.fill.background()                    # no outline
    add_text(slide, x+Inches(0.2), y+Inches(0.15), w-Inches(0.4), Inches(0.5),
             title, size=16, bold=True, color=accent or THEME["accent"])
    add_text(slide, x+Inches(0.2), y+Inches(0.75), w-Inches(0.4), h-Inches(0.9),
             body, size=12, color=THEME["fg"])
```

### 表格

```python
def add_table(slide, x, y, w, h, headers, rows):
    tbl_shape = slide.shapes.add_table(len(rows)+1, len(headers), x, y, w, h)
    tbl = tbl_shape.table
    # header
    for c, text in enumerate(headers):
        cell = tbl.cell(0, c)
        cell.text = text
        cell.fill.solid()
        cell.fill.fore_color.rgb = THEME["accent"]
        for p in cell.text_frame.paragraphs:
            for r in p.runs:
                r.font.bold = True
                r.font.color.rgb = THEME["bg"]
                r.font.size = Pt(13)
                r.font.name = FONT
    # rows
    for r, row in enumerate(rows, start=1):
        for c, text in enumerate(row):
            cell = tbl.cell(r, c)
            cell.text = str(text)
            cell.fill.solid()
            cell.fill.fore_color.rgb = THEME["card_bg"] if r % 2 == 0 else hex_color("#273449")
            for p in cell.text_frame.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(11)
                    run.font.color.rgb = THEME["fg"]
                    run.font.name = FONT
```

### 图表

```python
def add_bar_chart(slide, x, y, w, h, categories, series_name, values):
    data = CategoryChartData()
    data.categories = categories
    data.add_series(series_name, values)
    chart_shape = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, x, y, w, h, data)
    chart = chart_shape.chart
    chart.has_title = False
    chart.has_legend = False
    return chart
```

### 图片

```python
def add_image(slide, x, y, w, h, image_path):
    # image_path MUST be a real file. If missing, fall through to a placeholder
    # rectangle — do not crash the whole script.
    import os
    if not os.path.isfile(image_path):
        ph = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
        ph.fill.solid()
        ph.fill.fore_color.rgb = THEME["muted"]
        add_text(slide, x, y+h/2-Inches(0.2), w, Inches(0.4),
                 f"[image missing: {os.path.basename(image_path)}]",
                 size=12, color=THEME["bg"], align=PP_ALIGN.CENTER)
        return
    slide.shapes.add_picture(image_path, x, y, w, h)
```

---

## Reference scripts（直接抄改）

### 最小完整脚本 — 1 页封面 + 1 页卡片 + 1 页结尾

```python
#!/usr/bin/env python3
# build_deck.py
import sys, os
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

OUT = os.environ.get("AGENT_WORKSPACE", ".") + "/out.pptx"

# --- theme / helpers (copy from cheatsheet) ---
def hex_color(s):
    s = s.lstrip("#")
    return RGBColor(int(s[0:2],16), int(s[2:4],16), int(s[4:6],16))
THEME = {"bg": hex_color("#0F172A"), "fg": hex_color("#F8FAFC"),
         "accent": hex_color("#22D3EE"), "muted": hex_color("#94A3B8"),
         "card_bg": hex_color("#1E293B")}
FONT = "Microsoft YaHei"

def set_bg(slide, c):
    f = slide.background.fill; f.solid(); f.fore_color.rgb = c

def add_text(slide, x, y, w, h, text, *, size=18, bold=False, color=None,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    tb = slide.shapes.add_textbox(x,y,w,h); tf = tb.text_frame
    tf.word_wrap = True; tf.vertical_anchor = anchor
    p = tf.paragraphs[0]; p.alignment = align
    r = p.add_run(); r.text = text
    r.font.name = FONT; r.font.size = Pt(size); r.font.bold = bold
    if color is not None: r.font.color.rgb = color
    return tb

def add_card(slide, x, y, w, h, title, body):
    c = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x,y,w,h)
    c.fill.solid(); c.fill.fore_color.rgb = THEME["card_bg"]
    c.line.fill.background()
    add_text(slide, x+Inches(0.2), y+Inches(0.15), w-Inches(0.4), Inches(0.5),
             title, size=16, bold=True, color=THEME["accent"])
    add_text(slide, x+Inches(0.2), y+Inches(0.75), w-Inches(0.4), h-Inches(0.9),
             body, size=12, color=THEME["fg"])

# --- build ---
prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
SW, SH = prs.slide_width, prs.slide_height
blank = prs.slide_layouts[6]

def slide_cover(prs):
    s = prs.slides.add_slide(blank); set_bg(s, THEME["bg"])
    add_text(s, Inches(0.8), Inches(2.5), SW-Inches(1.6), Inches(1.2),
             "市场分析报告", size=48, bold=True, color=THEME["fg"])
    add_text(s, Inches(0.8), Inches(3.8), SW-Inches(1.6), Inches(0.6),
             "2026 Q2 · 战略投研组", size=20, color=THEME["accent"])

def slide_cards(prs):
    s = prs.slides.add_slide(blank); set_bg(s, THEME["bg"])
    add_text(s, Inches(0.6), Inches(0.4), SW-Inches(1.2), Inches(0.8),
             "核心发现", size=28, bold=True, color=THEME["fg"])
    cards = [
        ("市场规模", "2025 年区域市场达 $4.2B, YoY +18%, 预计 2028 年突破 $7B."),
        ("增长动力", "政策开放 + 企业云迁移加速 + 本地化合规推动三方协同."),
        ("关键风险", "美元汇率波动、地缘合规壁垒、渠道分发依赖单一 GSI."),
    ]
    cw, gap = Inches(3.9), Inches(0.3)
    for i, (t, b) in enumerate(cards):
        x = Inches(0.6) + i*(cw+gap)
        add_card(s, x, Inches(1.8), cw, Inches(4.8), t, b)

def slide_closing(prs):
    s = prs.slides.add_slide(blank); set_bg(s, THEME["bg"])
    add_text(s, Inches(0), Inches(2.8), SW, Inches(1.2), "谢谢观看",
             size=64, bold=True, color=THEME["accent"],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    add_text(s, Inches(0), Inches(4.4), SW, Inches(0.6),
             "questions → research@tudouclaw.ai",
             size=18, color=THEME["muted"], align=PP_ALIGN.CENTER)

slide_cover(prs)
slide_cards(prs)
slide_closing(prs)

prs.save(OUT)
print(f"OK: {OUT} ({len(prs.slides)} slides)")
```

### 需要更多 layout？照着加函数就行

- 目录页：一列编号 + 标题文本
- KPI 数字：大数字 + 下方小标签（用 `add_text(size=60, bold=True)` 堆叠）
- 对比表格：调用上面 `add_table` helper
- 图表页：`add_bar_chart` / 切 `XL_CHART_TYPE.LINE` / `PIE`
- 引用块：圆角矩形底色 + 斜体文字 + `"— 作者名"`

**每加一页就加一个 `slide_xxx(prs)` 函数，主程序里按顺序调**。代码 300 行内能搞定 10 页。

---

## 几个常见陷阱

| 症状 | 原因 | 解法 |
|------|------|------|
| 文字显示为方框 □□□ | 字体名拼错 / 系统无此字体 | 中文 deck 用 "Microsoft YaHei", 英文用 "Calibri" / "Arial" |
| 形状超出页面 | 用 Inches() 加出去了 | 检查 x+w ≤ Inches(13.333), y+h ≤ Inches(7.5) |
| 表格单元格样式不生效 | 忘了把 `cell.text` 的已存在 paragraph 重新改格式 | 用 `for p in cell.text_frame.paragraphs: for r in p.runs: ...` |
| `add_picture` 报 File not found | 图片路径相对而非绝对 | 一律用绝对路径, 或 `os.path.join(AGENT_WORKSPACE, ...)` |
| 图表没显示 | 忘了 `data.categories` 或 series values 长度不一致 | 确认 `len(values) == len(categories)` |
| 保存 .pptx 后 PowerPoint 打开报错 | 一般是 shape 边界越界或图片损坏 | 重新跑验证脚本逐页看 shape count, 定位出错页 |
| **多列布局文字串栏 / 重叠到旁边的卡片** | 左列宽度没扣掉右列的占用空间。LLM 写代码时按"左+右两列"视觉规划,但给左列写了 W=8.0 没考虑右列 L=8.2 → 1.6" 重叠区 | **每个 shape 算 right_edge = left + width,确保 ≤ 下一列的 left**。或先定义 `COLS = [(L1,W1), (L2,W2)]` 全局变量,所有 shape 引用 col 边界 |

---

## 排版铁律 — 字数 / 字号 / 文本框尺寸的换算

LLM 经常写"看起来合理"的尺寸但实际**塞不下**或**挤出去**。用下列经验公式估算,**写完一个文本框就立刻验算**:

**单行能装多少字 (近似)**

| 字号 | 中文(每英寸) | 英文字符(每英寸) |
|------|--------------|----------------|
| 10pt | 8 字 | 16 字符 |
| 12pt | 7 字 | 14 字符 |
| 14pt | 6 字 | 12 字符 |
| 16pt | 5 字 | 10 字符 |
| 18pt | 4-5 字 | 9 字符 |
| 24pt | 3-4 字 | 7 字符 |
| 32pt | 2-3 字 | 5 字符 |

**计算公式 — 写文本框前必算**

```python
def fits_one_line(text: str, width_inches: float, font_pt: float, is_chinese=True) -> bool:
    """Return True if text fits in a single line at given font size."""
    cps_per_inch = (78 / font_pt) if is_chinese else (160 / font_pt)
    capacity = int(width_inches * cps_per_inch)
    return len(text) <= capacity

def required_height(text: str, width_inches: float, font_pt: float, is_chinese=True) -> float:
    """Return the height in inches needed to fit text with wrapping."""
    cps_per_inch = (78 / font_pt) if is_chinese else (160 / font_pt)
    chars_per_line = max(1, int(width_inches * cps_per_inch))
    line_count = (len(text) + chars_per_line - 1) // chars_per_line
    line_height_in = font_pt * 1.4 / 72  # 1.4 line spacing, 72 pt/inch
    return line_count * line_height_in + 0.10  # padding

# Use it
title = "GPU 集群高速组网拓扑详细方案"
assert fits_one_line(title, 6.0, 24, True), "title overflows"
body = "● 单节点: 8× H100/H800 GPU\n● GPU显存: 640 GB HBM3 / 节点 ..."
h_needed = required_height(body, width_inches=4.0, font_pt=12)
# Use h_needed as the textbox height, NOT a guess like 4.0"
```

**多列布局 — 必算 column boundaries 防重叠**

```python
# ❌ WRONG: hardcode each shape's L/W independently — easy to overlap
add_textbox(slide, Inches(1.8), Inches(2.6), Inches(8.0), Inches(0.3), "...")  # right edge 9.8
add_textbox(slide, Inches(8.2), Inches(1.5), Inches(4.5), Inches(5.3), "...")  # left edge 8.2 → OVERLAP!

# ✅ RIGHT: define columns up front, derive each shape from them
SLIDE_W = 13.333
MARGIN_L, MARGIN_R = 0.8, 0.6
GAP = 0.4              # gap between columns
COLS = 2
COL_W = (SLIDE_W - MARGIN_L - MARGIN_R - GAP * (COLS - 1)) / COLS  # = 5.77
COL_L = [MARGIN_L + i * (COL_W + GAP) for i in range(COLS)]         # [0.8, 6.97]

add_textbox(slide, Inches(COL_L[0]), Inches(2.6), Inches(COL_W), Inches(0.3), "...")
add_textbox(slide, Inches(COL_L[1]), Inches(1.5), Inches(COL_W), Inches(5.3), "...")
```

**何时该缩字号 vs 何时该缩字数**

| 场景 | 处理 |
|------|------|
| 用户给的标题 12 字内 | 32pt 加粗,不要缩 |
| 用户给的标题 13-20 字 | 24pt; 超过则换行(不要硬塞 32pt) |
| 章节副标题 5 字以内 | 18pt 加粗 |
| 章节副标题 6-15 字 | 16pt |
| 正文(< 80 字) | 14pt,留 1.4 行距 |
| 正文(80-200 字) | 12pt,考虑分两栏 |
| 正文 (>200 字) | 10pt + 必须分栏或精简,不要塞满一页 |
| 卡片摘要 / KPI 数字 | 36-48pt,周围只放 1-2 行说明 |

**铁律: 字号低于 10pt 的内容不该出现在 slide 上**——能不能讲清楚靠的是删字,不是缩字号。如果文本框里塞了 200 字,先问"这页要表达 1 个核心意思,删掉 70% 行不行"。

---

## 标题强制单行 — 不允许 wrap

**铁律: slide 标题永远 1 行,任何标题超出容器宽度的情况都视为 BUG**, 必须用以下三种方式之一处理(按优先顺序):

1. **缩短文字** — "Layer 2 — High-Speed Interconnect Fabric (高速互联结构)" → "Layer 2 高速互联 Fabric"
2. **缩字号** — 30pt 装不下就降到 24pt, 24pt 不行就 20pt; **不要为了字号大硬塞 2 行**
3. **加宽容器** — 标题容器宽度推到 slide 极限 (`SLIDE_W - 2 * MARGIN`)

**写代码前必算 — 标题最大可用字号:**

```python
def max_title_font_pt(title: str, container_width_in: float, is_chinese=None) -> int:
    """Return the largest font size that lets `title` fit on ONE line."""
    if is_chinese is None:
        is_chinese = any('一' <= c <= '鿿' for c in title)
    chars = len(title)
    if chars == 0: return 32
    # Solve: container_width_in * (cps_per_inch_at_X) >= chars
    # where cps_per_inch = (78 if cn else 160) / X
    # → X <= (78 or 160) * container_width_in / chars
    cap = 78 if is_chinese else 160
    max_pt = int(cap * container_width_in / chars)
    # Snap to standard sizes; cap at 32pt (titles bigger feel obnoxious)
    for std in [32, 28, 24, 20, 18, 16, 14, 12]:
        if std <= max_pt:
            return std
    return 12  # below this, the title is too long for ANY size — go shorten the text

# Use it
TITLE = "Layer 2 — High-Speed Interconnect Fabric (高速互联结构)"
W_IN = 12.0
fs = max_title_font_pt(TITLE, W_IN)   # → 18 in this case
add_textbox(slide, Inches(0.8), Inches(0.4), Inches(W_IN), Inches(0.6),
            TITLE, font_size=fs, bold=True)
```

**如果 max_title_font_pt 返回值 < 16pt 怎么办?** 标题就是太长了, 缩字号到 14pt 看起来很怂; 此时**回头改 TITLE 字符串**, 把"高速互联结构(子标题层)"那种括号副标题去掉, 让主标题保持简洁。永远不要为了"完整保留用户给的字"而牺牲视觉效果。

---

## 卡片 + 文字 — 文字 frame 必须在卡片 contained 范围内

**铁律: 当你设计"底框卡片 + 上层文字"的结构时, 文字 frame 的 L/T/R/B 必须严格 ≤ 卡片的 L/T/R/B (留 padding)**。否则文字会"溢出卡片", 视觉上像漂在卡片外。

**反模式 (错误):**
```python
# ❌ WRONG — text frame W=4.5", but card W=4.0" — text overflows card visually
add_card(slide, L=8.0, T=2.0, W=4.0, H=3.0, fill="card_bg")
add_textbox(slide, L=7.8, T=1.9, W=4.5, H=3.2, "标题", font_size=14)
```

**正确模式: 永远从卡片定义反推文字 frame:**
```python
# ✅ RIGHT — derive text frame from card with explicit padding
PAD = 0.2   # 0.2" padding inside card
def add_card_with_text(slide, L, T, W, H, title, body, fill_color):
    # 1. card background
    card = add_rectangle(slide, Inches(L), Inches(T), Inches(W), Inches(H), fill_color)
    # 2. title text — contained in upper region of card
    title_h = 0.5
    add_textbox(slide, Inches(L+PAD), Inches(T+PAD),
                Inches(W - 2*PAD), Inches(title_h),
                title, font_size=16, bold=True)
    # 3. body text — contained in lower region of card
    body_t = T + PAD + title_h + 0.1
    body_h = H - (body_t - T) - PAD
    add_textbox(slide, Inches(L+PAD), Inches(body_t),
                Inches(W - 2*PAD), Inches(body_h),
                body, font_size=12)
    # Sanity assert: text frame must be inside card
    assert L+PAD >= L and L+PAD + (W-2*PAD) <= L+W, "text overflows card horizontally"
    assert T+PAD >= T and body_t + body_h <= T+H, "text overflows card vertically"
```

**自检条件 (built into the QA gate's "REAL OVERLAP" check):**
- 卡片 + contained 文字 → 不报警 (合法)
- 文字 L/T/R/B 任何一边超出卡片 → 报警 "REAL OVERLAP X% with [Rectangle N]" — 必须修文字 frame 让它真正 contained

**Slide 9 那个 "97% overlap with [Rectangle 21]" 就是这个错误**: 文字 "带外管理网络隔离..." 的 frame 不在 Rectangle 21 范围内。修复时要么扩大 Rectangle 21, 要么缩小文字 frame, 让 contained 关系成立。

---

## 质量门（声明完成前必须通过）

跑下列**统一验证脚本**, 它做 4 件事:

1. **BLANK 页检测** — shape 数 == 0 报警
2. **超出页面边界** — shape 越界报警
3. **真 overlap 检测** — **跳过"卡片背景包含文字"这种合法包含**, 只报真正错位的 overlap
4. **文字垂直溢出** — 用字号 + 容器尺寸算出"能塞几行" vs "实际几行", 差距 > 5% 报警

任何一项不过就不要说"已生成 pptx":

```bash
python3 - <<'PY'
from pptx import Presentation
from pptx.util import Emu

PPTX = "/abs/path/to/out.pptx"   # 改成你的 deck 路径
p = Presentation(PPTX)
SW, SH = p.slide_width, p.slide_height
TOL = Emu(0.05 * 914400)   # 0.05" tolerance for "containment" classification

def overlap_pct(a, b):
    x = max(0, min(a['r'], b['r']) - max(a['l'], b['l']))
    y = max(0, min(a['b'], b['b']) - max(a['t'], b['t']))
    ov = x * y
    if ov == 0: return 0.0
    return 100.0 * ov / min(a['w']*a['h'], b['w']*b['h'])

def is_contained(inner, outer):
    """True if inner box fully fits inside outer (intentional card-text relationship)."""
    return (inner['l'] >= outer['l']-TOL and inner['r'] <= outer['r']+TOL
            and inner['t'] >= outer['t']-TOL and inner['b'] <= outer['b']+TOL)

def has_chinese(s):
    return any('一' <= c <= '鿿' for c in s)

problems = []
slides = list(p.slides)
for i, slide in enumerate(slides, 1):
    rects = []
    for sh in slide.shapes:
        if sh.left is None: continue
        text = sh.text_frame.text.strip() if sh.has_text_frame else ""
        max_fs = 0
        if sh.has_text_frame:
            for para in sh.text_frame.paragraphs:
                for r in para.runs:
                    if r.font.size: max_fs = max(max_fs, r.font.size.pt)
        if max_fs == 0: max_fs = 12
        rects.append({'l':sh.left,'t':sh.top,'r':sh.left+sh.width,'b':sh.top+sh.height,
                      'w':sh.width,'h':sh.height,'text':text,'has_text':bool(text),'fs':max_fs})

    # CHECK 1: BLANK (skip cover/end slide which may legitimately be sparse)
    if len(rects) == 0 and i not in (1, len(slides)):
        problems.append(f"slide {i}: BLANK")

    # CHECK 2: out-of-page boundary
    for r in rects:
        if r['r'] > SW + Emu(0.01*914400) or r['b'] > SH + Emu(0.01*914400):
            problems.append(f"slide {i}: '{r['text'][:30]}' overflows slide boundary")

    # CHECK 3: real overlap (NOT containment)
    for a in range(len(rects)):
        for b in range(a+1, len(rects)):
            ra, rb = rects[a], rects[b]
            # Skip intentional card-text containment
            if is_contained(ra, rb) or is_contained(rb, ra):
                continue
            ov = overlap_pct(ra, rb)
            if ov < 10: continue
            ta = ra['text'][:25] if ra['has_text'] else "[shape]"
            tb = rb['text'][:25] if rb['has_text'] else "[shape]"
            problems.append(f"slide {i}: REAL OVERLAP {ov:.0f}% — '{ta}' <-> '{tb}'")

    # CHECK 4: text vertical overflow (content > frame can hold)
    # CHECK 4a: TITLE wrap — any large-font text (≥18pt) must be ONE line
    for r in rects:
        if not r['has_text']: continue
        w_in = r['w']/914400; h_in = r['h']/914400
        cps_per_inch = (78/r['fs']) if has_chinese(r['text']) else (160/r['fs'])
        chars_per_line = max(1, int(w_in * cps_per_inch))
        line_h_in = r['fs'] * 1.4 / 72
        max_lines = max(1, int(h_in / line_h_in))
        needed_lines = sum((len(line)+chars_per_line-1)//chars_per_line for line in r['text'].split('\n'))

        # Distinguish TITLE (large font, expected single-line) from BODY
        is_title = r['fs'] >= 18 and '\n' not in r['text']  # explicit \n means user planned multi-line
        if is_title and needed_lines > 1:
            cap = 78 if has_chinese(r['text']) else 160
            suggested_pt = int(cap * w_in / max(1, len(r['text'])))
            problems.append(
                f"slide {i}: TITLE WRAP — '{r['text'][:35]}...' "
                f"({len(r['text'])} chars @ {r['fs']:.0f}pt in W={w_in:.1f}\" wraps to {needed_lines} lines). "
                f"Title MUST be 1 line — either shorten title, or drop font to ~{suggested_pt}pt."
            )
        elif needed_lines > max_lines * 1.05:
            problems.append(
                f"slide {i}: TEXT OVERFLOW — '{r['text'][:30]}...' "
                f"(needs {needed_lines} lines @ {r['fs']:.0f}pt in W={w_in:.1f}\", H fits only {max_lines})"
            )

if problems:
    print(f"❌ QA FAILED — {len(problems)} issues:")
    for prob in problems: print("  -", prob)
    raise SystemExit(1)
else:
    print(f"✓ QA PASSED — {len(slides)} slides clean (no overlap, no overflow)")
PY
```

**质量门 = 上面这段脚本退出码 0**。任何一条 problem 都必须回头改 build_deck.py 后重跑, 不要交付带 overlap / overflow 的 deck。

**两类典型修法:**

| 报警类型 | 修法 |
|---------|------|
| `REAL OVERLAP X%` 在 slide 2 的左右两列文字之间 | 修 build_deck.py 里那两个 shape 的 L/W,确保左列 right_edge ≤ 右列 left。最稳的是把 `COL_L = [...]` 全局变量提前定义, shape 引用 `COL_L[0]` / `COL_L[1]`,而不是硬编码 |
| `TEXT OVERFLOW` 标题 30pt 在 W=10" H=0.5" | 标题超出 1 行能装的字数 → (a) 换行符 `\n` 主动断 + 增大 H; (b) 缩字号到 24pt; (c) 缩短标题文字。**不要**直接把 H 加到 1.5" 还塞 30pt — 视觉上很丑 |
| `TEXT OVERFLOW` 正文 12pt 在 W=4" 需要 14 行 | 正文太多 → (a) **分两栏**(优先选这个); (b) 删字精简(每页 1 个核心意思); (c) 拆成 2 页。**不要**直接缩到 9pt 硬塞 |

**为什么不能靠肉眼:** LLM 看截图时容易"按预期看", 真有 1.6" 重叠也会脑补成"看起来挺好"; 文本框溢出在 PowerPoint 渲染里有时被自动截断, 截图上看不出来但打印或 export 时会显现。基于 shape 几何 + 字号容量的**程序化检查**不会被骗。

**Containment 例外的设计逻辑:** 卡片设计常用"先画一个圆角矩形(背景)→再在矩形内放文字"的两层结构, 文字 100% 在矩形内是**预期的**, 不是 overlap bug。所以 CHECK 3 用 `is_contained` 跳过包含关系, 只报告**互相错位**的真重叠。如果你的图设计是"卡片 A 的文字伸出去碰到卡片 B 的边", 那就是真重叠, 必报。

---

## 和 create_pptx_advanced 的迁移关系

- 本 skill 是 `create_pptx_advanced` 的**完整替代品**。
- 新 PPT 任务 → 用这个 skill。
- 看到 `create_pptx_advanced` 文档里的 declarative JSON spec 示例 → 忽略, 那套 silent-blank-slide 问题不值得再维护。

---

## 与 drawio-skill 组合 — 架构图嵌入 PPT

**触发场景**：用户要"方案介绍"、"架构汇报"、"设计评审"、"技术路演"等需要**架构图 + 文字说明**的 PPT。

**铁律 — 先画图，后写 deck。** 图的实际像素尺寸/aspect ratio 决定 slide 上嵌入的占位区。先随便用 placeholder 占位再写 deck，等真图出来 90% 概率 overflow 或留大白边。

**标准工作流：**

1. 调 `get_skill_guide("drawio-skill")` 生成 `.drawio` 源文件
2. 走 drawio-skill 的 Step 3.5 pre-flight + Step 4 export(注意是 `--width` 不是裸 `-s`) + Step 4.5 dimension check + Step 5 vision self-check
3. **拿到尺寸 OK 的 PNG 后**，再开始写 deck 脚本
4. 在脚本里用 `Pillow` 读图片实际宽高,按 slide 宽度反推等比例高度后嵌入:

```python
from PIL import Image
from pptx.util import Inches

DIAGRAM = "/abs/path/to/architecture.drawio.png"   # 用 drawio-skill 的 -e 输出, 双扩展名
img_w, img_h = Image.open(DIAGRAM).size
target_w_in = 11.5                                  # 13.33" 16:9 slide, 留 0.9" 左右 margin
target_h_in = target_w_in * img_h / img_w
x = Inches((13.333 - target_w_in) / 2)
y = Inches(1.5)                                      # 给上方 slide 标题留出空间
slide.shapes.add_picture(DIAGRAM, x, y, Inches(target_w_in), Inches(target_h_in))
```

5. 验证脚本时**专门 grep Picture shape**确认图嵌进去了:

```python
for i, s in enumerate(p.slides, 1):
    pics = [sh for sh in s.shapes if sh.shape_type == 13]   # MSO_SHAPE_TYPE.PICTURE
    if pics: print(f"  slide {i}: {len(pics)} picture(s) — sizes {[(p.width,p.height) for p in pics]}")
```

通过条件: 你预期有图的那一页 (整体架构页) 必须有至少 1 个 Picture shape, 否则 add_picture 路径错了 / 图被覆盖了。

**5–8 页方案介绍标准结构:**

| # | Slide | 内容 |
|---|-------|------|
| 1 | 封面 | 标题 / 副标题 / 日期 |
| 2 | 背景 & 目标 | 1-2 段问题陈述 + 3 KPI 卡片 |
| 3 | **整体架构** | **drawio PNG 占主区域** + 1 行 caption |
| 4 | 关键组件 | 2-3 个组件说明 (cards) |
| 5 | 数据/流量路径 | 第二张 drawio 图 (序列/流程类) |
| 6 | 选型对比 | 2-4 列对比表 (纯 python-pptx) |
| 7 | 落地计划 | 时间线/里程碑 (python-pptx 形状) |
| 8 | 总结 | 大字标语 + 联系方式 |

**关键: 一份图,两个用途。** 如果 drawio 用 `-e` 标志导出 (双扩展名 `.drawio.png`), 嵌入 PPT 的图同时也保留了完整可编辑的 .drawio XML —— 评审人右键 slide 上的图 → 另存 → 用 draw.io 打开能直接修, 不用单独留 `.drawio` 源文件。
- 发现用户的老工程里有 layout JSON → 直接翻译成上面模板里的 python 函数, 一对一, 不要再走 declarative 路径。
