"""
workflow_catalog.py — 企业级工作流模板 Catalog。

预置常见业务场景的 WorkflowTemplate，用户可一键使用并按需调整。
每个模板的 steps 遵循 StepTemplate 结构，支持 DAG 依赖。

分类：
  1. 产品研发 (Product Development)
  2. 内容创作 (Content Creation)
  3. 项目管理 (Project Management)
  4. 质量保障 (Quality Assurance)
  5. 运维部署 (DevOps)
  6. 数据分析 (Data & Analytics)
  7. 客户支持 (Customer Support)
  8. 人力资源 (HR)
  9. 市场营销 (Marketing)
  10. 安全合规 (Security & Compliance)

Phase 2 P2-7 (2026-05-06) — Deliverable contract pattern:

  Each step CAN declare a machine-checkable contract via these fields
  (see app/workflow.py StepTemplate + app/core/deliverable_check.py):

      "output_files":    ["spec.md", "src/api.py"],   # required outputs
      "must_contain":    ["## API", "FastAPI"],       # substrings (re: prefix = regex)
      "must_contain_per_file": {                       # per-file overrides
          "src/api.py": ["@app.get", "uvicorn.run"],
      },
      "min_lines": 30,                                 # per-output_file
      "max_lines": 0,                                  # 0 = unlimited
      "acceptance_cmd": "pytest tests/api.py",         # whitelisted bins only
      "acceptance_expect_exit": 0,

  Without a contract, the step's task_complete is unrestricted (legacy
  behavior). With a contract, the framework auto-verifies after each
  write_file and refuses task_complete until every output_file passes.

  Currently filled: 产品开发全流程 — s_architecture / s_development /
  s_testing. Other workflows ship without contracts; users add them
  when they want stricter verification.
"""

from typing import Any

# ---------------------------------------------------------------------------
# Catalog 数据
# ---------------------------------------------------------------------------

WORKFLOW_CATALOG: list[dict[str, Any]] = [

    # =====================================================================
    # 1. 产品研发 — 完整产品开发生命周期
    # =====================================================================
    {
        "id": "catalog_product_dev",
        "name": "产品开发全流程",
        "description": "从立项到上线的完整产品开发流程：需求分析 → 架构设计 → 开发实现 → 测试验证 → 部署上线 → 复盘总结",
        "category": "产品研发",
        "icon": "🚀",
        "tags": ["产品", "研发", "全流程", "敏捷"],
        "steps": [
            {
                "id": "s_initiation",
                "name": "项目立项",
                "description": "明确项目目标、范围、干系人、里程碑和资源需求",
                "prompt_template": (
                    "作为项目经理，请完成项目立项分析：\n"
                    "项目需求: {input}\n\n"
                    "请输出：\n"
                    "1. 项目目标（SMART 原则）\n"
                    "2. 范围定义（包含/不包含）\n"
                    "3. 关键干系人\n"
                    "4. 初步里程碑计划\n"
                    "5. 资源需求估算\n"
                    "6. 风险预评估"
                ),
                "input_spec": "项目需求描述或产品愿景",
                "output_spec": "项目立项文档（含目标、范围、计划、风险）",
                "suggested_role": "pm",
                "depends_on": [],
            },
            {
                "id": "s_requirements",
                "name": "需求分析",
                "description": "细化功能需求和非功能需求，产出 PRD",
                "prompt_template": (
                    "基于项目立项文档，进行详细需求分析：\n\n"
                    "{context}\n\n"
                    "请输出：\n"
                    "1. 用户故事（User Stories）列表\n"
                    "2. 功能需求清单（按优先级 P0/P1/P2 分级）\n"
                    "3. 非功能需求（性能、安全、可用性）\n"
                    "4. 用户流程图描述\n"
                    "5. 验收标准（Acceptance Criteria）"
                ),
                "input_spec": "项目立项文档",
                "output_spec": "PRD (Product Requirements Document)",
                "suggested_role": "pm",
                "depends_on": ["s_initiation"],
            },
            {
                "id": "s_architecture",
                "name": "架构设计",
                "description": "系统架构设计、技术选型、接口定义",
                "prompt_template": (
                    "基于需求文档，进行系统架构设计：\n\n"
                    "{context}\n\n"
                    "请输出：\n"
                    "1. 系统架构图描述（组件、服务划分）\n"
                    "2. 技术选型及理由\n"
                    "3. 数据模型设计\n"
                    "4. API 接口定义\n"
                    "5. 部署架构\n"
                    "6. 技术风险及应对方案"
                ),
                "input_spec": "PRD 文档",
                "output_spec": "技术架构设计文档",
                "suggested_role": "architect",
                "depends_on": ["s_requirements"],
                # Phase 2 P2-7 (2026-05-06): deliverable contract
                "output_files": ["architecture-design.md"],
                "must_contain": [
                    "## 系统架构", "## 技术选型", "## 数据模型",
                    "## API", "## 部署", "## 风险",
                ],
                "min_lines": 60,
            },
            {
                "id": "s_development",
                "name": "开发实现",
                "description": "按架构设计进行编码实现，含代码审查",
                "prompt_template": (
                    "基于架构设计文档，进行开发实现：\n\n"
                    "{context}\n\n"
                    "请按以下步骤执行：\n"
                    "1. 拆分开发任务（Task Breakdown）\n"
                    "2. 实现核心功能代码\n"
                    "3. 编写单元测试\n"
                    "4. 代码自审（Code Self-Review）\n"
                    "5. 输出开发完成报告"
                ),
                "input_spec": "技术架构设计文档",
                "output_spec": "代码实现 + 单元测试 + 开发报告",
                "suggested_role": "coder",
                "depends_on": ["s_architecture"],
                # Phase 2 P2-7 (2026-05-06): deliverable contract
                # NOTE: paths are relative to the project's shared
                # workspace. Replace with the project's actual entry
                # point file when binding the workflow.
                "output_files": ["development-report.md"],
                "must_contain": [
                    "## 任务拆分", "## 核心实现",
                    "## 单元测试", "## 自审",
                ],
                "min_lines": 50,
            },
            {
                "id": "s_testing",
                "name": "测试验证",
                "description": "功能测试、集成测试、性能测试",
                "prompt_template": (
                    "基于需求和开发成果，进行全面测试：\n\n"
                    "{context}\n\n"
                    "请完成：\n"
                    "1. 测试用例设计（覆盖所有验收标准）\n"
                    "2. 功能测试执行\n"
                    "3. 边界条件和异常测试\n"
                    "4. 性能/压力测试（如适用）\n"
                    "5. 测试报告（通过率、缺陷列表、严重度分级）"
                ),
                "input_spec": "PRD + 开发代码",
                "output_spec": "测试报告（含通过率、缺陷列表）",
                "suggested_role": "tester",
                "depends_on": ["s_development"],
                # Phase 2 P2-7: deliverable contract
                "output_files": ["test-report.md"],
                "must_contain": [
                    "## 测试用例", "## 通过率", "## 缺陷列表",
                ],
                "min_lines": 30,
            },
            {
                "id": "s_deploy",
                "name": "部署上线",
                "description": "部署到生产环境，含发布检查清单",
                "prompt_template": (
                    "准备部署上线：\n\n"
                    "{context}\n\n"
                    "请完成：\n"
                    "1. 发布前检查清单（Checklist）\n"
                    "2. 部署步骤文档\n"
                    "3. 回滚方案\n"
                    "4. 监控告警配置\n"
                    "5. 发布公告草稿"
                ),
                "input_spec": "测试通过的代码 + 测试报告",
                "output_spec": "部署文档 + 发布检查清单 + 回滚方案",
                "suggested_role": "devops",
                "depends_on": ["s_testing"],
            },
            {
                "id": "s_retrospective",
                "name": "项目复盘",
                "description": "项目回顾、经验总结、改进建议",
                "prompt_template": (
                    "对整个项目进行复盘总结：\n\n"
                    "{context}\n\n"
                    "请从以下维度总结：\n"
                    "1. 目标达成度\n"
                    "2. 做得好的方面（Keep）\n"
                    "3. 需要改进的方面（Improve）\n"
                    "4. 新尝试（Try）\n"
                    "5. 经验教训\n"
                    "6. 后续行动项"
                ),
                "input_spec": "项目全过程记录",
                "output_spec": "复盘报告（KIT 格式）",
                "suggested_role": "pm",
                "depends_on": ["s_deploy"],
            },
        ],
    },

    # =====================================================================
    # 2. 产品需求评审（轻量级）
    # =====================================================================
    {
        "id": "catalog_req_review",
        "name": "需求评审流程",
        "description": "对产品需求进行多角色评审：产品评审 → 技术可行性 → 设计评审 → 最终定稿",
        "category": "产品研发",
        "icon": "📋",
        "tags": ["需求", "评审", "PRD"],
        "steps": [
            {
                "id": "s_prd_draft",
                "name": "需求初稿",
                "description": "产品经理撰写需求初稿",
                "prompt_template": (
                    "请根据以下需求描述，撰写结构化的 PRD 初稿：\n\n"
                    "{input}\n\n"
                    "包含：背景、目标用户、功能清单、用户流程、优先级排序"
                ),
                "input_spec": "需求描述或用户反馈",
                "output_spec": "PRD 初稿",
                "suggested_role": "pm",
                "depends_on": [],
            },
            {
                "id": "s_tech_feasibility",
                "name": "技术可行性评估",
                "description": "技术团队评估实现难度、工期、技术风险",
                "prompt_template": (
                    "请对以下需求进行技术可行性评估：\n\n"
                    "{context}\n\n"
                    "评估：实现难度（高/中/低）、预估工期、技术风险、所需技术栈、依赖项"
                ),
                "input_spec": "PRD 初稿",
                "output_spec": "技术可行性报告",
                "suggested_role": "architect",
                "depends_on": ["s_prd_draft"],
            },
            {
                "id": "s_design_review",
                "name": "设计评审",
                "description": "UX/UI 设计评审，交互流程确认",
                "prompt_template": (
                    "请对以下需求进行用户体验设计评审：\n\n"
                    "{context}\n\n"
                    "评审：交互流程合理性、UI 规范一致性、可用性问题、设计建议"
                ),
                "input_spec": "PRD 初稿 + 技术评估",
                "output_spec": "设计评审意见",
                "suggested_role": "designer",
                "depends_on": ["s_prd_draft"],
            },
            {
                "id": "s_final_prd",
                "name": "需求定稿",
                "description": "综合各方评审意见，产出最终 PRD",
                "prompt_template": (
                    "请综合以下评审意见，产出最终版 PRD：\n\n"
                    "{context}\n\n"
                    "需要整合技术可行性评估和设计评审的反馈，调整优先级和范围"
                ),
                "input_spec": "PRD 初稿 + 技术评估 + 设计评审",
                "output_spec": "最终 PRD",
                "suggested_role": "pm",
                "depends_on": ["s_tech_feasibility", "s_design_review"],
            },
        ],
    },

    # =====================================================================
    # 3. 代码审查流程
    # =====================================================================
    {
        "id": "catalog_code_review",
        "name": "代码审查流程",
        "description": "规范化代码审查：代码扫描 → 安全审查 → 同行评审 → 修复确认",
        "category": "产品研发",
        "icon": "🔍",
        "tags": ["代码", "审查", "质量", "安全"],
        "steps": [
            {
                "id": "s_static_analysis",
                "name": "静态代码分析",
                "description": "自动化代码扫描，检查代码质量和规范",
                "prompt_template": (
                    "请对以下代码进行静态分析：\n\n"
                    "{input}\n\n"
                    "检查：代码规范、命名约定、复杂度、重复代码、潜在 Bug"
                ),
                "input_spec": "代码文件或 PR diff",
                "output_spec": "静态分析报告",
                "suggested_role": "coder",
                "depends_on": [],
            },
            {
                "id": "s_security_review",
                "name": "安全审查",
                "description": "检查安全漏洞、敏感信息泄露风险",
                "prompt_template": (
                    "请对以下代码进行安全审查：\n\n"
                    "{context}\n\n"
                    "检查：SQL 注入、XSS、CSRF、敏感信息硬编码、权限漏洞、依赖漏洞"
                ),
                "input_spec": "代码文件",
                "output_spec": "安全审查报告",
                "suggested_role": "security",
                "depends_on": ["s_static_analysis"],
            },
            {
                "id": "s_peer_review",
                "name": "同行评审",
                "description": "资深开发者进行逻辑审查和最佳实践评估",
                "prompt_template": (
                    "请对以下代码进行同行评审：\n\n"
                    "{context}\n\n"
                    "评审：架构合理性、设计模式、边界处理、错误处理、可维护性、性能"
                ),
                "input_spec": "代码 + 静态分析报告",
                "output_spec": "评审意见和改进建议",
                "suggested_role": "senior_coder",
                "depends_on": ["s_static_analysis"],
            },
            {
                "id": "s_fix_confirm",
                "name": "修复确认",
                "description": "确认所有审查意见已处理",
                "prompt_template": (
                    "请检查以下审查意见是否已全部处理：\n\n"
                    "{context}\n\n"
                    "输出：各条审查意见的处理状态（已修复/已解释/延后/拒绝）、最终结论"
                ),
                "input_spec": "审查意见 + 修复后的代码",
                "output_spec": "审查通过确认",
                "suggested_role": "coder",
                "depends_on": ["s_security_review", "s_peer_review"],
                # Phase 3 P3-6 (2026-05-06): deliverable contract
                "output_files": ["code-review-report.md"],
                "must_contain": [
                    "## 静态分析", "## 安全审查",
                    "## 同行评审", "## 修复确认",
                ],
                "min_lines": 30,
            },
        ],
    },

    # =====================================================================
    # 4. 内容创作流程
    # =====================================================================
    {
        "id": "catalog_content_creation",
        "name": "内容创作与发布",
        "description": "从选题到发布的完整内容创作流程：选题策划 → 内容撰写 → 编辑审核 → SEO 优化 → 发布",
        "category": "内容创作",
        "icon": "✍️",
        "tags": ["内容", "文章", "博客", "公众号"],
        "steps": [
            {
                "id": "s_topic",
                "name": "选题策划",
                "description": "确定内容主题、目标受众、关键词",
                "prompt_template": (
                    "请根据以下方向进行内容选题策划：\n\n"
                    "{input}\n\n"
                    "输出：3-5 个选题方案，每个包含：标题、目标受众、核心关键词、"
                    "预期效果、内容大纲"
                ),
                "input_spec": "内容方向或领域",
                "output_spec": "选题方案列表",
                "suggested_role": "writer",
                "depends_on": [],
            },
            {
                "id": "s_draft",
                "name": "内容撰写",
                "description": "撰写初稿",
                "prompt_template": (
                    "请根据确定的选题撰写内容初稿：\n\n"
                    "{context}\n\n"
                    "要求：结构清晰、观点鲜明、数据支撑、配图建议、字数 2000-3000"
                ),
                "input_spec": "选题方案",
                "output_spec": "内容初稿",
                "suggested_role": "writer",
                "depends_on": ["s_topic"],
            },
            {
                "id": "s_edit",
                "name": "编辑审核",
                "description": "语法检查、事实核查、风格统一",
                "prompt_template": (
                    "请对以下文章进行编辑审核：\n\n"
                    "{context}\n\n"
                    "检查：语法错误、事实准确性、逻辑连贯性、"
                    "品牌调性一致性、标点和格式规范\n"
                    "输出修改标注和修改后的文章"
                ),
                "input_spec": "内容初稿",
                "output_spec": "编辑后的文章 + 审核意见",
                "suggested_role": "editor",
                "depends_on": ["s_draft"],
            },
            {
                "id": "s_seo",
                "name": "SEO 优化",
                "description": "关键词优化、标题优化、摘要优化",
                "prompt_template": (
                    "请对以下文章进行 SEO 优化：\n\n"
                    "{context}\n\n"
                    "优化：标题（含关键词、吸引力）、Meta Description、"
                    "关键词密度、内链建议、图片 ALT 标签"
                ),
                "input_spec": "编辑后的文章",
                "output_spec": "SEO 优化后的文章 + 优化报告",
                "suggested_role": "seo",
                "depends_on": ["s_edit"],
            },
            {
                "id": "s_publish",
                "name": "发布准备",
                "description": "最终检查并准备发布",
                "prompt_template": (
                    "请对以下文章进行最终发布准备：\n\n"
                    "{context}\n\n"
                    "检查清单：格式排版、图片配置、分类标签、"
                    "发布时间建议、多平台适配方案"
                ),
                "input_spec": "SEO 优化后的文章",
                "output_spec": "发布就绪的文章 + 发布清单",
                "suggested_role": "writer",
                "depends_on": ["s_seo"],
                # Phase 3 P3-6 (2026-05-06): deliverable contract
                "output_files": ["article-final.md", "publish-checklist.md"],
                "must_contain_per_file": {
                    "publish-checklist.md": [
                        "## 格式", "## 图片", "## 分类", "## 发布时间",
                    ],
                },
                "min_lines": 20,
            },
        ],
    },

    # =====================================================================
    # 5. Bug 修复流程
    # =====================================================================
    {
        "id": "catalog_bug_fix",
        "name": "Bug 修复流程",
        "description": "标准化 Bug 处理流程：问题分析 → 根因定位 → 修复方案 → 实施修复 → 回归测试",
        "category": "质量保障",
        "icon": "🐛",
        "tags": ["Bug", "修复", "调试", "回归"],
        "steps": [
            {
                "id": "s_bug_analysis",
                "name": "问题分析",
                "description": "分析 Bug 报告，复现问题，确认影响范围",
                "prompt_template": (
                    "请分析以下 Bug 报告：\n\n"
                    "{input}\n\n"
                    "输出：复现步骤、影响范围、严重等级（P0-P3）、影响用户数估计"
                ),
                "input_spec": "Bug 报告（含复现步骤、截图）",
                "output_spec": "问题分析报告",
                "suggested_role": "tester",
                "depends_on": [],
            },
            {
                "id": "s_root_cause",
                "name": "根因定位",
                "description": "定位 Bug 的根本原因",
                "prompt_template": (
                    "请根据问题分析进行根因定位：\n\n"
                    "{context}\n\n"
                    "定位：相关代码位置、问题成因、触发条件、"
                    "关联问题（是否是系统性问题）"
                ),
                "input_spec": "问题分析报告",
                "output_spec": "根因分析报告",
                "suggested_role": "coder",
                "depends_on": ["s_bug_analysis"],
            },
            {
                "id": "s_fix_plan",
                "name": "修复方案",
                "description": "制定修复方案和风险评估",
                "prompt_template": (
                    "请根据根因分析制定修复方案：\n\n"
                    "{context}\n\n"
                    "输出：修复方案（可能多个）、风险评估、影响范围、是否需要数据迁移"
                ),
                "input_spec": "根因分析报告",
                "output_spec": "修复方案文档",
                "suggested_role": "coder",
                "depends_on": ["s_root_cause"],
            },
            {
                "id": "s_implement_fix",
                "name": "实施修复",
                "description": "编写修复代码",
                "prompt_template": (
                    "请按照修复方案进行代码修复：\n\n"
                    "{context}\n\n"
                    "要求：最小改动原则、添加防御性代码、编写针对性测试"
                ),
                "input_spec": "修复方案",
                "output_spec": "修复代码 + 测试",
                "suggested_role": "coder",
                "depends_on": ["s_fix_plan"],
            },
            {
                "id": "s_regression_test",
                "name": "回归测试",
                "description": "验证修复有效性，确保无新问题引入",
                "prompt_template": (
                    "请对修复后的代码进行回归测试：\n\n"
                    "{context}\n\n"
                    "测试：原始 Bug 是否修复、相关功能是否正常、"
                    "性能是否受影响、是否引入新问题"
                ),
                "input_spec": "修复代码",
                "output_spec": "回归测试报告",
                "suggested_role": "tester",
                "depends_on": ["s_implement_fix"],
                # Phase 3 P3-6 (2026-05-06): deliverable contract
                "output_files": ["regression-report.md"],
                "must_contain": [
                    "## 原始 Bug 验证", "## 相关功能",
                    "## 性能影响", "## 结论",
                ],
                "min_lines": 25,
            },
        ],
    },

    # =====================================================================
    # 6. CI/CD 发布流程
    # =====================================================================
    {
        "id": "catalog_cicd_release",
        "name": "CI/CD 发布流程",
        "description": "标准化发布流程：构建 → 测试 → 灰度发布 → 全量上线 → 监控验证",
        "category": "运维部署",
        "icon": "🔄",
        "tags": ["CI/CD", "发布", "部署", "灰度"],
        "steps": [
            {
                "id": "s_build",
                "name": "构建打包",
                "description": "代码编译、依赖安装、产物打包",
                "prompt_template": (
                    "请执行构建流程：\n\n"
                    "{input}\n\n"
                    "步骤：环境检查、依赖安装、编译构建、"
                    "产物验证、版本号更新"
                ),
                "input_spec": "代码分支和版本信息",
                "output_spec": "构建产物 + 构建报告",
                "suggested_role": "devops",
                "depends_on": [],
            },
            {
                "id": "s_auto_test",
                "name": "自动化测试",
                "description": "运行自动化测试套件",
                "prompt_template": (
                    "请运行自动化测试套件：\n\n"
                    "{context}\n\n"
                    "执行：单元测试、集成测试、E2E 测试、覆盖率统计"
                ),
                "input_spec": "构建产物",
                "output_spec": "测试报告 + 覆盖率报告",
                "suggested_role": "tester",
                "depends_on": ["s_build"],
            },
            {
                "id": "s_canary",
                "name": "灰度发布",
                "description": "小流量灰度发布，观察指标",
                "prompt_template": (
                    "请执行灰度发布：\n\n"
                    "{context}\n\n"
                    "步骤：灰度策略配置（5% → 20% → 50%）、"
                    "核心指标监控、异常告警配置、回滚触发条件"
                ),
                "input_spec": "测试通过的构建产物",
                "output_spec": "灰度发布报告 + 指标变化",
                "suggested_role": "devops",
                "depends_on": ["s_auto_test"],
            },
            {
                "id": "s_full_release",
                "name": "全量上线",
                "description": "全量发布到生产环境",
                "prompt_template": (
                    "灰度验证通过，请执行全量上线：\n\n"
                    "{context}\n\n"
                    "步骤：全量切流、DNS/CDN 更新、缓存刷新、"
                    "数据库迁移（如有）、回滚方案就绪确认"
                ),
                "input_spec": "灰度验证通过的报告",
                "output_spec": "全量发布确认",
                "suggested_role": "devops",
                "depends_on": ["s_canary"],
            },
            {
                "id": "s_post_monitor",
                "name": "上线监控",
                "description": "上线后持续监控，确认系统稳定",
                "prompt_template": (
                    "请进行上线后监控验证：\n\n"
                    "{context}\n\n"
                    "监控：错误率、延迟 P99、CPU/内存使用、"
                    "业务指标（转化率/DAU 等）、用户反馈\n"
                    "观察周期：30 分钟快速验证 + 24 小时稳定性观察"
                ),
                "input_spec": "全量发布确认",
                "output_spec": "上线监控报告",
                "suggested_role": "devops",
                "depends_on": ["s_full_release"],
            },
        ],
    },

    # =====================================================================
    # 7. 数据分析报告
    # =====================================================================
    {
        "id": "catalog_data_analysis",
        "name": "数据分析报告",
        "description": "数据驱动决策流程：数据采集 → 清洗处理 → 分析建模 → 可视化 → 报告输出",
        "category": "数据分析",
        "icon": "📊",
        "tags": ["数据", "分析", "报告", "BI"],
        "steps": [
            {
                "id": "s_data_collect",
                "name": "数据采集",
                "description": "明确数据需求，采集所需数据",
                "prompt_template": (
                    "请根据分析目标进行数据采集规划：\n\n"
                    "{input}\n\n"
                    "输出：数据源清单、采集方法、数据字段说明、"
                    "数据时间范围、采样策略"
                ),
                "input_spec": "分析目标和业务问题",
                "output_spec": "数据采集方案 + 原始数据",
                "suggested_role": "data_analyst",
                "depends_on": [],
            },
            {
                "id": "s_data_clean",
                "name": "数据清洗",
                "description": "数据质量检查、清洗、转换",
                "prompt_template": (
                    "请对采集到的数据进行清洗处理：\n\n"
                    "{context}\n\n"
                    "处理：缺失值、异常值、重复数据、类型转换、"
                    "数据标准化、数据质量报告"
                ),
                "input_spec": "原始数据",
                "output_spec": "清洗后的数据 + 数据质量报告",
                "suggested_role": "data_analyst",
                "depends_on": ["s_data_collect"],
            },
            {
                "id": "s_analysis",
                "name": "分析建模",
                "description": "数据分析、统计检验、趋势发现",
                "prompt_template": (
                    "请对清洗后的数据进行分析：\n\n"
                    "{context}\n\n"
                    "分析：描述性统计、趋势分析、相关性分析、"
                    "异常点识别、假设检验、关键发现"
                ),
                "input_spec": "清洗后的数据",
                "output_spec": "分析结果 + 关键发现",
                "suggested_role": "data_analyst",
                "depends_on": ["s_data_clean"],
            },
            {
                "id": "s_visualization",
                "name": "可视化呈现",
                "description": "制作数据可视化图表",
                "prompt_template": (
                    "请将分析结果进行可视化呈现：\n\n"
                    "{context}\n\n"
                    "要求：选择合适的图表类型、清晰的标题和标签、"
                    "突出关键数据点、配色方案、交互建议"
                ),
                "input_spec": "分析结果",
                "output_spec": "可视化图表 + 图表说明",
                "suggested_role": "data_analyst",
                "depends_on": ["s_analysis"],
            },
            {
                "id": "s_report",
                "name": "报告输出",
                "description": "整合分析成果，产出可执行的报告",
                "prompt_template": (
                    "请整合所有分析成果，产出最终报告：\n\n"
                    "{context}\n\n"
                    "报告结构：Executive Summary、关键发现、"
                    "详细分析、建议行动项、数据附录"
                ),
                "input_spec": "分析结果 + 可视化图表",
                "output_spec": "完整数据分析报告",
                "suggested_role": "data_analyst",
                "depends_on": ["s_visualization"],
                # Phase 3 P3-6 (2026-05-06): deliverable contract
                "output_files": ["data-analysis-report.md"],
                "must_contain": [
                    "## Executive Summary", "## 关键发现",
                    "## 详细分析", "## 建议行动项",
                ],
                "min_lines": 50,
            },
        ],
    },

    # =====================================================================
    # 8. 新员工入职流程
    # =====================================================================
    {
        "id": "catalog_onboarding",
        "name": "新员工入职流程",
        "description": "新员工入职全流程：入职准备 → 环境配置 → 制度培训 → 业务培训 → 试用期考核",
        "category": "人力资源",
        "icon": "👋",
        "tags": ["入职", "培训", "HR", "新人"],
        "steps": [
            {
                "id": "s_pre_onboard",
                "name": "入职准备",
                "description": "账号开通、设备准备、工位安排",
                "prompt_template": (
                    "新员工入职准备：\n\n"
                    "{input}\n\n"
                    "清单：邮箱/IM 账号开通、代码仓库权限、"
                    "设备领取、门禁/VPN 配置、工位安排、"
                    "Buddy 指定、入职材料准备"
                ),
                "input_spec": "新员工信息（姓名、部门、岗位、入职日期）",
                "output_spec": "入职准备清单（完成状态）",
                "suggested_role": "hr",
                "depends_on": [],
            },
            {
                "id": "s_env_setup",
                "name": "开发环境配置",
                "description": "技术团队协助配置开发环境",
                "prompt_template": (
                    "请生成新员工开发环境配置指南：\n\n"
                    "{context}\n\n"
                    "包含：代码 clone、依赖安装、IDE 配置、"
                    "数据库连接、测试环境 access、常用脚本"
                ),
                "input_spec": "员工岗位和技术栈",
                "output_spec": "环境配置文档 + 常见问题 FAQ",
                "suggested_role": "coder",
                "depends_on": ["s_pre_onboard"],
            },
            {
                "id": "s_policy_training",
                "name": "制度培训",
                "description": "公司制度、流程、文化介绍",
                "prompt_template": (
                    "请生成新员工制度培训材料：\n\n"
                    "{context}\n\n"
                    "涵盖：公司文化/价值观、考勤制度、报销流程、"
                    "信息安全规范、行为准则、沟通渠道"
                ),
                "input_spec": "公司制度文档",
                "output_spec": "培训材料 + 考核题目",
                "suggested_role": "hr",
                "depends_on": ["s_pre_onboard"],
            },
            {
                "id": "s_biz_training",
                "name": "业务培训",
                "description": "产品/业务知识培训",
                "prompt_template": (
                    "请生成新员工业务培训计划：\n\n"
                    "{context}\n\n"
                    "包含：产品架构概览、核心业务流程、"
                    "技术架构介绍、团队分工、近期重点项目"
                ),
                "input_spec": "部门和岗位信息",
                "output_spec": "业务培训计划 + 学习资料清单",
                "suggested_role": "pm",
                "depends_on": ["s_env_setup", "s_policy_training"],
            },
            {
                "id": "s_probation_plan",
                "name": "试用期目标",
                "description": "制定试用期考核目标和计划",
                "prompt_template": (
                    "请制定新员工试用期考核计划：\n\n"
                    "{context}\n\n"
                    "包含：30/60/90 天目标、关键交付物、"
                    "考核标准、定期 1:1 计划、转正条件"
                ),
                "input_spec": "岗位职责 + 培训完成情况",
                "output_spec": "试用期计划文档",
                "suggested_role": "hr",
                "depends_on": ["s_biz_training"],
            },
        ],
    },

    # =====================================================================
    # 9. 市场推广方案
    # =====================================================================
    {
        "id": "catalog_marketing_campaign",
        "name": "市场推广活动",
        "description": "市场活动策划流程：市场调研 → 方案策划 → 物料制作 → 渠道投放 → 效果追踪",
        "category": "市场营销",
        "icon": "📢",
        "tags": ["营销", "推广", "活动", "投放"],
        "steps": [
            {
                "id": "s_market_research",
                "name": "市场调研",
                "description": "目标市场和竞品分析",
                "prompt_template": (
                    "请进行市场调研分析：\n\n"
                    "{input}\n\n"
                    "分析：目标用户画像、市场规模、竞品分析（至少3家）、"
                    "差异化定位、市场机会"
                ),
                "input_spec": "产品/服务信息和推广目标",
                "output_spec": "市场调研报告",
                "suggested_role": "marketing",
                "depends_on": [],
            },
            {
                "id": "s_campaign_plan",
                "name": "方案策划",
                "description": "制定推广策略和执行方案",
                "prompt_template": (
                    "基于调研结果制定推广方案：\n\n"
                    "{context}\n\n"
                    "包含：推广目标（KPI）、核心卖点提炼、"
                    "渠道策略（线上/线下）、预算分配、时间表"
                ),
                "input_spec": "市场调研报告",
                "output_spec": "推广方案文档",
                "suggested_role": "marketing",
                "depends_on": ["s_market_research"],
            },
            {
                "id": "s_material_creation",
                "name": "物料制作",
                "description": "制作推广所需的内容物料",
                "prompt_template": (
                    "请根据推广方案制作内容物料：\n\n"
                    "{context}\n\n"
                    "包含：广告文案（多版本 A/B）、落地页文案、"
                    "社交媒体帖子、EDM 邮件模板、Banner 文案"
                ),
                "input_spec": "推广方案",
                "output_spec": "推广物料包",
                "suggested_role": "writer",
                "depends_on": ["s_campaign_plan"],
            },
            {
                "id": "s_channel_launch",
                "name": "渠道投放",
                "description": "各渠道投放配置和上线",
                "prompt_template": (
                    "请制定各渠道投放计划：\n\n"
                    "{context}\n\n"
                    "配置：投放渠道设置、受众定向、出价策略、"
                    "预算分配、A/B 测试计划、排期表"
                ),
                "input_spec": "推广物料包",
                "output_spec": "投放配置文档 + 排期表",
                "suggested_role": "marketing",
                "depends_on": ["s_material_creation"],
            },
            {
                "id": "s_campaign_tracking",
                "name": "效果追踪",
                "description": "投放效果监测和优化建议",
                "prompt_template": (
                    "请对推广效果进行追踪分析：\n\n"
                    "{context}\n\n"
                    "分析：各渠道 ROI、转化漏斗分析、"
                    "A/B 测试结果、优化建议、下阶段策略调整"
                ),
                "input_spec": "投放数据",
                "output_spec": "效果分析报告 + 优化建议",
                "suggested_role": "data_analyst",
                "depends_on": ["s_channel_launch"],
            },
        ],
    },

    # =====================================================================
    # 10. 客户问题处理流程
    # =====================================================================
    {
        "id": "catalog_customer_issue",
        "name": "客户问题处理",
        "description": "客户工单处理流程：接收分类 → 问题诊断 → 解决方案 → 客户沟通 → 跟踪关闭",
        "category": "客户支持",
        "icon": "🎧",
        "tags": ["客服", "工单", "问题", "支持"],
        "steps": [
            {
                "id": "s_ticket_triage",
                "name": "工单分类",
                "description": "接收工单，分类和评估优先级",
                "prompt_template": (
                    "请对以下客户工单进行分类和优先级评估：\n\n"
                    "{input}\n\n"
                    "输出：问题分类（技术/账户/计费/咨询）、"
                    "优先级（P0-P3）、影响范围、SLA 要求"
                ),
                "input_spec": "客户工单内容",
                "output_spec": "分类和优先级评估",
                "suggested_role": "support",
                "depends_on": [],
            },
            {
                "id": "s_diagnosis",
                "name": "问题诊断",
                "description": "深入分析问题原因",
                "prompt_template": (
                    "请对工单进行深入诊断：\n\n"
                    "{context}\n\n"
                    "诊断：问题复现、日志分析、关联问题排查、根因判断"
                ),
                "input_spec": "工单分类信息",
                "output_spec": "诊断报告",
                "suggested_role": "support",
                "depends_on": ["s_ticket_triage"],
            },
            {
                "id": "s_solution",
                "name": "解决方案",
                "description": "制定解决方案",
                "prompt_template": (
                    "请制定解决方案：\n\n"
                    "{context}\n\n"
                    "输出：解决步骤、临时 workaround（如需）、"
                    "长期修复方案、是否需要升级到工程团队"
                ),
                "input_spec": "诊断报告",
                "output_spec": "解决方案文档",
                "suggested_role": "support",
                "depends_on": ["s_diagnosis"],
            },
            {
                "id": "s_customer_reply",
                "name": "客户沟通",
                "description": "向客户反馈解决进展",
                "prompt_template": (
                    "请草拟客户回复邮件：\n\n"
                    "{context}\n\n"
                    "要求：专业友善的语气、清晰的问题说明、"
                    "解决步骤指导、后续跟进计划"
                ),
                "input_spec": "解决方案",
                "output_spec": "客户回复邮件草稿",
                "suggested_role": "support",
                "depends_on": ["s_solution"],
            },
            {
                "id": "s_ticket_close",
                "name": "工单关闭",
                "description": "确认问题解决，关闭工单",
                "prompt_template": (
                    "请进行工单关闭确认：\n\n"
                    "{context}\n\n"
                    "检查：客户确认解决、知识库更新、"
                    "是否需要产品改进建议、满意度调查"
                ),
                "input_spec": "客户反馈",
                "output_spec": "工单关闭报告 + 知识库更新",
                "suggested_role": "support",
                "depends_on": ["s_customer_reply"],
                # Phase 3 P3-6 (2026-05-06): deliverable contract
                "output_files": ["ticket-close-report.md"],
                "must_contain": [
                    "## 客户确认", "## 知识库更新",
                    "## 改进建议", "## 满意度",
                ],
                "min_lines": 15,
            },
        ],
    },

    # =====================================================================
    # 11. 安全事件响应
    # =====================================================================
    {
        "id": "catalog_incident_response",
        "name": "安全事件响应",
        "description": "安全事件标准处理流程：检测 → 遏制 → 根因分析 → 修复恢复 → 复盘改进",
        "category": "安全合规",
        "icon": "🛡️",
        "tags": ["安全", "事件", "应急", "响应"],
        "steps": [
            {
                "id": "s_detect",
                "name": "事件检测与评估",
                "description": "确认安全事件，评估影响等级",
                "prompt_template": (
                    "安全事件已触发：\n\n"
                    "{input}\n\n"
                    "请进行初步评估：事件类型、影响等级（高/中/低）、"
                    "受影响系统/数据、是否正在进行中"
                ),
                "input_spec": "告警信息或安全报告",
                "output_spec": "事件评估报告",
                "suggested_role": "security",
                "depends_on": [],
            },
            {
                "id": "s_contain",
                "name": "遏制隔离",
                "description": "采取紧急措施遏制事件扩散",
                "prompt_template": (
                    "请制定遏制措施：\n\n"
                    "{context}\n\n"
                    "措施：网络隔离、账号冻结、服务降级、"
                    "证据保全、通知相关方"
                ),
                "input_spec": "事件评估报告",
                "output_spec": "遏制行动清单",
                "suggested_role": "security",
                "depends_on": ["s_detect"],
            },
            {
                "id": "s_forensics",
                "name": "根因分析",
                "description": "深入调查事件根因和攻击路径",
                "prompt_template": (
                    "请进行安全事件根因分析：\n\n"
                    "{context}\n\n"
                    "分析：攻击向量、漏洞利用方式、入侵时间线、"
                    "数据泄露范围、攻击者画像"
                ),
                "input_spec": "遏制行动记录 + 日志数据",
                "output_spec": "根因分析报告",
                "suggested_role": "security",
                "depends_on": ["s_contain"],
            },
            {
                "id": "s_remediate",
                "name": "修复恢复",
                "description": "修复漏洞，恢复服务",
                "prompt_template": (
                    "请制定修复和恢复计划：\n\n"
                    "{context}\n\n"
                    "包含：漏洞修补、系统加固、服务恢复步骤、"
                    "数据完整性验证、安全基线重建"
                ),
                "input_spec": "根因分析报告",
                "output_spec": "修复方案 + 恢复计划",
                "suggested_role": "security",
                "depends_on": ["s_forensics"],
            },
            {
                "id": "s_post_incident",
                "name": "复盘改进",
                "description": "事件复盘和安全策略改进",
                "prompt_template": (
                    "请进行安全事件复盘：\n\n"
                    "{context}\n\n"
                    "内容：事件时间线、响应效果评估、改进措施、"
                    "安全策略更新、培训需求、预防措施"
                ),
                "input_spec": "完整事件处理记录",
                "output_spec": "复盘报告 + 改进行动项",
                "suggested_role": "security",
                "depends_on": ["s_remediate"],
            },
        ],
    },

    # =====================================================================
    # 12. Sprint 迭代流程
    # =====================================================================
    {
        "id": "catalog_sprint",
        "name": "Sprint 迭代管理",
        "description": "敏捷 Sprint 流程：Sprint 规划 → 每日站会 → 开发交付 → Sprint 评审 → Sprint 回顾",
        "category": "项目管理",
        "icon": "🏃",
        "tags": ["敏捷", "Sprint", "Scrum", "迭代"],
        "steps": [
            {
                "id": "s_sprint_plan",
                "name": "Sprint 规划",
                "description": "确定 Sprint 目标和任务分配",
                "prompt_template": (
                    "请进行 Sprint 规划：\n\n"
                    "{input}\n\n"
                    "输出：Sprint 目标、Story Point 估算、"
                    "任务分配、优先级排序、Definition of Done"
                ),
                "input_spec": "Product Backlog + 团队容量",
                "output_spec": "Sprint Backlog + 任务分配",
                "suggested_role": "pm",
                "depends_on": [],
            },
            {
                "id": "s_daily_standup",
                "name": "每日站会总结",
                "description": "汇总每日进展、阻塞和计划",
                "prompt_template": (
                    "请汇总 Sprint 进展：\n\n"
                    "{context}\n\n"
                    "整理：各成员昨日完成、今日计划、阻塞项、"
                    "燃尽图更新、风险预警"
                ),
                "input_spec": "团队成员的每日更新",
                "output_spec": "每日站会纪要",
                "suggested_role": "pm",
                "depends_on": ["s_sprint_plan"],
            },
            {
                "id": "s_sprint_dev",
                "name": "开发交付",
                "description": "按 Sprint 目标进行开发",
                "prompt_template": (
                    "请按 Sprint 计划进行开发：\n\n"
                    "{context}\n\n"
                    "执行：按优先级开发、编写测试、"
                    "代码提交、PR 创建、CI 通过确认"
                ),
                "input_spec": "Sprint Backlog",
                "output_spec": "开发完成的功能列表 + PR 链接",
                "suggested_role": "coder",
                "depends_on": ["s_sprint_plan"],
            },
            {
                "id": "s_sprint_review",
                "name": "Sprint 评审",
                "description": "向干系人演示交付成果",
                "prompt_template": (
                    "请准备 Sprint 评审：\n\n"
                    "{context}\n\n"
                    "准备：Demo 脚本、完成 Story 列表、"
                    "未完成项说明、指标汇总、下一 Sprint 候选项"
                ),
                "input_spec": "Sprint 交付物",
                "output_spec": "Sprint 评审报告",
                "suggested_role": "pm",
                "depends_on": ["s_sprint_dev"],
            },
            {
                "id": "s_sprint_retro",
                "name": "Sprint 回顾",
                "description": "团队回顾，持续改进",
                "prompt_template": (
                    "请进行 Sprint 回顾总结：\n\n"
                    "{context}\n\n"
                    "回顾：Keep（继续做的）、Stop（停止做的）、"
                    "Start（开始做的）、改进行动项、跟踪方式"
                ),
                "input_spec": "Sprint 评审报告 + 团队反馈",
                "output_spec": "回顾报告 + 改进行动项",
                "suggested_role": "pm",
                "depends_on": ["s_sprint_review"],
            },
        ],
    },

    # =====================================================================
    # 13. 技术文档编写
    # =====================================================================
    {
        "id": "catalog_tech_doc",
        "name": "技术文档编写",
        "description": "技术文档标准流程：大纲规划 → 内容编写 → 技术评审 → 排版发布",
        "category": "内容创作",
        "icon": "📖",
        "tags": ["文档", "技术", "API", "说明书"],
        "steps": [
            {
                "id": "s_doc_outline",
                "name": "大纲规划",
                "description": "确定文档结构和内容范围",
                "prompt_template": (
                    "请规划技术文档大纲：\n\n"
                    "{input}\n\n"
                    "输出：文档目标受众、章节结构、"
                    "每章核心内容描述、代码示例计划、预计篇幅"
                ),
                "input_spec": "文档主题和目标",
                "output_spec": "文档大纲",
                "suggested_role": "writer",
                "depends_on": [],
            },
            {
                "id": "s_doc_write",
                "name": "内容编写",
                "description": "按大纲编写文档内容",
                "prompt_template": (
                    "请按大纲编写技术文档：\n\n"
                    "{context}\n\n"
                    "要求：清晰准确、代码示例可运行、"
                    "包含注意事项和最佳实践、术语统一"
                ),
                "input_spec": "文档大纲",
                "output_spec": "文档初稿",
                "suggested_role": "writer",
                "depends_on": ["s_doc_outline"],
            },
            {
                "id": "s_tech_review",
                "name": "技术评审",
                "description": "技术准确性和完整性审查",
                "prompt_template": (
                    "请对技术文档进行评审：\n\n"
                    "{context}\n\n"
                    "评审：技术准确性、代码可运行性、"
                    "完整性（边界情况、错误处理）、一致性"
                ),
                "input_spec": "文档初稿",
                "output_spec": "评审意见 + 修改建议",
                "suggested_role": "senior_coder",
                "depends_on": ["s_doc_write"],
            },
            {
                "id": "s_doc_publish",
                "name": "排版发布",
                "description": "最终排版和发布",
                "prompt_template": (
                    "请完成文档最终排版和发布准备：\n\n"
                    "{context}\n\n"
                    "检查：格式统一、超链接有效、目录生成、"
                    "版本号标注、更新日志"
                ),
                "input_spec": "评审后的文档",
                "output_spec": "发布就绪的文档",
                "suggested_role": "writer",
                "depends_on": ["s_tech_review"],
            },
        ],
    },

    # =====================================================================
    # 14. API 设计与开发
    # =====================================================================
    {
        "id": "catalog_api_design",
        "name": "API 设计与开发",
        "description": "RESTful/GraphQL API 开发流程：需求梳理 → 接口设计 → 开发实现 → 文档生成 → 集成测试",
        "category": "产品研发",
        "icon": "🔌",
        "tags": ["API", "接口", "REST", "开发"],
        "steps": [
            {
                "id": "s_api_req",
                "name": "API 需求梳理",
                "description": "明确 API 使用场景和需求",
                "prompt_template": (
                    "请梳理 API 需求：\n\n"
                    "{input}\n\n"
                    "输出：调用方（前端/第三方/内部服务）、"
                    "业务场景、数据流、性能要求、安全要求"
                ),
                "input_spec": "业务需求描述",
                "output_spec": "API 需求文档",
                "suggested_role": "architect",
                "depends_on": [],
            },
            {
                "id": "s_api_design",
                "name": "接口设计",
                "description": "设计 API 接口规范",
                "prompt_template": (
                    "请设计 API 接口：\n\n"
                    "{context}\n\n"
                    "设计：Endpoint 路由、HTTP 方法、"
                    "请求/响应格式、状态码规范、分页/排序、"
                    "认证方式、限流策略、版本化方案"
                ),
                "input_spec": "API 需求文档",
                "output_spec": "API 设计规范（OpenAPI/Swagger）",
                "suggested_role": "architect",
                "depends_on": ["s_api_req"],
            },
            {
                "id": "s_api_implement",
                "name": "API 开发",
                "description": "按设计规范实现 API",
                "prompt_template": (
                    "请按接口设计规范实现 API：\n\n"
                    "{context}\n\n"
                    "实现：路由注册、参数校验、业务逻辑、"
                    "错误处理、日志记录、单元测试"
                ),
                "input_spec": "API 设计规范",
                "output_spec": "API 代码 + 单元测试",
                "suggested_role": "coder",
                "depends_on": ["s_api_design"],
            },
            {
                "id": "s_api_doc",
                "name": "API 文档生成",
                "description": "生成 API 使用文档和示例",
                "prompt_template": (
                    "请生成 API 文档：\n\n"
                    "{context}\n\n"
                    "包含：接口列表、参数说明、调用示例（curl/Python/JS）、"
                    "错误码表、认证说明、Rate Limit 说明"
                ),
                "input_spec": "API 代码",
                "output_spec": "API 文档",
                "suggested_role": "writer",
                "depends_on": ["s_api_implement"],
                # Phase 3 P3-6 (2026-05-06): deliverable contract
                "output_files": ["api-docs.md"],
                "must_contain": [
                    "## 接口列表", "## 参数说明", "## 调用示例",
                    "## 错误码", "## 认证",
                ],
                "min_lines": 40,
            },
            {
                "id": "s_api_test",
                "name": "API 集成测试",
                "description": "端到端集成测试",
                "prompt_template": (
                    "请对 API 进行集成测试：\n\n"
                    "{context}\n\n"
                    "测试：Happy Path、异常路径、并发测试、"
                    "认证/授权测试、性能基准测试"
                ),
                "input_spec": "API 代码 + 文档",
                "output_spec": "集成测试报告",
                "suggested_role": "tester",
                "depends_on": ["s_api_implement"],
            },
        ],
    },

    # =====================================================================
    # 15. 竞品分析
    # =====================================================================
    {
        "id": "catalog_competitive_analysis",
        "name": "竞品分析报告",
        "description": "系统化竞品分析：信息收集 → 功能对比 → 策略分析 → 建议输出",
        "category": "市场营销",
        "icon": "🔬",
        "tags": ["竞品", "分析", "市场", "策略"],
        "steps": [
            {
                "id": "s_info_gather",
                "name": "信息收集",
                "description": "收集竞品公开信息",
                "prompt_template": (
                    "请收集以下竞品的公开信息：\n\n"
                    "{input}\n\n"
                    "收集：产品定位、核心功能、定价策略、"
                    "目标用户、市场份额、融资情况、团队规模"
                ),
                "input_spec": "竞品名称列表 + 分析维度",
                "output_spec": "竞品信息汇总",
                "suggested_role": "marketing",
                "depends_on": [],
            },
            {
                "id": "s_feature_compare",
                "name": "功能对比",
                "description": "详细功能特性对比矩阵",
                "prompt_template": (
                    "请制作功能对比矩阵：\n\n"
                    "{context}\n\n"
                    "对比：核心功能覆盖度、用户体验、技术架构、"
                    "生态/集成能力、定价竞争力"
                ),
                "input_spec": "竞品信息汇总",
                "output_spec": "功能对比矩阵",
                "suggested_role": "pm",
                "depends_on": ["s_info_gather"],
            },
            {
                "id": "s_strategy_analysis",
                "name": "策略分析",
                "description": "分析竞品策略和市场趋势",
                "prompt_template": (
                    "请进行竞品策略分析：\n\n"
                    "{context}\n\n"
                    "分析：SWOT 分析、市场定位差异、增长策略、"
                    "护城河分析、市场趋势判断"
                ),
                "input_spec": "功能对比矩阵 + 市场数据",
                "output_spec": "策略分析报告",
                "suggested_role": "marketing",
                "depends_on": ["s_feature_compare"],
            },
            {
                "id": "s_recommendations",
                "name": "建议输出",
                "description": "产出可执行的策略建议",
                "prompt_template": (
                    "请基于竞品分析产出策略建议：\n\n"
                    "{context}\n\n"
                    "输出：差异化打法、功能优先级建议、定价策略建议、"
                    "短期行动项（3个月）、长期战略（1年）"
                ),
                "input_spec": "策略分析报告",
                "output_spec": "策略建议书",
                "suggested_role": "pm",
                "depends_on": ["s_strategy_analysis"],
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Catalog 分类索引
# ---------------------------------------------------------------------------

def get_catalog_categories() -> dict[str, list[dict]]:
    """按分类组织 catalog，返回 {category: [template_summary]}。"""
    categories: dict[str, list[dict]] = {}
    for tmpl in WORKFLOW_CATALOG:
        cat = tmpl.get("category", "其他")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append({
            "id": tmpl["id"],
            "name": tmpl["name"],
            "description": tmpl["description"],
            "icon": tmpl.get("icon", "📋"),
            "tags": tmpl.get("tags", []),
            "step_count": len(tmpl.get("steps", [])),
        })
    return categories


def get_catalog_template(template_id: str) -> dict | None:
    """按 ID 获取 catalog 中的模板详情。"""
    for tmpl in WORKFLOW_CATALOG:
        if tmpl["id"] == template_id:
            return tmpl
    return None


def list_catalog_templates() -> list[dict]:
    """返回所有 catalog 模板的摘要列表。"""
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "category": t.get("category", "其他"),
            "icon": t.get("icon", "📋"),
            "tags": t.get("tags", []),
            "step_count": len(t.get("steps", [])),
        }
        for t in WORKFLOW_CATALOG
    ]
