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

## ⚠️ 命名铁律（抄代码前必看）

**所有 slide 变量和函数形参只叫一个名字：`slide`**。
不要自己改名成 `s` / `sl` / `slide_obj` / `sld` 等任何别名。

```python
# ✅ 正确
def slide_cover(prs):
    slide = prs.slides.add_slide(blank)
    set_bg(slide, THEME["bg"])
    add_text(slide, ..., "标题")

# ❌ 错误 —— LLM 常见错误：混用三种名字
def slide_cover(prs):
    s = prs.slides.add_slide(blank)       # 用了 s
    set_bg(slide, THEME["bg"])             # 又写了 slide → NameError
    add_text(sl, ..., "标题")              # 又冒出 sl → 再挂
```

为什么要这样：所有 helper（`set_bg`、`add_text`、`add_card`、`add_table`、
`header_bar`、`takeaway_band` 等）的第一个形参都叫 `slide`。当你在函数体
里用 `s` 时，就得每次调用 helper 都写 `set_bg(s, ...)` —— 很容易丢失
某个 `s` 没改对，变成 `set_bg(slide, ...)` NameError，或者反过来
`add_text(s, ...)` 在某些 helper 出错。**统一用 `slide` 就没有这类错误**。

如果你已经写成了 `s` 或 `sl`，不要**一行一行 grep 改**（容易漏；也是
产生 bug 的根源）。直接重写那个函数：把 `s = prs.slides.add_slide(blank)`
改成 `slide = prs.slides.add_slide(blank)`，然后函数里的 `.shapes` 调用
直接用 `slide.shapes`，helper 调用传 `slide`。

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
for i, slide in enumerate(p.slides, 1):
    shapes = list(slide.shapes)
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
    slide = prs.slides.add_slide(blank); set_bg(slide, THEME["bg"])
    add_text(slide, Inches(0.8), Inches(2.5), SW-Inches(1.6), Inches(1.2),
             "市场分析报告", size=48, bold=True, color=THEME["fg"])
    add_text(slide, Inches(0.8), Inches(3.8), SW-Inches(1.6), Inches(0.6),
             "2026 Q2 · 战略投研组", size=20, color=THEME["accent"])

def slide_cards(prs):
    slide = prs.slides.add_slide(blank); set_bg(slide, THEME["bg"])
    add_text(slide, Inches(0.6), Inches(0.4), SW-Inches(1.2), Inches(0.8),
             "核心发现", size=28, bold=True, color=THEME["fg"])
    cards = [
        ("市场规模", "2025 年区域市场达 $4.2B, YoY +18%, 预计 2028 年突破 $7B."),
        ("增长动力", "政策开放 + 企业云迁移加速 + 本地化合规推动三方协同."),
        ("关键风险", "美元汇率波动、地缘合规壁垒、渠道分发依赖单一 GSI."),
    ]
    cw, gap = Inches(3.9), Inches(0.3)
    for i, (t, b) in enumerate(cards):
        x = Inches(0.6) + i*(cw+gap)
        add_card(slide, x, Inches(1.8), cw, Inches(4.8), t, b)

def slide_closing(prs):
    slide = prs.slides.add_slide(blank); set_bg(slide, THEME["bg"])
    add_text(slide, Inches(0), Inches(2.8), SW, Inches(1.2), "谢谢观看",
             size=64, bold=True, color=THEME["accent"],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    add_text(slide, Inches(0), Inches(4.4), SW, Inches(0.6),
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
| 形状超出页面 | 用 Inches() 加出去了 | 跑 `check_bounds(path)`（见下一节"边界检查"），越界页和 shape 会被列出来 |
| 表格单元格样式不生效 | 忘了把 `cell.text` 的已存在 paragraph 重新改格式 | 用 `for p in cell.text_frame.paragraphs: for r in p.runs: ...` |
| `add_picture` 报 File not found | 图片路径相对而非绝对 | 一律用绝对路径, 或 `os.path.join(AGENT_WORKSPACE, ...)` |
| 图表没显示 | 忘了 `data.categories` 或 series values 长度不一致 | 确认 `len(values) == len(categories)` |
| 保存 .pptx 后 PowerPoint 打开报错 | 一般是 shape 边界越界或图片损坏 | 重新跑验证脚本逐页看 shape count, 定位出错页 |

---

## 边界检查（防止形状越界）

PowerPoint 保存 .pptx **不会**拒绝越界形状 —— 你可以把一个矩形的 top 放到 8 inch、宽度放到 20 inch，文件能存能打开，只是显示时掉出页面。所以生成后**必须跑一遍**：

```python
from pptx import Presentation
from pptx.util import Emu

EMU_PER_INCH = 914400

def check_bounds(pptx_path: str, tol_inch: float = 0.02) -> list[str]:
    """
    遍历所有 shape，报告 x/y/w/h 越界的元素。
    tol_inch 允许 0.02" (约 0.5mm) 的容差 —— 有些主题模板的装饰条
    天生卡在边上，不要误报。
    返回空 list 表示全部过关。
    """
    prs = Presentation(pptx_path)
    SW, SH = prs.slide_width, prs.slide_height
    tol = int(tol_inch * EMU_PER_INCH)
    issues = []
    for i, slide in enumerate(prs.slides, start=1):
        for shp in slide.shapes:
            x, y, w, h = shp.left or 0, shp.top or 0, shp.width or 0, shp.height or 0
            name = getattr(shp, "name", "") or str(shp.shape_type)
            if x < -tol or y < -tol:
                issues.append(
                    f"slide {i}: '{name}' 左上角越界 "
                    f"({x/EMU_PER_INCH:.2f}, {y/EMU_PER_INCH:.2f})"
                )
            if x + w > SW + tol:
                issues.append(
                    f"slide {i}: '{name}' 右边越界: right={((x+w)/EMU_PER_INCH):.2f}\" "
                    f"> slide_width={(SW/EMU_PER_INCH):.2f}\""
                )
            if y + h > SH + tol:
                issues.append(
                    f"slide {i}: '{name}' 下边越界: bottom={((y+h)/EMU_PER_INCH):.2f}\" "
                    f"> slide_height={(SH/EMU_PER_INCH):.2f}\""
                )
    return issues


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    issues = check_bounds(path)
    if issues:
        print(f"❌ {len(issues)} 处越界：", file=sys.stderr)
        for s in issues:
            print("  " + s, file=sys.stderr)
        sys.exit(2)
    print(f"✅ {path} 全部 shape 在页面内")
```

**用法**：build 脚本最后一步、或作为独立 `check_bounds.py` 脚本跑。

**越界怎么修**：看报告里哪一页、哪个 shape —— 通常是**累计 y 算错**（前面某个 block 实际高度 > 预期）。调整方法：
- 把那个超高 block 的 height 改小
- 或把后续元素整体上移
- 或拆成两页

**不要**用 tol 把越界藏起来，容差只给 shape 装饰条（贴边 accent strip）用。

---

## 质量门（声明完成前必须通过）

1. `python build_deck.py` 退出码 0，无 stderr 输出
2. 验证脚本输出的每一页 shape 数 ≥ 3（封面/结尾可 ≥ 2）
3. 没有 `BLANK` 标记
4. 文件路径在 `$AGENT_WORKSPACE` 或项目共享目录内（遵循 `safe-artifact-paths` skill）
5. **`python check_bounds.py <pptx_path>` 退出码 0**（所有 shape 在页面内）

**任何一项不过就不要说 "已生成 pptx"**——继续修脚本。

---

## 和 create_pptx_advanced 的迁移关系

- 本 skill 是 `create_pptx_advanced` 的**完整替代品**。
- 新 PPT 任务 → 用这个 skill。
- 看到 `create_pptx_advanced` 文档里的 declarative JSON spec 示例 → 忽略, 那套 silent-blank-slide 问题不值得再维护。

---

## Design Recipes — 成品级布局（直接抄，立刻不粗糙）

上面的 cheatsheet 教你**怎么画 shape**。这一章给你**画成什么样才像一份真正的报告**。

> **命名约定（抄代码前看这一行）**
> 所有 recipe 内的本地变量一律叫 `slide`，所有 helper (`add_text` / `set_bg` /
> `header_bar` / `takeaway_band`) 的第一个参数也叫 `slide`。抄代码时**不要**
> 手滑改成 `s`、`sl`、`slide_obj` 等短名 —— helper 内部还是用 `slide` 才能
> 对应上。只有在**极少数**同一函数里需要两个 slide（比如做 slide 复制）
> 才需要命名区分，否则永远就叫 `slide`。

**先看 ASCII 线框选 recipe**：

| Recipe | 线框 | 用途 |
|---|---|---|
| **R1 · Title Cover** | 深底 + 橙 accent bar + 3 张产品卡 | 封面 / 章节分隔 |
| **R2 · Three Intro** | 3 张并列卡片 | 产品介绍 / 并列概念 |
| **R3 · Comparison** | N×M 表格 + 一列高亮 + takeaway band | 对比矩阵 / 能力对齐 |
| **R4 · User Segments** | 3 张纵向人群卡 + 底部 pill | 目标人群 / 方案适用 |
| **R5 · Stat Dashboard** | 大数字 callout | 关键指标汇报 |

每个 recipe 都是独立 **`def slide_recipeX(prs, data):`** — 直接 copy 进 `build_deck.py`，按需改 `data` 字典即可。

### 配色三选一（脚本顶部挑一个赋给 THEME）

```python
# ═══ Ocean Gradient (深蓝 + 橙色) — 技术/架构报告首选 ═══
PALETTE_OCEAN = {
    "bg":        hex_color("#21295C"),  # navy 深蓝
    "bg_light":  hex_color("#E8F1F8"),  # ice 浅底（内容页）
    "deep":      hex_color("#065A82"),  # 表头深色
    "teal":      hex_color("#1C7293"),  # 辅助
    "fg":        hex_color("#0B1B2A"),  # 深底文字→白/浅底文字→此色
    "fg_light":  hex_color("#FFFFFF"),
    "accent":    hex_color("#F97316"),  # 橙色重点
    "muted":     hex_color("#6B7A8F"),
    "card_bg":   hex_color("#FFFFFF"),
    "card_pick": hex_color("#FFF3E6"),  # 高亮列背景
    "border":    hex_color("#CBD5E1"),
}

# ═══ Warm Terracotta (陶土 + 沙色) — 生活化 / 行业科普 ═══
PALETTE_TERRA = {
    "bg":        hex_color("#B85042"),
    "bg_light":  hex_color("#E7E8D1"),
    "deep":      hex_color("#8B3A2E"),
    "teal":      hex_color("#A7BEAE"),
    "fg":        hex_color("#2C1810"),
    "fg_light":  hex_color("#FFFFFF"),
    "accent":    hex_color("#E09F3E"),
    "muted":     hex_color("#A7BEAE"),
    "card_bg":   hex_color("#F7F3E9"),
    "card_pick": hex_color("#F5E6D3"),
    "border":    hex_color("#D4C5B0"),
}

# ═══ Berry & Cream (莓红 + 奶油) — 品牌 / 市场类 ═══
PALETTE_BERRY = {
    "bg":        hex_color("#6D2E46"),
    "bg_light":  hex_color("#FDF5F0"),
    "deep":      hex_color("#4A1F30"),
    "teal":      hex_color("#A26769"),
    "fg":        hex_color("#2A1520"),
    "fg_light":  hex_color("#FFFFFF"),
    "accent":    hex_color("#D4A017"),
    "muted":     hex_color("#A26769"),
    "card_bg":   hex_color("#FFFFFF"),
    "card_pick": hex_color("#F8E8EA"),
    "border":    hex_color("#D4B5BC"),
}

# 挑一个赋给 THEME，后面所有 recipe 都从 THEME 取色
THEME = PALETTE_OCEAN
```

### 通用 header bar（内容页一律用，保持视觉一致）

```python
def header_bar(slide, title, subtitle="", brand=""):
    """Dark navy strip + 橙色 accent strip + title/subtitle.
    封面不用它；内容页全用它。"""
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                  0, 0, SW, Inches(0.9))
    bar.fill.solid(); bar.fill.fore_color.rgb = THEME["bg"]
    bar.line.fill.background()
    strip = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                    0, Inches(0.9), SW, Inches(0.04))
    strip.fill.solid(); strip.fill.fore_color.rgb = THEME["accent"]
    strip.line.fill.background()
    add_text(slide, Inches(0.5), Inches(0.1), Inches(11), Inches(0.6),
             title, size=22, bold=True, color=THEME["fg_light"])
    if subtitle:
        add_text(slide, Inches(0.5), Inches(0.54), Inches(11), Inches(0.3),
                 subtitle, size=11, color=hex_color("#CADCFC"))
    if brand:
        add_text(slide, Inches(10.5), Inches(0.25), Inches(2.5), Inches(0.4),
                 brand, size=10, color=hex_color("#CADCFC"),
                 align=PP_ALIGN.RIGHT)
```

### 底部 takeaway band（核心结论 1 句话）

```python
def takeaway_band(slide, text, y=Inches(6.55)):
    """Dark rounded pill — 把全页最重要的一句话放这里，
    让受众一眼看到你想让他记住什么。"""
    band = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                   Inches(0.5), y,
                                   SW - Inches(1.0), Inches(0.55))
    band.fill.solid(); band.fill.fore_color.rgb = THEME["bg"]
    band.line.fill.background()
    add_text(slide, Inches(0.7), y, SW - Inches(1.4), Inches(0.55),
             text, size=12, color=THEME["fg_light"],
             anchor=MSO_ANCHOR.MIDDLE)
```

---

### Recipe 1 · Title Cover

```
┌─────────────────────────────────────┐
│ ┃ 超大标题（两行）                    │
│ ┃ ━ 副标题 (橙色)                     │
│ ┃ 斜体 teaser                         │
│                                      │
│ ┌─────┐ ┌─────┐ ┌─────┐             │
│ │卡片A│ │卡片B│ │卡片C│             │
│ └─────┘ └─────┘ └─────┘             │
└─────────────────────────────────────┘
```

```python
def slide_cover(prs, data):
    """
    data = {
      "title": "三大 AI Agent 平台对比",
      "subtitle": "Claude Code · OpenClaw · Tudou Claw",
      "teaser": "架构 · 能力 · 用户群",
      "cards": [
        {"name":"A","tag":"by X","badge":"CLI","desc":"..."},
        {"name":"B","tag":"OSS","badge":"Local","desc":"..."},
        {"name":"C","tag":"Self-Hosted","badge":"Multi-Agent","desc":"...",
         "featured": True},   # 被推荐 / 自家产品
      ],
    }
    """
    slide = prs.slides.add_slide(blank)
    set_bg(slide, THEME["bg"])
    # left accent bar
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                              Inches(0.5), Inches(1.1),
                              Inches(0.1), Inches(1.4))
    bar.fill.solid(); bar.fill.fore_color.rgb = THEME["accent"]
    bar.line.fill.background()
    add_text(slide, Inches(0.8), Inches(1.0), Inches(11.5), Inches(0.9),
             data["title"], size=40, bold=True, color=THEME["fg_light"])
    add_text(slide, Inches(0.8), Inches(1.85), Inches(11.5), Inches(0.5),
             data.get("subtitle", ""), size=22, color=THEME["accent"])
    if data.get("teaser"):
        add_text(slide, Inches(0.8), Inches(2.4), Inches(11.5), Inches(0.35),
                 data["teaser"], size=14, color=hex_color("#CADCFC"))

    cards = data.get("cards", [])[:3]
    card_w, card_h, y0 = Inches(3.9), Inches(3.8), Inches(3.1)
    for i, c in enumerate(cards):
        x = Inches(0.6) + i * (card_w + Inches(0.2))
        col = THEME["accent"] if c.get("featured") else THEME["teal"]
        fill = hex_color("#0F3460") if c.get("featured") else hex_color("#17284B")
        card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                    x, y0, card_w, card_h)
        card.fill.solid(); card.fill.fore_color.rgb = fill
        card.line.color.rgb = col
        card.line.width = Pt(2 if c.get("featured") else 1)
        # accent sliver
        sv = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                 x + Inches(0.25), y0 + Inches(0.35),
                                 Inches(0.4), Inches(0.05))
        sv.fill.solid(); sv.fill.fore_color.rgb = col
        sv.line.fill.background()
        add_text(slide, x + Inches(0.25), y0 + Inches(0.5),
                 card_w - Inches(0.5), Inches(0.55),
                 c["name"], size=22, bold=True, color=THEME["fg_light"])
        add_text(slide, x + Inches(0.25), y0 + Inches(1.05),
                 card_w - Inches(0.5), Inches(0.3),
                 c.get("tag", ""), size=11, color=hex_color("#CADCFC"))
        # badge pill
        pill = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                    x + Inches(0.25), y0 + Inches(1.45),
                                    Inches(1.6), Inches(0.35))
        pill.fill.solid(); pill.fill.fore_color.rgb = col
        pill.line.fill.background()
        add_text(slide, x + Inches(0.25), y0 + Inches(1.45),
                 Inches(1.6), Inches(0.35), c.get("badge", ""),
                 size=10, bold=True,
                 color=THEME["bg"] if c.get("featured") else THEME["fg_light"],
                 align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        add_text(slide, x + Inches(0.25), y0 + Inches(1.95),
                 card_w - Inches(0.5), Inches(1.7),
                 c.get("desc", ""), size=12, color=hex_color("#E8F1F8"))
```

---

### Recipe 2 · Three-column Intro (light)

用于内容页 · 3 张并列卡。最后一张带 featured=True 可自动用 accent 色突出。

```python
def slide_three_intro(prs, data):
    """
    data = {
      "title": "产品定位",
      "subtitle": "三家各自服务的用户画像",
      "cards": [{"name":"A","desc":"..."},
                {"name":"B","desc":"..."},
                {"name":"C","desc":"...","featured":True}],
    }
    """
    slide = prs.slides.add_slide(blank)
    set_bg(slide, THEME["bg_light"])
    header_bar(slide, data["title"], data.get("subtitle", ""))
    cards = data.get("cards", [])[:3]
    cw, ch, y0 = Inches(4.0), Inches(5.3), Inches(1.15)
    for i, c in enumerate(cards):
        x = Inches(0.55) + i * (cw + Inches(0.1))
        card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                    x, y0, cw, ch)
        card.fill.solid(); card.fill.fore_color.rgb = THEME["card_bg"]
        card.line.color.rgb = THEME["border"]; card.line.width = Pt(1)
        strip_col = THEME["accent"] if c.get("featured") else THEME["teal"]
        strip = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                     x, y0, cw, Inches(0.14))
        strip.fill.solid(); strip.fill.fore_color.rgb = strip_col
        strip.line.fill.background()
        add_text(slide, x + Inches(0.25), y0 + Inches(0.3),
                 cw - Inches(0.5), Inches(0.5),
                 c["name"], size=20, bold=True, color=THEME["deep"])
        add_text(slide, x + Inches(0.25), y0 + Inches(0.85),
                 cw - Inches(0.5), ch - Inches(1.1),
                 c.get("desc", ""), size=12, color=THEME["fg"])
```

---

### Recipe 3 · Comparison Matrix

```
┌──────────────────────────────────┐
│ Header bar                        │
├────┬────┬────┬────┬──────────────┤
│    │ A  │ B  │ C* │  ← C 列高亮  │
├────┼────┼────┼────┤              │
│行1 │... │... │... │              │
│行2 │... │... │... │              │
└────┴────┴────┴────┘              │
│ ╭━━━ 🎯 结论：... ━━━╮            │
└──────────────────────────────────┘
```

```python
def slide_comparison(prs, data):
    """
    data = {
      "title": "架构形态对比",
      "subtitle": "部署 · 技术栈 · 核心架构",
      "columns": ["", "Product A", "Product B", "Product C"],
      "rows": [
        ["部署形态", "CLI", "Daemon", "HTTP Server"],
        ["主语言",   "TS",   "TS",     "Python"],
        # ...
      ],
      "highlight_col_index": 3,   # 哪一列 (1-based+0) 用 accent 色高亮
      "takeaway": "🎯 三家架构分野清晰，C 面向企业协作",
    }
    """
    slide = prs.slides.add_slide(blank)
    set_bg(slide, THEME["bg_light"])
    header_bar(slide, data["title"], data.get("subtitle", ""))
    cols = data["columns"]
    rows = [cols] + data["rows"]
    n_cols, n_rows = len(cols), len(rows)
    col_x_defaults = [Inches(0.5), Inches(2.5), Inches(5.2),
                       Inches(8.0), Inches(10.8), Inches(12.83)]
    col_x = col_x_defaults[:n_cols + 1]
    row_h = Inches(0.76); y0 = Inches(1.15)
    hl = data.get("highlight_col_index", -1)
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            x, w = col_x[ci], col_x[ci + 1] - col_x[ci]
            y = y0 + ri * row_h
            is_header = ri == 0
            is_label  = ci == 0 and not is_header
            is_hl     = ci == hl and not is_header
            bg = (THEME["deep"]      if is_header else
                  THEME["card_pick"] if is_hl else
                  hex_color("#DCEAF5") if is_label else
                  THEME["card_bg"])
            rect = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, row_h)
            rect.fill.solid(); rect.fill.fore_color.rgb = bg
            rect.line.color.rgb = THEME["border"]; rect.line.width = Pt(0.5)
            fg = (THEME["fg_light"] if is_header else
                  THEME["accent"]   if is_hl else
                  THEME["fg"])
            add_text(slide, x + Inches(0.12), y + Inches(0.05),
                     w - Inches(0.24), row_h - Inches(0.1),
                     str(cell),
                     size=13 if is_header else 11,
                     bold=is_header or is_label,
                     color=fg, anchor=MSO_ANCHOR.MIDDLE)
    if data.get("takeaway"):
        takeaway_band(slide, data["takeaway"],
                      y=y0 + n_rows * row_h + Inches(0.15))
```

---

### Recipe 4 · User Segment Cards

3 张纵向人群卡 + 底部彩色 pill。

```python
def slide_user_segments(prs, data):
    """
    data = {
      "title": "目标用户群",
      "subtitle": "",
      "cards": [
        {"name":"Claude Code","who":"💻 开发者",
         "profile": ["使用终端编码的开发者",
                     "熟悉 git/shell/IDE 工作流",
                     "Claude API 用户"],
         "scene": "单人编码 · 审查 · 脚本自动化",
         "fit": "🎯 单人精细 coding",
         "featured": False},
        # ...最多 3 张, 最后一张带 featured=True 可以突出
      ],
    }
    """
    slide = prs.slides.add_slide(blank)
    set_bg(slide, THEME["bg_light"])
    header_bar(slide, data["title"], data.get("subtitle", ""))
    cards = data.get("cards", [])[:3]
    cw, ch, y0, gap = Inches(4.1), Inches(5.5), Inches(1.15), Inches(0.15)
    for i, c in enumerate(cards):
        x = Inches(0.5) + i * (cw + gap)
        color = THEME["accent"] if c.get("featured") else THEME["teal"]
        card_fill = THEME["card_pick"] if c.get("featured") else THEME["card_bg"]
        card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y0, cw, ch)
        card.fill.solid(); card.fill.fore_color.rgb = card_fill
        card.line.color.rgb = color
        card.line.width = Pt(2.5 if c.get("featured") else 1)
        strip = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y0, cw, Inches(0.18))
        strip.fill.solid(); strip.fill.fore_color.rgb = color
        strip.line.fill.background()
        add_text(slide, x + Inches(0.2), y0 + Inches(0.3),
                 cw - Inches(0.4), Inches(0.5),
                 c["name"], size=20, bold=True, color=color)
        add_text(slide, x + Inches(0.2), y0 + Inches(0.85),
                 cw - Inches(0.4), Inches(0.35),
                 c.get("who", ""), size=13, bold=True, color=THEME["fg"])
        div = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                   x + Inches(0.2), y0 + Inches(1.3),
                                   cw - Inches(0.4), Inches(0.02))
        div.fill.solid(); div.fill.fore_color.rgb = THEME["border"]
        div.line.fill.background()
        add_text(slide, x + Inches(0.2), y0 + Inches(1.4),
                 cw - Inches(0.4), Inches(0.3),
                 "用户画像", size=10, bold=True, color=THEME["muted"])
        bullets = "\n".join("• " + b for b in c.get("profile", []))
        add_text(slide, x + Inches(0.3), y0 + Inches(1.7),
                 cw - Inches(0.5), Inches(1.8),
                 bullets, size=11, color=THEME["fg"])
        add_text(slide, x + Inches(0.2), y0 + Inches(3.55),
                 cw - Inches(0.4), Inches(0.3),
                 "典型场景", size=10, bold=True, color=THEME["muted"])
        add_text(slide, x + Inches(0.2), y0 + Inches(3.85),
                 cw - Inches(0.4), Inches(0.8),
                 c.get("scene", ""), size=11, color=THEME["fg"])
        pill = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                    x + Inches(0.2), y0 + Inches(4.75),
                                    cw - Inches(0.4), Inches(0.55))
        pill.fill.solid(); pill.fill.fore_color.rgb = color
        pill.line.fill.background()
        add_text(slide, x + Inches(0.2), y0 + Inches(4.75),
                 cw - Inches(0.4), Inches(0.55), c.get("fit", ""),
                 size=11, bold=True, color=THEME["fg_light"],
                 align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
```

---

### Recipe 5 · Stat Dashboard

大数字 callout，适合放开场或季度复盘。

```python
def slide_stat_dashboard(prs, data):
    """
    data = {
      "title": "Q3 关键指标", "subtitle": "",
      "stats": [
        {"value": "+42%", "label": "MAU"},
        {"value": "$2.1M", "label": "ARR"},
        {"value": "94%", "label": "满意度"},
      ],
      "takeaway": "三项指标均超年度 OKR 完成率",
    }
    """
    slide = prs.slides.add_slide(blank)
    set_bg(slide, THEME["bg_light"])
    header_bar(slide, data["title"], data.get("subtitle", ""))
    stats = data.get("stats", [])
    n = max(len(stats), 1)
    cw = Inches(12.33 / n)
    y0 = Inches(2.2)
    for i, st in enumerate(stats):
        x = Inches(0.5) + i * cw
        col = st.get("color") or THEME["accent"]
        add_text(slide, x, y0, cw, Inches(2.2),
                 st["value"], size=72, bold=True, color=col,
                 align=PP_ALIGN.CENTER)
        add_text(slide, x, y0 + Inches(2.2), cw, Inches(0.5),
                 st.get("label", ""), size=14, color=THEME["muted"],
                 align=PP_ALIGN.CENTER)
    if data.get("takeaway"):
        takeaway_band(slide, data["takeaway"])
```

---

## 把 recipes 串起来（30 行出一份 5 页报告）

```python
#!/usr/bin/env python3
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
import os

# 1. paste hex_color / PALETTE_OCEAN, set THEME = PALETTE_OCEAN
# 2. paste add_text / set_bg / header_bar / takeaway_band
# 3. paste the recipes you need

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
SW, SH = prs.slide_width, prs.slide_height
blank = prs.slide_layouts[6]

# 4. build the deck
slide_cover(prs, {"title": "…", "subtitle": "…", "teaser": "…", "cards": [...]})
slide_comparison(prs, {"title": "…", "columns": [...], "rows": [...],
                        "highlight_col_index": 3, "takeaway": "…"})
slide_user_segments(prs, {"title": "…", "cards": [...]})

out = os.path.join(os.environ.get("AGENT_WORKSPACE", "."), "report.pptx")
prs.save(out); print("WROTE:", out)
```

**心法**：
- 3-5 页的报告选 **R1 封面 + R3 对比 + R4 人群 + R5 指标**，按这个组合基本不会难看
- 封面一定要 **配色 + accent bar + 3 张卡片** 三件套，否则像占位符
- 每页结尾的 **takeaway band** 是体面报告的核心 — 别把关键结论埋在正文里
- 内容页统一用 `header_bar()`，视觉一致度远比炫技的多样布局重要
- 发现用户的老工程里有 layout JSON → 直接翻译成上面模板里的 python 函数, 一对一, 不要再走 declarative 路径。
