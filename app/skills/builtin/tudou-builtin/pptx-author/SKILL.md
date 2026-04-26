---
name: pptx-author
description: Use when the user asks you to produce a PowerPoint (.pptx) file — presentation, slide deck, report, 产品介绍, 市场分析, 路演, 汇报, 会议纪要, PPT. Write a python-pptx script, run it with bash, and verify the output slide-by-slide. This replaces the declarative create_pptx_advanced tool (which has a silent-blank-slide failure mode). Triggers: 生成PPT, 生成pptx, 做一份PPT, slide deck, presentation, 幻灯片.
applicable_roles:
  - "coder"
  - "analyst"
  - "business-consultant"
scenarios:
  - "市场分析 PPT"
  - "路演汇报"
  - "会议纪要成稿"
  - "产品介绍文档"
metadata:
  source: tudou-builtin
  license: Apache-2.0
  tier: official
---

# pptx-author — 用模板系统生成 PPT (推荐) 或手写脚本 (兜底)

## 🛑 禁止行为 (会被一票拒绝)

写脚本时如果你的代码以这种开头:

```python
from pptx import Presentation       # ❌ 你绕过了模板系统
prs = Presentation()                 # ❌
prs.slide_layouts[6]                 # ❌  自己 add_shape 拼版
```

**立即停止重写**。这是"路径 B 手写脚本",**只有路径 A 不覆盖你需要的版式时才走**。
绝大多数 PPT (封面 + 章节 + 三栏 + 时间线 + 对比 + 数据 + Q&A 收尾) 都被
路径 A 的 15 个版式 × 10 套主题完全覆盖。

## ✅ 正确的开头 (路径 A)

```python
from pptx import Presentation
from _template_loader import render_from_md, list_themes, list_layouts

prs = Presentation()
prs.slide_width  = 12192000   # 16:9 (13.33")
prs.slide_height =  6858000

# 选主题 (一次决定所有 slide 的视觉)
THEME = "corporate"   # 或 navy_gold / minimal / tech_dark / ...

# 渲染每页 — 全部用模板
render_from_md(prs, f"{THEME}/T01_cover", params={"title": "...", "subtitle": "..."})
render_from_md(prs, f"{THEME}/T04_section_divider", params={"no": "01", "heading": "..."})
render_from_md(prs, f"{THEME}/T02_three_column", params={"title": "...", "columns": [...]})
# ... 更多 slide,全用 render_from_md

prs.save("report.pptx")
```

只有当 15 个版式都不合适 (比如要嵌入 Excel 透视图这种特殊场景),**才**回落到
"路径 B 手写脚本",并在脚本注释里写明"路径 A 不覆盖,故 fallback"。

## ⚠️ 防幻觉 — 别去找不存在的工具

**这个 skill 没有任何 CLI 脚本**。不存在 `md2pptx.sh` / `convert.sh` 这些。
**不要** `ls` / `find` 去找 — 找了也只是浪费轮次。

目录只有两个文件:

```
pptx-author/
├── SKILL.md             ← 你现在读的这份
├── _template_loader.py  ← 路径 A 的模板渲染引擎 (导入 render_from_md)
└── _pptx_helpers.py     ← 路径 B 的 python-pptx 封装 (路径 A 不够时才用)
```

**工作流(主路径 A,没有捷径)**:
1. 写一段 python 脚本 (例如 `build_report.py`)
2. 首行 `from _template_loader import render_from_md`
3. 用 render_from_md 调每个版式
4. `prs.save("report.pptx")` + 跑脚本验证

---

## 🎨 两条生成路径：模板系统 vs 手写脚本

从 v2 开始这个 skill 支持两种工作流,**优先用 A**：

### A. 模板系统（推荐）—— MD 模板 + 主题 + 参数

用预置的 **5 套视觉主题** × **5 种版式** 快速拼页,只填参数,不用自己布局。
每一页都经过 verify_slides 校验过布局规范,不会越界/空白/title-only。

```python
# 你在 bash 跑的脚本 —— 这是完整的最小示例
from _pptx_helpers import new_deck, verify_slides
from _template_loader import render_from_md, list_themes, list_layouts, describe_layout

prs = new_deck()

# 1. 封面
render_from_md(prs, "corporate/T01_cover", params={
    "title": "2026 年度经营分析",
    "subtitle": "从增长质量到价值创造：三大关键发现",
    "tag": "2026 Q2 | 经营分析报告",
})

# 2. 章节分隔
render_from_md(prs, "corporate/T04_section_divider", params={
    "no": "02",
    "heading": "增长质量诊断",
    "subtitle": "结构、效率与可持续性三维评估",
})

# 3. 2×2 要求网格
render_from_md(prs, "corporate/T06_requirement_grid", params={
    "title": "四大核心能力",
    "cells": [
        {"icon": "shield", "heading": "安全合规", "body": "全流程合规接入,端到端加密保护。"},
        {"icon": "chart",  "heading": "实时监控", "body": "全域指标秒级聚合,异常自动告警。"},
        {"icon": "zap",    "heading": "自动响应", "body": "预设策略自动执行,人机协同闭环。"},
        {"icon": "users",  "heading": "统一治理", "body": "单一管理入口,多团队权限清晰隔离。"},
    ],
})

# 4. 垂直时间线
render_from_md(prs, "corporate/T09_vertical_timeline", params={
    "title": "三步实施路径",
    "steps": [
        {"heading": "规划期", "body": "明确目标、团队与预算 (4-6 周)"},
        {"heading": "试点期", "body": "3-5 个业务场景验证 (2 个月)"},
        {"heading": "推广期", "body": "全量迁移并建立治理体系 (6-9 个月)"},
    ],
})

# 5. 收尾
render_from_md(prs, "corporate/T19_qa_closing", params={
    "title": "Q & A",
    "cta": "主动构建 — 赢得确定性",
    "contact": "2026 Q2 | contact@example.com",
})

prs.save("out.pptx")
verify_slides("out.pptx")
```

### 10 套视觉主题（只需换前缀 `{theme_id}/T**` 即可切换）

**商业/专业类**
| theme_id | 风格 | 主色 | 最合适场景 |
|---|---|---|---|
| `corporate` | 深蓝 + 橙,稳重专业 | `#1E3A5F` + `#E86C3A` | 咨询 / 战略 / 管理汇报 |
| `navy_gold` | 海军蓝 + 金,低调奢华 | `#0A2540` + `#C9A960` | 投资 / 路演 / 并购 / 财富管理 |
| `minimal` | 黑白极简 | `#000000` + 灰阶 | 产品 / 设计 / Keynote 风 |
| `editorial` | 米底 + 深棕,杂志风 | `#1F2937` + `#92400E` | 长研究 / 学术 / 出版物 |

**科技/创意类**
| theme_id | 风格 | 主色 | 最合适场景 |
|---|---|---|---|
| `tech_dark` | 近黑底 + 霓虹青 | `#0A0A0A` + `#06B6D4` | AI/ML 发布 / 开发者大会 / Keynote |
| `fresh_mint` | 薄荷 + 珊瑚,轻快 | `#10B981` + `#F97066` | 初创 / DevRel / SaaS |
| `vibrant_purple` | 亮紫 + 珊瑚粉,鲜艳 | `#7C3AED` + `#EC4899` | 创意公司 / 设计工作室 / 艺术 |

**特定场景**
| theme_id | 风格 | 主色 | 最合适场景 |
|---|---|---|---|
| `academic` | 浅灰 + 深蓝 + serif | `#1E3A8A` + `#991B1B` | 论文答辩 / 学术汇报 / 教学 |
| `news_red` | 经典红 + 深炭灰,强烈张力 | `#B91C1C` + `#F59E0B` | 新闻简报 / 品牌快报 / 媒体 / 体育 |
| `eco_green` | 深林绿 + 大地棕,厚重自然 | `#166534` + `#78350F` | 环保 / ESG / 可再生能源 / 农林 |

不确定用哪个? 调 `list_themes()` 看完整描述。快速选择参考:
- 严谨正式 → `corporate` / `navy_gold` / `academic` / `editorial`
- 轻快现代 → `fresh_mint` / `vibrant_purple` / `minimal`
- 视觉冲击 → `tech_dark` / `news_red`
- 专属场景 → `eco_green` (ESG) / `academic` (学术)

### 8 种版式（按场景选）

| 版式 id | 用途 | 参数要点 |
|---|---|---|
| `T01_cover` | 封面 / 首页 | `title`, (`subtitle`), (`tag`) |
| `T02_three_column` | **恰好 3 个**并列要点 (三大支柱) | `title`, `columns[3].{icon,heading,body}` |
| `T04_section_divider` | 章节分隔页 (大数字 + 标题) | `no`, `heading`, (`subtitle`) |
| `T06_requirement_grid` | **恰好 4 个**并列要点 2×2 网格 | `title`, `cells[4].{icon,heading,body}` |
| `T09_vertical_timeline` | 3-5 步流程 / 阶段 / 路线图 | `title`, `steps[3-5].{(no),heading,body}` |
| `T10_comparison` | A vs B / Before vs After 对比 | `title`, `left.{label,items}`, `right.{label,items}`, (`vs_text`) |
| `T15_chart_page` | 大图表 + 底部 takeaway | `title`, (`subtitle`), (`takeaway`)；然后 agent 往占位区 `(0.5, 1.3, 12.3, 4.8)` 插 chart |
| `T19_qa_closing` | 报告收尾 / Q&A 页 | `title`, (`cta`), (`contact`) |

需要详细参数约束(字段长度上限等)? 调 `describe_layout("T06_requirement_grid")`
返回完整 JSON schema。

### 选版式决策树（速查）

- **封面** → T01；**章节分隔** → T04；**收尾** → T19
- **3 个并列要点** → T02
- **4 个并列要点,2×2 网格** → T06
- **A vs B / Before vs After 对比** → T10
- **3-5 步有顺序的流程** → T09
- **需要强调 1 张图表 + 结论** → T15 (然后自己 add_bar_chart 填入占位区)
- **5+ 个要点 / 表格对比** → 回落到"路径 B. 手写脚本" (用 `add_card` / `add_styled_table`)

### 不知道用哪套主题? 直接问

```python
from _template_loader import recommend_theme

# 输入用户对任务的描述,返回打分排名的前 3 个主题
recommend_theme("帮我做 AI 大模型发布会 PPT")
# → [{"id":"tech_dark","score":4,"matched":["AI","模型","大模型","ai"], ...},
#    {"id":"minimal","score":1, "matched":["发布会"], ...}]
```

匹配机制: 每个主题在 `theme.yaml` 里声明了 `tags` 和 `trigger_keywords`,
函数对查询做**substring 命中计数**,命中最多的排第一。如果返回第一个
`score >= 2`,直接用。`score == 0` 说明查询很泛,不强推某套,你可以按场景判断或问用户。

### 混用: 模板 + 手写脚本同一份 .pptx

模板调用就是往 prs 加 slide,和你 `prs.slides.add_slide(blank)` 自己加的没区别。
**可以任意混用**:用模板搭整体骨架,遇到模板不覆盖的场景 (比如要放一张大柱状图)
就在同一个脚本里用 helper 手写一页,最后统一 save + verify。

### B. 手写脚本 — 仅审批后可用

> ⚠️ **路径 B 不再是默认 fallback。** 在写任何 `from pptx import` /
> `from _pptx_helpers import` 的自定义脚本前,必须先用
> `propose_skill` 工具向用户说明:为什么 15 layouts × 10 themes 不够,
> 你打算手写哪几页,期望产出什么。**等用户在 portal 批准后**才能动手。
> 不批准就只能用路径 A,信息不够就拆成多页或问用户。

**强制流程 (违反 = 步骤直接 fail_step)**:

1. 先认真过一遍 layout × 主题组合 — 大多数场景路径 A 已覆盖:
   - 封面 / 章节 / 收尾 → T01/T04/T19
   - 三栏 / 四宫格 / 6 卡片 → T02/T06/T23
   - 时间线 / 流程 → T09/T26
   - 对比 → T10
   - KPI 大数字 / 引言 / 表格 → T24/T25/T20
   - 图文混排 → T21/T22
2. 路径 A 不够时,**先调** `propose_skill(name="custom-pptx-X", description="为什么需要 + 计划做什么", reason="...")`
3. 等用户在 portal 审批返回 OK
4. 才能写自定义 python-pptx 脚本(用 `_pptx_helpers` 里的 helper)

**审批被拒时**: 拆需求,看能不能用现有 layout 多页组合达成。比如
"5 个并列卡 (T06 只支持 4)" → 拆成 T06(前 4) + T01(总结第 5) 的 2 页方案。

### 什么时候才"必须"路径 B (审批通常会 OK)

- 用户明确给的独特设计稿,任何主题/layout 都对不上
- 真的需要 native chart (柱状图/折线图,模板系统当前不支持)
- 复杂自定义对比页 (≥3 列并排)

### 什么时候**不**该走路径 B

- ❌ "我想多放 1 个 KPI" → 拆成 2 页 T24
- ❌ "颜色我想换一下" → 试 10 个主题全部
- ❌ "标题我想往左移一点" → 模板的位置就是规范的,不要偏

---

## 📐 单页布局硬规范（verify_slides 会检测）

生成出来的每一页都会经过 `verify_slides(strict=True)`，违反下列任意一条 → `SystemExit(2)` → bash ❌，你必须改。

1. **画布固定**：16:9，`SW = 13.33"`，`SH = 7.5"` —— 已由 `new_deck()` 设好，别自己改。
2. **不得空页**：`shapes == 0` 即 BLANK，失败。
3. **不得 title-only**：一页只有顶部横栏 + 标题文字（≤2 shapes 且首 shape 是 `(x=0, y=0, w≈SW, h<1.5")` 的横栏）即 TITLE_ONLY，失败。H3/H4 subsection 内容**必须**合进父级 H2 slide，不要单开空页。
4. **不得越出画布**：所有 shape 的 x/y/w/h 必须保证 `x ≥ -0.02"`、`y ≥ -0.02"`、`x+w ≤ SW+0.02"`、`y+h ≤ SH+0.02"`。
5. **0.3" 安全边距**：除了顶部/底部 1" 装饰带和全宽 decorative bar，所有内容 shape 必须在 `[0.3, SW-0.3] × [0.3, SH-0.3]` 内。
6. **一页最多 1 个图表**：两张柱状图挤一页 = 失败。想比较两组数据就用 multi-series line/bar 或拆成两页。
7. **字号合理范围**：`add_text(size=...)` 建议 14-32pt；size <10 或 >50 会 warn（标题 24-32，正文 14-16，divider 大数字 120-160 属于例外，不触发警告上限）。
8. **一页要点 ≤ 7 条**：超过会 warn。信息再多就拆页或做卡片网格。

### 4 种推荐布局（用现成 helper 就不会出错）

| 布局 | helper | 用途 |
|---|---|---|
| A. 标题 + 主内容 | `header_bar(prs, title)` + `add_text` / `add_bullets` / `add_card` | 要点页、说明页 |
| B. 标题 + 左文右图 | `header_bar` + `add_bullets`(左) + `add_bar_chart`(右) | 数据解读 |
| C. 标题 + 卡片网格 | `header_bar` + 多次 `add_card` | 3-6 个并列小节 |
| D. 标题 + 全屏图表/表格 | `slide_full_chart(prs, title, chart_builder)` | 数据主角页 |

对比页：用 `header_bar + 2 列 rounded rect + add_bullets` 或补一个 `slide_comparison` 模式的 SKILL.md 参考脚本。

---

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

**⭐ 必用 `_pptx_helpers` —— 一行导入，省掉 200 行样板**

脚本开头只要一行：

```python
from _pptx_helpers import *   # bash 工具已自动 PYTHONPATH，直接 import
```

`_pptx_helpers` 已经导出了：
- `Presentation` / `Inches` / `Pt` / `Emu` / `RGBColor` / `MSO_SHAPE` / `PP_ALIGN` / `MSO_ANCHOR` / `CategoryChartData` / `XL_CHART_TYPE`
- `THEME`（bg/fg/accent/accent2/muted/card_bg/ok/warn/bad...）/ `FONT` / `SW` / `SH`
- `new_deck()` / `blank_layout(prs)` / `hex_color("#XXXXXX")`
- `set_bg / add_text / add_rect / add_card / add_styled_table / add_bullets / add_bar_chart / add_line_chart / add_image`
- `header_bar(prs, title)` —— 一行创建带标题栏的新页
- `check_bounds(path)` / `verify_slides(path)` —— 质量门
- `strip_md / parse_md_outline` —— md → outline

**不要**再写 `from pptx.util import ...` 或 `from pptx.dml.color import ...` —— 会 shadow 掉上面导出的版本。**也不要**自己重新 `def set_bg`、重新 `THEME = {...}` —— 抄了就会漏字段、错拼 key。

完整最小脚本（30 行以内）见下面 "Reference scripts"。

### 3. 先语法检查，再跑脚本

```bash
cd "$AGENT_WORKSPACE"
# 先单独编译检查，能在 0.1s 内捕获 SyntaxError
python -m py_compile build_report.py || echo "SYNTAX ERROR - 先修语法再跑"
# 只有 py_compile 过了才真正执行
python build_report.py 2>&1
```

- **py_compile 失败** → 不要 bypass，不要 "重试"。翻到报错行号，**只修那行**。
  最常见的错误：`Inches(X]`、`Pt(12]`、`)]` — LLM 在长代码里会把 `)` 敲成 `]`。
  **批量扫描**：`grep -nE 'Inches\([0-9.]+\]|Pt\([0-9.]+\]|\)\]' build_report.py` 能一次找出所有此类错误。
- **退出码 0 且无 `Error` / `Traceback`** → 继续第 4 步
- **有 traceback** → 看**最后一行**报错 → 定位行号 → 改那一行 → **不要整个重写**（改错的地方，保留其他）
- **退出码非 0** → 绝对不能当成功上报。bash 工具现在会用 ❌ 标记，你必须解决

### 4. 逐页验证——这一步不可省（用 `verify_slides`）

调 helper 里的 `verify_slides(path)` 就行，不要再写内嵌 `python - <<'PY'`：

```bash
python -c "from _pptx_helpers import verify_slides; verify_slides('out.pptx')"
```

`verify_slides` 默认 strict 模式：只要有一页 BLANK（0 shape）或任何 shape 越出画布，就 `sys.exit(2)` —— bash 工具的 ❌ 会自动触发，你必须回第 3 步修。

### 4b. 旧版等价写法（仅供对照，不推荐）

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

### Markdown 报告 → 完整 deck（**从结构化 md 生成的首选模板，v2**）

**什么时候用这份**：你拿到一份带章节结构的 md 报告（`#` 标题、`##` 一级章节、`###` 二级小节、可选 `####` 子节、bullet、table），要转成 16:9 深色主题 deck。**不要自己从零写** —— copy 下面这份到工作目录存成 `md_to_deck.py`，跑 `python md_to_deck.py report.md out.pptx` 一键生成。

**v2 相比 v1 新增了 3 种专用 layout**（从手写版 brand deck 学来的）：

| Layout | 触发条件 | 视觉效果 |
|---|---|---|
| **section_divider**（强化版） | 每个 `##` 自动编号 | 左侧超大号 01/02/03（160pt）+ 右侧章节标题（40pt）+ 中间 accent 色分隔条 |
| **comparison**（2 列对比） | `###` 下恰好 2 个 `####`，每个 `####` 只含 bullet | 左右两列圆角矩形，左列 accent 色（青）+ 右列 accent2 色（橙），头部色条 + 边框，清晰的对比视觉 |
| **card_grid**（卡片网格） | `###` 下单一 bullet 块且每条都是 `**title**: body` 格式 | 2×2 / 2×3 圆角 card grid，每 card 有左侧 accent 色条 + bold 标题 + body |

**怎么用才能触发 comparison**（最有价值的 layout）：md 写成
```md
### 全球服务布局对比
#### AWS
- 33 个地理区域
- 105 个可用区

#### Azure
- 70+ 个区域
- 140+ 国家
```
→ 自动渲染成左右两列对比卡，不用自己写渲染代码。

**怎么用才能触发 card_grid**：
```md
### 人才结构特点
- **AWS TAM**: 技术客户经理，负责架构评审
- **Azure CSAM**: 客户成功经理，专注采用率
- **FastTrack**: 标准化上云路径
```
→ 自动渲染成 2×2 卡片网格。

**其他保留的 v1 特性**：
- 16:9 尺寸 + `THEME` 深色主题 + `Microsoft YaHei` 字体
- 自动 strip `**bold**` / `` `code` `` / `[link](url)` / `[1]` 等 md 语法
- 每张内容页最多 4 个 block 堆叠，高度均分防溢出
- 每张 slide 都有彩色背景（set_bg），无白底
- 表格样式（头部 accent 色 + 斑马条纹）
- cover 左侧 accent 竖条

```python
#!/usr/bin/env python3
"""md_to_deck.py (v2) — structured markdown -> 16:9 styled deck.

v2 adds three new slide layouts learned from hand-written brand decks:
  - slide_section_divider: big auto-numbered (01/02/...) + section title
  - slide_comparison:      2-column side-by-side with accent-color backing
                            triggered by a ### with exactly two #### children
  - slide_card_grid:       2x2 / 2x3 grid of rounded cards, triggered by a
                            bullet list whose items are all ``**title**: body``

Everything else (THEME, strip_md, parse_md base, basic content slide) is
unchanged from v1. Parser prefers the specialized layouts when their
shape matches; falls back to plain content slide.

Usage: python md_to_deck.py input.md output.pptx
"""
from __future__ import annotations
import os, re, sys

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR


def hex_color(s):
    s = s.lstrip("#")
    return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


THEME = {
    "bg":        hex_color("#0F172A"),
    "fg":        hex_color("#F8FAFC"),
    "accent":    hex_color("#22D3EE"),   # primary / left column
    "accent2":   hex_color("#F59E0B"),   # secondary / right column
    "muted":     hex_color("#94A3B8"),
    "card_bg":   hex_color("#1E293B"),
    "card_alt":  hex_color("#273449"),
    "row_alt":   hex_color("#273449"),
    "divider":   hex_color("#1E40AF"),
}
FONT = "Microsoft YaHei"


# ---------- md parser ----------
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITAL = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_REFS = re.compile(r"\[\d+\]")

# Matches "**title**: body" at start of a bullet → (title, body)
_CARD_ITEM = re.compile(r"^\s*\*\*(?P<title>[^*]+)\*\*\s*[:：]\s*(?P<body>.+)$")


def strip_md(s: str) -> str:
    s = _BOLD.sub(r"\1", s)
    s = _ITAL.sub(r"\1", s)
    s = _CODE.sub(r"\1", s)
    s = _LINK.sub(r"\1", s)
    s = _REFS.sub("", s)
    return s.strip()


def parse_md(text: str) -> dict:
    """Return {title, sections: [{title, subs: [{title, blocks, children}]}]}.

    blocks = list of {kind: 'p'|'bullets'|'table', ...}
    children = list of {title, blocks} when the ### has #### sub-sections
               (used for comparison layout detection).
    """
    lines = text.split("\n")
    title = ""
    sections = []
    cur_section = None
    cur_sub = None        # H3 subsection
    cur_child = None      # H4 sub-subsection under an H3

    def ensure_sub():
        nonlocal cur_section, cur_sub, cur_child
        if cur_section is None:
            cur_section = {"title": "", "subs": []}
            sections.append(cur_section)
        if cur_sub is None:
            cur_sub = {"title": "", "blocks": [], "children": []}
            cur_section["subs"].append(cur_sub)
        cur_child = None
        return cur_sub

    def target_blocks():
        """Where does a block belong? If we're inside an H4, append to its
        children entry; otherwise to the H3's blocks."""
        nonlocal cur_sub, cur_child
        ensure_sub()
        if cur_child is not None:
            return cur_child["blocks"]
        return cur_sub["blocks"]

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if line.startswith("# ") and not line.startswith("## "):
            title = strip_md(line[2:]); i += 1; continue

        if line.startswith("## "):
            cur_section = {"title": strip_md(line[3:]), "subs": []}
            sections.append(cur_section)
            cur_sub = None; cur_child = None
            i += 1; continue

        if line.startswith("### "):
            cur_sub = {"title": strip_md(line[4:]),
                       "blocks": [], "children": []}
            if cur_section is None:
                cur_section = {"title": "", "subs": []}
                sections.append(cur_section)
            cur_section["subs"].append(cur_sub)
            cur_child = None
            i += 1; continue

        if line.startswith("#### "):
            ensure_sub()
            cur_child = {"title": strip_md(line[5:]), "blocks": []}
            cur_sub["children"].append(cur_child)
            i += 1; continue

        # table (H3- or H4-scope)
        if (stripped.startswith("|") and i + 1 < len(lines)
                and re.match(r"^\s*\|[-:|\s]+\|\s*$", lines[i + 1])):
            headers = [strip_md(c) for c in stripped.strip("|").split("|")]
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append([strip_md(c) for c
                             in lines[j].strip().strip("|").split("|")])
                j += 1
            target_blocks().append(
                {"kind": "table", "headers": headers, "rows": rows})
            i = j; continue

        # bullets
        if stripped.startswith(("- ", "* ")) and not line.startswith("**"):
            items = []
            while (i < len(lines)
                   and lines[i].strip().startswith(("- ", "* "))):
                items.append(lines[i].strip()[2:])  # KEEP raw for **title** detection
                i += 1
            target_blocks().append({"kind": "bullets",
                                    "items_raw": items,
                                    "items": [strip_md(x) for x in items]})
            continue

        if not stripped or stripped == "---":
            i += 1; continue

        # paragraph
        paras = [line]; j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if (not nxt.strip() or nxt.startswith("#")
                    or nxt.strip().startswith(("- ", "* ", "|"))):
                break
            paras.append(nxt); j += 1
        target_blocks().append(
            {"kind": "p", "text": strip_md(" ".join(paras))})
        i = j
    return {"title": title, "sections": sections}


# ---------- shape helpers ----------
def set_bg(slide, c):
    f = slide.background.fill; f.solid(); f.fore_color.rgb = c


def add_text(slide, x, y, w, h, text, *, size=18, bold=False, color=None,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True; tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.08)
    tf.margin_top = tf.margin_bottom = Inches(0.04)
    p = tf.paragraphs[0]; p.alignment = align
    r = p.add_run(); r.text = text
    r.font.name = FONT; r.font.size = Pt(size); r.font.bold = bold
    r.font.color.rgb = color if color is not None else THEME["fg"]
    return tb


def add_rect(slide, x, y, w, h, fill, *, rounded=False,
             line_color=None, line_width_pt=0):
    kind = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
    sh = slide.shapes.add_shape(kind, x, y, w, h)
    sh.fill.solid(); sh.fill.fore_color.rgb = fill
    if line_color is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line_color
        if line_width_pt:
            sh.line.width = Pt(line_width_pt)
    return sh


def add_styled_table(slide, x, y, w, h, headers, rows):
    n_cols = max(len(headers), 1)
    n_rows = max(len(rows) + 1, 2)
    shape = slide.shapes.add_table(n_rows, n_cols, x, y, w, h)
    tbl = shape.table
    for c in range(n_cols):
        cell = tbl.cell(0, c)
        cell.text = headers[c] if c < len(headers) else ""
        cell.fill.solid(); cell.fill.fore_color.rgb = THEME["accent"]
        for p in cell.text_frame.paragraphs:
            for r in p.runs:
                r.font.bold = True; r.font.name = FONT
                r.font.size = Pt(12); r.font.color.rgb = THEME["bg"]
    for ri, row in enumerate(rows, start=1):
        for c in range(n_cols):
            cell = tbl.cell(ri, c)
            cell.text = row[c] if c < len(row) else ""
            cell.fill.solid()
            cell.fill.fore_color.rgb = (
                THEME["card_bg"] if ri % 2 == 0 else THEME["row_alt"])
            for p in cell.text_frame.paragraphs:
                for r in p.runs:
                    r.font.name = FONT; r.font.size = Pt(10)
                    r.font.color.rgb = THEME["fg"]


def add_bullets_inside(slide, x, y, w, h, items, *, size=14, color=None):
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame; tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.1)
    clr = color or THEME["fg"]
    for idx, item in enumerate(items):
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT; p.space_after = Pt(4)
        r = p.add_run(); r.text = "• " + item
        r.font.name = FONT; r.font.size = Pt(size)
        r.font.color.rgb = clr


# ---------- slide builders ----------
SW, SH = Inches(13.333), Inches(7.5)


def slide_cover(prs, title, subtitle=""):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(slide, THEME["bg"])
    add_rect(slide, 0, 0, Inches(0.35), SH, THEME["accent"])
    add_text(slide, Inches(1.2), Inches(2.7), Inches(11), Inches(1.6),
             title, size=40, bold=True, color=THEME["fg"], align=PP_ALIGN.LEFT)
    if subtitle:
        add_text(slide, Inches(1.2), Inches(4.5), Inches(11), Inches(0.7),
                 subtitle, size=18, color=THEME["muted"], align=PP_ALIGN.LEFT)


def slide_section_divider(prs, number: int, title: str):
    """Divider with big "01" / "02" on the left + section title on the right."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(slide, THEME["divider"])
    # Giant number "0N" — left side
    num_str = f"{number:02d}"
    add_text(slide, Inches(0.6), Inches(1.8), Inches(3), Inches(3.5),
             num_str, size=160, bold=True, color=THEME["accent"],
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE)
    # Section title — right side (wrapped if long)
    add_text(slide, Inches(4.2), Inches(2.8), Inches(8.5), Inches(2.5),
             title, size=40, bold=True, color=THEME["fg"],
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE)
    # Thin accent bar connecting them
    add_rect(slide, Inches(4.0), Inches(3.55), Inches(0.08), Inches(0.9),
             THEME["accent"])


def slide_title_bar(prs, title):
    """Shared helper: top title bar with text. Returns the slide."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(slide, THEME["bg"])
    add_rect(slide, 0, 0, SW, Inches(1.0), THEME["card_bg"])
    add_text(slide, Inches(0.6), Inches(0.15), Inches(12.1), Inches(0.7),
             title, size=24, bold=True, color=THEME["fg"],
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE)
    return slide


def slide_comparison(prs, title, left_title, left_items,
                     right_title, right_items):
    """Side-by-side comparison with accent-colored column backings."""
    slide = slide_title_bar(prs, title)
    # Left column — accent tint
    left_bg = add_rect(slide, Inches(0.4), Inches(1.25),
                       Inches(6.1), Inches(5.95),
                       hex_color("#123447"), rounded=True,
                       line_color=THEME["accent"], line_width_pt=2)
    add_rect(slide, Inches(0.4), Inches(1.25),
             Inches(6.1), Inches(0.5),
             THEME["accent"], rounded=True)  # header strip
    add_text(slide, Inches(0.5), Inches(1.28),
             Inches(5.9), Inches(0.45),
             left_title, size=18, bold=True, color=THEME["bg"],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    add_bullets_inside(slide, Inches(0.6), Inches(1.9),
                       Inches(5.9), Inches(5.2),
                       left_items, size=13)
    # Right column — accent2 tint
    right_bg = add_rect(slide, Inches(6.85), Inches(1.25),
                        Inches(6.1), Inches(5.95),
                        hex_color("#4A3520"), rounded=True,
                        line_color=THEME["accent2"], line_width_pt=2)
    add_rect(slide, Inches(6.85), Inches(1.25),
             Inches(6.1), Inches(0.5),
             THEME["accent2"], rounded=True)
    add_text(slide, Inches(6.95), Inches(1.28),
             Inches(5.9), Inches(0.45),
             right_title, size=18, bold=True, color=THEME["bg"],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    add_bullets_inside(slide, Inches(7.05), Inches(1.9),
                       Inches(5.9), Inches(5.2),
                       right_items, size=13)


def slide_card_grid(prs, title, cards):
    """2 or 3 column grid of rounded cards with a bold title + body."""
    slide = slide_title_bar(prs, title)
    n = len(cards)
    if n <= 0:
        return
    # Pick grid shape: 2x2 for 3-4, 2x3 for 5-6, 2x1 for 1-2
    if n <= 2:
        cols, rows = n, 1
    elif n <= 4:
        cols, rows = 2, 2
    else:
        cols, rows = 2, 3
    cards = cards[:cols * rows]
    area_top = Inches(1.25)
    area_h = Inches(6.0)
    area_left = Inches(0.4)
    area_w = Inches(12.55)
    gap = Inches(0.2)
    card_w = (area_w - gap * (cols - 1)) // cols
    card_h = (area_h - gap * (rows - 1)) // rows
    for i, (ctitle, cbody) in enumerate(cards):
        r = i // cols
        c = i % cols
        x = area_left + (card_w + gap) * c
        y = area_top + (card_h + gap) * r
        add_rect(slide, x, y, card_w, card_h, THEME["card_bg"], rounded=True)
        # accent strip on left
        add_rect(slide, x, y, Inches(0.1), card_h, THEME["accent"])
        # title
        add_text(slide, x + Inches(0.25), y + Inches(0.15),
                 card_w - Inches(0.4), Inches(0.5),
                 ctitle, size=15, bold=True, color=THEME["accent"])
        # body
        add_text(slide, x + Inches(0.25), y + Inches(0.7),
                 card_w - Inches(0.4), card_h - Inches(0.85),
                 cbody, size=12, color=THEME["fg"],
                 anchor=MSO_ANCHOR.TOP)


def slide_content(prs, title, blocks):
    """Plain content layout — title bar + up to 4 stacked blocks."""
    slide = slide_title_bar(prs, title)
    if not blocks:
        return
    num = min(len(blocks), 4)
    top = Inches(1.3); avail = SH - Inches(1.5); gap = Inches(0.2)
    slot_h = (avail - gap * (num - 1)) // num
    for i, b in enumerate(blocks[:num]):
        y = top + (slot_h + gap) * i
        _render_block(slide, Inches(0.6), y,
                      SW - Inches(1.2), slot_h, b)


def _render_block(slide, x, y, w, h, block):
    kind = block.get("kind")
    if kind == "bullets":
        add_rect(slide, x, y, w, h, THEME["card_bg"], rounded=True)
        add_bullets_inside(slide,
                           x + Inches(0.3), y + Inches(0.2),
                           w - Inches(0.6), h - Inches(0.4),
                           block.get("items", []))
    elif kind == "table":
        add_styled_table(slide, x, y, w, h,
                         block["headers"], block["rows"])
    elif kind == "p":
        add_rect(slide, x, y, w, h, THEME["card_bg"], rounded=True)
        add_text(slide,
                 x + Inches(0.3), y + Inches(0.2),
                 w - Inches(0.6), h - Inches(0.4),
                 block["text"], size=13, color=THEME["fg"],
                 anchor=MSO_ANCHOR.TOP)


def slide_closing(prs, message="谢谢"):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_bg(slide, THEME["divider"])
    add_text(slide, Inches(0), Inches(2.8), SW, Inches(1.9),
             message, size=72, bold=True, color=THEME["fg"],
             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)


# ---------- layout chooser ----------
def _detect_comparison(sub):
    """Return (left_title, left_items, right_title, right_items) when the
    H3 has exactly 2 H4 children and each H4 has a single bullet block.
    Else None."""
    kids = sub.get("children") or []
    if len(kids) != 2:
        return None
    cleaned = []
    for k in kids:
        blocks = k.get("blocks") or []
        if len(blocks) != 1 or blocks[0].get("kind") != "bullets":
            return None
        items = blocks[0].get("items") or []
        if not items:
            return None
        cleaned.append((k.get("title") or "", items))
    return (cleaned[0][0], cleaned[0][1],
            cleaned[1][0], cleaned[1][1])


def _detect_card_grid(sub):
    """Return [(title, body), ...] when sub has a single bullets block and
    every item matches `**title**: body`. Else None."""
    blocks = sub.get("blocks") or []
    if len(blocks) != 1 or blocks[0].get("kind") != "bullets":
        return None
    raw = blocks[0].get("items_raw") or []
    if len(raw) < 2:
        return None
    cards = []
    for line in raw:
        m = _CARD_ITEM.match(line.strip())
        if not m:
            return None
        cards.append((strip_md(m.group("title")),
                      strip_md(m.group("body"))))
    return cards


# ---------- main build ----------
def build(md_path, out_path):
    with open(md_path, encoding="utf-8") as f:
        doc = parse_md(f.read())
    prs = Presentation()
    prs.slide_width, prs.slide_height = SW, SH

    # Cover
    slide_cover(prs,
                doc["title"] or os.path.splitext(
                    os.path.basename(md_path))[0])

    # Sections
    section_no = 0
    for sec in doc["sections"]:
        if sec["title"]:
            section_no += 1
            slide_section_divider(prs, section_no, sec["title"])
        for sub in sec["subs"]:
            if not sub["title"] and not sub.get("blocks"):
                continue

            title = sub["title"] or sec["title"] or "Details"

            # Try specialized layouts in priority order
            cmp_data = _detect_comparison(sub)
            if cmp_data:
                slide_comparison(prs, title, *cmp_data)
                continue

            grid_data = _detect_card_grid(sub)
            if grid_data:
                slide_card_grid(prs, title, grid_data)
                continue

            # Fall back: render parent's own blocks first, then one content
            # slide per child H4 (so H4 content is not silently dropped when
            # the specialized layouts don't match).
            parent_blocks = sub.get("blocks") or []
            if parent_blocks:
                slide_content(prs, title, parent_blocks)
            for child in (sub.get("children") or []):
                child_blocks = child.get("blocks") or []
                if not child.get("title") and not child_blocks:
                    continue
                child_title = child.get("title") or title
                # Visually distinguish child slides by prefixing with parent
                # title when the child title is short.
                if title and child_title and title != child_title:
                    child_title = f"{title} — {child_title}"
                slide_content(prs, child_title, child_blocks)
            # If both parent and children were empty, still emit an empty
            # title slide so the H3 isn't silently swallowed.
            if not parent_blocks and not sub.get("children"):
                slide_content(prs, title, [])

    # Closing
    slide_closing(prs, "谢谢")

    prs.save(out_path)
    print(f"OK: {out_path} ({len(prs.slides)} slides)")
    return out_path


if __name__ == "__main__":
    md = sys.argv[1] if len(sys.argv) > 1 else "report.md"
    out = sys.argv[2] if len(sys.argv) > 2 else "deck.pptx"
    build(md, out)

```

**验证结果**（对一份 ~12k 字符的结构化中文报告）：

| 指标 | 从零裸写 python-pptx | md_to_deck v1 | md_to_deck v2 |
|------|---------------------|---------------|---------------|
| 尺寸 | 10×7.5（4:3） | 13.33×7.5（16:9） | **13.33×7.5（16:9）** |
| 总 slide 数 | 39 | 90 | 91 |
| 形状 / slide | 1.7 | 5.7 | **3.8（更紧凑）** |
| 专用 layout | 无 | 无 | **comparison + card_grid + 编号 divider** |
| Brand 色 | 手写才有 | 单色主题 | **accent + accent2 双色可配对** |
| 手动内容硬编码 | 是 | 否 | 否 |

v2 的平均 shape/slide 比 v1 低，是因为 comparison/card_grid 占一张 slide 就把原来要多张堆的内容聚合了，**每张 slide 更致密、视觉更丰富**，总信息量不降反升。

**扩展建议**：
- 需要画图表？在 `_render_block` 里多加 `kind == "chart"` 分支，调 cheatsheet 里的 `add_bar_chart`。你得在 `parse_md` 里识别"可画图"的数据块。
- 跳过某些章节？在 `build()` 里按 `sec["title"]` 过滤。
- 换主题？改 `THEME` 字典 —— `accent` / `accent2` 这对色决定了 comparison 左右列的视觉区分。换成品牌色对（如 AWS 橙 + Azure 蓝）立刻得到品牌 deck。
- 关掉 comparison / card_grid 自动识别？删 `_detect_comparison` / `_detect_card_grid` 的调用即可。

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
