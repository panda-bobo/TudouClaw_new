"""
Security module — authentication, authorization, tool approval, audit logging.

Provides:
- Token-based API auth (Bearer tokens)
- Cookie-based web session auth
- Role-based access control: admin / operator / viewer
- Admin user management: superAdmin / admin with agent binding
- Tool execution policy: safe / needs-approval / denied
- Blocking approval queue for dangerous operations
- Audit log with persistent file output
- Per-IP rate limiting
- Shared secret for agent-portal trust
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("tudou.auth")
from enum import Enum
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Optional

from .defaults import (
    SESSION_TTL, RATE_LIMIT_RPS, RATE_LIMIT_BURST,
    APPROVAL_TIMEOUT, DELEGATION_TIMEOUT,
    MAX_DELEGATION_CONCURRENCY, MAX_DELEGATION_QUEUE,
    LOCAL_ADDRESSES,
)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

class Role(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"

    def can(self, action: str) -> bool:
        return action in _ROLE_PERMISSIONS.get(self.value, set())


class AdminRole(str, Enum):
    """Admin user roles."""
    SUPER_ADMIN = "superAdmin"
    ADMIN = "admin"


_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {
        "view_dashboard", "view_agents", "view_config", "view_audit", "view_tokens",
        "chat", "create_agent", "delete_agent", "clear_agent", "delegate",
        "manage_tokens", "manage_config", "manage_nodes",
        "register_node", "broadcast", "orchestrate",
        "approve_tool", "deny_tool", "manage_policy",
    },
    "operator": {
        "view_dashboard", "view_agents", "view_config", "view_audit",
        "chat", "create_agent", "delete_agent", "clear_agent", "delegate",
        "manage_nodes", "register_node",
        "approve_tool", "deny_tool",
    },
    "viewer": {
        "view_dashboard", "view_agents", "view_audit",
    },
}


# ---------------------------------------------------------------------------
# Tool execution policy
# ---------------------------------------------------------------------------

class ToolRisk(str, Enum):
    """4-tier risk model for tool execution policy.

    RED    — 红线: Unconditionally blocked (deny). Cannot be overridden.
    HIGH   — 高风险: Requires human admin approval via Portal.
    MODERATE — 中风险: Agent with approval authority can approve, or auto-approve.
    LOW    — 低风险: Always auto-approved, no approval needed.
    """
    RED = "red"                 # 红线 — always deny, cannot override
    HIGH = "high"               # 高风险 — admin/human approval required
    MODERATE = "moderate"       # 中风险 — agent-approvable or auto-approve
    LOW = "low"                 # 低风险 — always auto-approve

    # Legacy aliases for backward compatibility
    SAFE = "low"
    DANGEROUS = "high"

# ── Default risk classification for ALL tool actions ──
# Admin can override any of these at runtime via Portal Settings.
DEFAULT_TOOL_RISK: dict[str, str] = {
    # ── Query / Read (低风险) ──
    "read_file":        "low",
    "search_files":     "low",
    "glob_files":       "low",
    "web_search":       "low",
    "web_fetch":        "low",
    "datetime_calc":    "low",
    "json_process":     "low",
    "text_process":     "low",
    "plan_update":      "low",      # Agent updates its own plan
    "task_update":      "low",      # Task status updates
    "send_message":     "low",      # Inter-agent messaging
    "get_skill_guide":  "low",      # Read SKILL.md from disk
    "knowledge_lookup": "low",      # Query knowledge base (read-only)
    "learn_from_peers": "low",      # Read other agents' experiences
    "save_experience":  "low",      # Agent's own journal, private
    "create_goal":      "low",      # Track own goals
    "update_goal_progress": "low",
    "create_milestone": "low",
    "update_milestone_status": "low",

    # ── Create / Modify (中风险) ──
    "write_file":       "moderate",
    "edit_file":        "moderate",
    "team_create":      "moderate", # Spawn sub-agent
    "web_screenshot":   "moderate",
    "http_request":     "moderate", # Outbound HTTP call
    "mcp_call":         "moderate", # Call external MCP tool
    "propose_skill":    "moderate", # Drafts a skill for admin review
    "submit_skill":     "moderate", # Submits skill to catalog
    "share_knowledge":  "moderate", # Broadcast to other agents
    "submit_deliverable": "moderate",
    "pip_install":      "moderate", # Installs Python packages

    # ── Dangerous execution (高风险) ──
    "bash":             "high",     # Shell command execution

    # ── Delete / Destructive (红线) ──
    # These are blocked by default. Admin must explicitly downgrade
    # the risk level if they want to allow them.
    "delete_file":      "red",      # File deletion
    "rm_rf":            "red",      # Recursive delete
    "drop_table":       "red",      # Database drop
    "truncate":         "red",      # Database truncate
}

# ── Agent approval authority ──
# Maps agent priority level → set of risk levels they can approve.
# Priority: 0=Admin, 1=CXO, 2=PM, 3=TeamMember
# Admin (0) can approve everything; configured via Portal.
DEFAULT_AGENT_APPROVAL_AUTHORITY: dict[int, list[str]] = {
    0: ["red", "high", "moderate", "low"],   # Admin — can override anything
    1: ["high", "moderate", "low"],           # CXO — can approve high risk
    2: ["moderate", "low"],                   # PM — can approve moderate risk
    3: ["low"],                               # Team Member — low risk only
}

# Bash commands that are ALWAYS blocked
DENY_PATTERNS: list[str] = [
    r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/\s*$",     # rm -rf /
    r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/\*",         # rm -rf /*
    r"mkfs\.",                                        # format disk
    r"dd\s+.*of=/dev/",                              # dd to device
    r":\(\)\{.*\}",                                  # fork bomb
    r"chmod\s+(-R\s+)?777\s+/",                      # chmod 777 /
    r"curl.*\|\s*(ba)?sh",                           # curl | sh
    r"wget.*\|\s*(ba)?sh",                           # wget | sh
    r">\s*/dev/sd[a-z]",                             # write to raw disk
    r"shutdown|reboot|poweroff|init\s+[06]",         # system commands
]

# ---------------------------------------------------------------------------
# Bash sub-command risk classification (三级分类)
# 每条子命令独立评估，整体风险 = max(所有子命令风险)
# ---------------------------------------------------------------------------

# LOW: 只读/查询类 → 自动通过
BASH_LOW_PATTERNS: list[str] = [
    # 文件查看/搜索
    r"^(ls|cat|head|tail|wc|echo|pwd|whoami|date|uname|which|file|stat|du|df)\b",
    r"^(grep|find|sort|uniq|cut|tr|awk|sed|xargs|tee|less|more|diff|comm)\b",
    r"^(tree|realpath|dirname|basename|readlink|md5sum|sha256sum|wc)\b",
    # Git 只读
    r"^git\s+(status|log|diff|branch|show|remote|tag|stash\s+list|rev-parse|config\s+--get)\b",
    # 目录切换 (cd 本身无风险)
    r"^cd\b",
    # 版本查询
    r"^(python|python3|node|npm|pip|pip3|cargo|go|java|ruby|rustc|gcc|g\+\+|make|cmake)\s+(-v|--version|-V|version)$",
    # 环境查看
    r"^(env|printenv|locale|lsb_release|hostname|uptime|free|top|ps|id|groups)\b",
    # 网络/端口诊断 (只读)
    r"^(lsof|netstat|ss|ifconfig|ip\s+(addr|route|link|neigh)|arp|nmap)\b",
    # 进程查看 (只读)
    r"^(pgrep|pidof|jobs|fg|bg)\b",
    # 包管理查询
    r"^npm\s+(ls|list|info|view|outdated|audit|explain|why)\b",
    r"^pip3?\s+(list|show|freeze|check)\b",
    r"^(dpkg|apt)\s+(-l|list|show|search)\b",
    # 网络诊断 (只读)
    r"^(ping|traceroute|dig|nslookup|host|curl\s+(-s\s+)?-I)\b",
    # 测试命令
    r"^(test)\b",
    r"^\[",
    # true/false/exit
    r"^(true|false|exit)\b",
]

# MODERATE: 构建/安装/写入类 → 中风险，Agent可审批或自动通过
BASH_MODERATE_PATTERNS: list[str] = [
    # 包安装/构建
    r"^npm\s+(install|ci|run|start|build|test|init|create|exec|pack)\b",
    r"^(npx|yarn|pnpm|bun)\b",
    r"^pip3?\s+install\b",
    r"^(cargo\s+(build|run|test|install)|go\s+(build|run|test|install|get|mod))\b",
    r"^(make|cmake|gradle|mvn|ant)\b",
    # Python/Node 脚本执行
    r"^(python|python3|node)\s+",
    # Git 写操作
    r"^git\s+(add|commit|push|pull|fetch|merge|rebase|checkout|switch|clone|init|reset)\b",
    # 文件创建/移动 (非删除)
    r"^(cp|mv|touch|mkdir|ln|install)\b",
    # 权限修改 (非777/)
    r"^chmod\b",
    r"^chown\b",
    # 压缩/解压
    r"^(tar|zip|unzip|gzip|gunzip|bzip2|7z)\b",
    # 服务管理 (启动/重启，非终止)
    r"^(systemctl\s+(start|restart|status|enable|disable|is-active))\b",
    r"^(service\s+\S+\s+(start|restart|status))\b",
    # Docker (非特权)
    r"^docker\s+(ps|images|logs|inspect|exec|build|run|pull|compose)\b",
    # 编辑器 (非交互)
    r"^(cat|tee)\s+>",
    # 网络下载 (到本地)
    r"^(curl|wget)\s+.*-o\b",
    r"^(curl|wget)\s+(?!.*\|\s*(ba)?sh)",
]

# HIGH: 需要人工审批的高风险操作
BASH_HIGH_PATTERNS: list[str] = [
    # 进程终止
    r"^(kill|killall|pkill|xkill)\b",
    # 系统服务停止
    r"^(systemctl\s+(stop|mask))\b",
    r"^(service\s+\S+\s+stop)\b",
    # 网络修改
    r"^(iptables|ip6tables|ufw|firewall-cmd)\b",
    # 用户/权限管理
    r"^(useradd|userdel|usermod|groupadd|groupdel|passwd|visudo)\b",
    # crontab 修改
    r"^crontab\s+(-e|-r)\b",
    # 磁盘操作
    r"^(fdisk|parted|mkswap|swapon|swapoff|mount|umount)\b",
]

# 向后兼容: 保留旧变量名
SAFE_BASH_PATTERNS: list[str] = BASH_LOW_PATTERNS


def classify_bash_subcmd(subcmd: str) -> str:
    """对单条 bash 子命令进行风险分类。

    Returns: "low" | "moderate" | "high"
    """
    s = subcmd.strip()
    if not s:
        return "low"

    # 1. 先检查 deny patterns → high (会在上层被 deny)
    for pattern in DENY_PATTERNS:
        if re.search(pattern, s, re.IGNORECASE):
            return "high"

    # 2. 显式 HIGH patterns → 需要人工审批
    for pattern in BASH_HIGH_PATTERNS:
        if re.match(pattern, s, re.IGNORECASE):
            return "high"

    # 3. LOW patterns
    for pattern in BASH_LOW_PATTERNS:
        if re.match(pattern, s, re.IGNORECASE):
            return "low"

    # 4. MODERATE patterns
    for pattern in BASH_MODERATE_PATTERNS:
        if re.match(pattern, s, re.IGNORECASE):
            return "moderate"

    # 4. 未匹配到任何模式 → high
    return "high"


def analyze_bash_command(cmd: str) -> tuple[str, str]:
    """分析完整 bash 命令的风险等级。

    处理:
    - 注释去除 (# ...)
    - 链式命令拆分 (&&, ||, ;)
    - 管道拆分 (|)
    - 每条子命令独立评估
    - 整体风险 = max(所有子命令)

    Returns: (risk_level, reason)
        risk_level: "low" | "moderate" | "high"
        reason: 人类可读的风险说明
    """
    if not cmd or not cmd.strip():
        return "low", "Empty command"

    # 去除行内注释 (保留引号内的#)
    # 简单处理: 去掉非引号包裹的 # 后面的内容
    cleaned_lines = []
    for line in cmd.split("\n"):
        # 去掉整行注释
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # 去掉行尾注释 (简单: 不在引号内的#)
        in_quote = False
        quote_char = ""
        clean_idx = len(line)
        for i, c in enumerate(line):
            if c in ('"', "'") and (i == 0 or line[i-1] != "\\"):
                if not in_quote:
                    in_quote = True
                    quote_char = c
                elif c == quote_char:
                    in_quote = False
            elif c == "#" and not in_quote:
                clean_idx = i
                break
        cleaned_lines.append(line[:clean_idx].strip())

    full_cmd = " ".join(cleaned_lines).strip()
    if not full_cmd:
        return "low", "Comment-only command"

    # 拆分链式命令: &&, ||, ;
    # 注意保留管道 | 作为单条命令的一部分
    subcmds = re.split(r'\s*(?:&&|\|\||;)\s*', full_cmd)

    max_risk = "low"
    risk_order = {"low": 0, "moderate": 1, "high": 2}
    high_parts = []

    for subcmd in subcmds:
        if not subcmd.strip():
            continue

        # 管道链: 每个管道段也需要评估
        pipe_parts = [p.strip() for p in subcmd.split("|") if p.strip()]
        for part in pipe_parts:
            risk = classify_bash_subcmd(part)
            if risk_order.get(risk, 2) > risk_order.get(max_risk, 0):
                max_risk = risk
                if risk == "high":
                    high_parts.append(part[:60])

    if max_risk == "low":
        return "low", "All sub-commands are read-only/safe"
    elif max_risk == "moderate":
        return "moderate", "Contains build/install/write operations"
    else:
        return "high", f"Contains high-risk operations: {', '.join(high_parts[:3])}"


# ---------------------------------------------------------------------------
# Authorization Hierarchy for Command Priority System
# ---------------------------------------------------------------------------

def can_authorize(authorizer_priority: int, target_priority: int) -> bool:
    """
    Check if an authorizer can authorize a target agent.
    Returns True if authorizer_priority <= target_priority (lower number = higher authority).
    Admin (priority 0) can authorize everyone.

    Priority levels:
    - 0: Admin (system)
    - 1: CXO (highest)
    - 2: Project Manager
    - 3: Team Member (default)
    """
    # Admin (0) can authorize anyone
    if authorizer_priority == 0:
        return True
    # CXO (1) can authorize PM (2) and Team Members (3)
    if authorizer_priority == 1:
        return target_priority >= 2
    # PM (2) can authorize Team Members (3)
    if authorizer_priority == 2:
        return target_priority >= 3
    # Team Members (3) cannot authorize anyone
    return False


@dataclass
class PendingApproval:
    """A tool execution waiting for human approval."""
    approval_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    agent_id: str = ""
    agent_name: str = ""
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "pending"     # pending / approved / denied / expired
    decided_by: str = ""
    decided_at: float = 0.0
    # Threading event: set when decision is made
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "tool_name": self.tool_name,
            "arguments": _safe_truncate(self.arguments),
            "reason": self.reason,
            "created_at": self.created_at,
            "status": self.status,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }


@dataclass
class PendingLoginRequest:
    """A web login request waiting for user to provide credentials."""
    request_id: str = field(default_factory=lambda: "login_" + uuid.uuid4().hex[:10])
    agent_id: str = ""
    agent_name: str = ""
    url: str = ""
    site_name: str = ""
    login_url: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "pending"     # pending / submitted / expired
    credentials: dict = field(default_factory=dict)  # filled when user submits
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "url": self.url,
            "site_name": self.site_name,
            "login_url": self.login_url,
            "reason": self.reason,
            "created_at": self.created_at,
            "status": self.status,
        }


# Module-level login request store
_login_requests: dict[str, PendingLoginRequest] = {}
_login_lock = threading.Lock()


def create_login_request(agent_id: str, agent_name: str, url: str,
                         site_name: str, reason: str,
                         login_url: str = "") -> PendingLoginRequest:
    req = PendingLoginRequest(
        agent_id=agent_id, agent_name=agent_name,
        url=url, site_name=site_name, login_url=login_url, reason=reason,
    )
    with _login_lock:
        _login_requests[req.request_id] = req
    return req


def wait_for_login(req: PendingLoginRequest, timeout: float = 300) -> dict:
    """Block until user submits credentials or timeout. Returns credentials dict."""
    decided = req._event.wait(timeout=timeout)
    with _login_lock:
        _login_requests.pop(req.request_id, None)
    if not decided:
        req.status = "expired"
        return {}
    return req.credentials


def submit_login(request_id: str, credentials: dict) -> bool:
    with _login_lock:
        req = _login_requests.get(request_id)
        if not req or req.status != "pending":
            return False
        req.status = "submitted"
        req.credentials = credentials
    req._event.set()
    return True


def list_pending_logins() -> list[dict]:
    with _login_lock:
        now = time.time()
        expired = [k for k, v in _login_requests.items()
                   if now - v.created_at > 300]
        for k in expired:
            r = _login_requests.pop(k)
            r.status = "expired"
            r._event.set()
        return [v.to_dict() for v in _login_requests.values()
                if v.status == "pending"]


class ToolPolicy:
    """Manages tool execution permissions and the approval queue.

    4-tier risk model:
      RED      — 红线: unconditionally blocked (deny)
      HIGH     — 高风险: requires human/admin approval
      MODERATE — 中风险: agent with authority can approve, or auto-approve
      LOW      — 低风险: always auto-approve
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.tool_risk: dict[str, str] = dict(DEFAULT_TOOL_RISK)
        self.deny_patterns: list[str] = list(DENY_PATTERNS)
        self.safe_bash_patterns: list[str] = list(SAFE_BASH_PATTERNS)
        self.pending: dict[str, PendingApproval] = {}
        self.history: list[PendingApproval] = []
        # Approval timeout in seconds (default 5 minutes)
        self.approval_timeout: float = 300.0
        # If True, moderate-risk tools auto-approve without any approval
        self.auto_approve_moderate: bool = True
        # Session-scoped approvals: set of (agent_id, tool_name) pairs
        # pre-approved and persisted to disk so they survive restarts.
        # Despite the "session" name, the user-facing semantics is
        # "until an admin revokes it" — nobody expects to re-approve
        # the same tool after a service restart.
        self.session_approvals: set = set()
        self._session_approvals_file: str = ""   # set by set_persist_path()

        # Global tool denylist. Tools whose names appear here are
        # refused for EVERY agent, regardless of allowed_tools /
        # agent-level denied_tools. Admin-editable; persisted to disk.
        # Ships with ``create_pptx_advanced`` pre-denied because it's a
        # legacy internal tool fully replaced by the pptx-author skill.
        self.global_denylist: set = {"create_pptx_advanced"}
        self._global_denylist_file: str = ""   # set by set_persist_path()
        # Agent approval authority: agent_id → set of risk levels they can approve
        # Populated from agent priority + DEFAULT_AGENT_APPROVAL_AUTHORITY
        self.agent_approval_authority: dict[str, list[str]] = {}
        # Custom per-priority approval authority (admin-configurable)
        self.priority_approval_authority: dict[int, list[str]] = dict(
            DEFAULT_AGENT_APPROVAL_AUTHORITY
        )

        # ── Skill escalation policy (admin-configurable) ──
        # Skill 默认运行时不审批；以下规则命中时升级为 needs_approval
        # escalate_skills:    list[str] — 命中即升级（按 skill_id 或 manifest.name）
        # escalate_mcps:      list[str] — skill 调用到列表里的 MCP id 时升级
        # escalate_for_roles: list[str] — agent.role 命中时升级
        # escalate_outside_hours: tuple[int,int] | None — (start_hour, end_hour) 之外升级
        # escalate_if_count_per_hour: int — 单 (skill,agent) 每小时调用次数超过则升级
        self.skill_escalation: dict = {
            "escalate_skills": [],
            "escalate_mcps": [],
            "escalate_for_roles": [],
            "escalate_outside_hours": None,
            "escalate_if_count_per_hour": 0,
        }
        # 内部计数：(skill_id, agent_id) -> [(timestamp, ...), ...]
        self._skill_call_log: dict = {}

        # ── Sub-agent fork policy (admin-configurable) ──
        # max_depth:           最大委派深度（包括自身），None=不限
        # max_concurrent_per_parent: 单个 parent 同时存活的子 agent 数上限
        # max_total_concurrent: 全节点活跃子 agent 总数上限
        # allowed_parent_roles: 仅这些角色允许 fork（空 = 全部允许）
        # cost_budget_per_hour_usd: 单 agent 每小时累计开销上限（含子树），0 = 不限
        # blocked_parent_ids:  黑名单 agent 不可 fork
        # allowed_role_edges: 父角色 -> 允许委派的子角色列表
        #   形如 {"manager": ["analyst","writer"], "writer": ["analyst"]}
        #   空 dict 或父角色不在表中 = 不限制
        #   显式设为 [] = 该角色不允许委派任何子角色
        self.fork_policy: dict = {
            "max_depth": 5,
            "max_concurrent_per_parent": MAX_DELEGATION_CONCURRENCY,
            "max_total_concurrent": MAX_DELEGATION_QUEUE,
            "allowed_parent_roles": [],
            "cost_budget_per_hour_usd": 0.0,
            "blocked_parent_ids": [],
            "allowed_role_edges": {},
        }
        # 活跃子 agent 计数：parent_id -> int；同时维护全局计数
        self._fork_active_counts: dict = {}
        self._fork_active_total: int = 0

    # ── Risk level helpers ──

    def _normalize_risk(self, risk: str) -> str:
        """Normalize legacy risk names to new 4-tier model."""
        mapping = {"safe": "low", "dangerous": "high"}
        return mapping.get(risk, risk)

    def get_risk(self, tool_name: str) -> str:
        """Get the risk level for a tool (normalized)."""
        raw = self.tool_risk.get(tool_name, "high")
        return self._normalize_risk(raw)

    def set_risk(self, tool_name: str, risk: str):
        """Set risk level for a tool (admin API)."""
        valid = {"red", "high", "moderate", "low"}
        if risk not in valid:
            raise ValueError(f"Invalid risk level '{risk}'. Must be one of: {valid}")
        with self._lock:
            self.tool_risk[tool_name] = risk

    def get_all_risks(self) -> dict[str, str]:
        """Return all tool→risk mappings (normalized)."""
        return {k: self._normalize_risk(v) for k, v in self.tool_risk.items()}

    def can_agent_approve(self, agent_id: str, agent_priority: int,
                          risk_level: str) -> bool:
        """Check if an agent can approve operations of the given risk level."""
        # Check per-agent override first
        if agent_id in self.agent_approval_authority:
            return risk_level in self.agent_approval_authority[agent_id]
        # Fall back to priority-based authority
        allowed = self.priority_approval_authority.get(agent_priority, ["low"])
        return risk_level in allowed

    def check_tool(self, tool_name: str, arguments: dict,
                   agent_id: str = "", agent_name: str = "",
                   agent_priority: int = 3) -> tuple[str, str]:
        """
        Check if a tool call should be allowed.

        Returns: ("allow"|"deny"|"needs_approval"|"agent_approvable", reason)

        Decision flow:
          1. RED → deny (unconditional)
          2. Session pre-approved → allow
          3. Bash special analysis (deny patterns, safe patterns, structural checks)
          4. Sensitive path checks for write/edit
          5. LOW → allow
          6. MODERATE → allow if auto_approve_moderate, else "agent_approvable"
          7. HIGH → "needs_approval" (human/admin only)
        """
        risk = self.get_risk(tool_name)

        # ── Global denylist (admin-configurable) ──
        # Highest precedence after RED: even LOW-risk tools are refused
        # if admin has put them on the global denylist. Used to retire
        # legacy tools like ``create_pptx_advanced`` without touching
        # per-agent profiles.
        if tool_name in self.global_denylist:
            return "deny", (
                f"🚫 Tool '{tool_name}' is on the global denylist. "
                "Admin can remove it from 工具与审批 settings."
            )

        # ── RED — 红线: always deny ──
        if risk == "red":
            return "deny", f"🚫 红线操作: '{tool_name}' is permanently blocked (risk: RED)"

        # ── LOW — 低风险: always allow ──
        if risk == "low":
            return "allow", ""

        # ── Skill-grant shortcut ───────────────────────────────────
        # If admin has granted a skill to this agent via 技能商店
        # ("Grant skill" button), and the skill's manifest declares
        # ``tool_name`` in its tool whitelist, auto-allow the call.
        # Rationale: a skill grant IS an admin approval for the set
        # of tools the skill needs. Re-asking per-call is noise.
        # Only applies when agent_id is provided (REST/agent path).
        if agent_id:
            try:
                import sys as _sys
                mod = _sys.modules.get("app.llm")
                hub = getattr(mod, "_active_hub", None) if mod else None
                reg = getattr(hub, "skill_registry", None) if hub else None
                if reg is not None and hasattr(reg, "list_for_agent"):
                    for inst in reg.list_for_agent(agent_id):
                        whitelist = set(getattr(inst.manifest, "tools", []) or [])
                        if tool_name in whitelist:
                            return "allow", (
                                f"Covered by granted skill "
                                f"'{inst.manifest.name}'")
            except Exception:
                pass  # registry unavailable → fall through to risk check

        # ── Session-scoped pre-approval ──
        if agent_id and (agent_id, tool_name) in self.session_approvals:
            return "allow", "Session-approved"

        # ── Bash special handling — 智能子命令分析 ──
        if tool_name == "bash":
            cmd = arguments.get("command", "")

            # Step 1: 绝对黑名单 (deny patterns — 无论如何都拒绝)
            for pattern in self.deny_patterns:
                if re.search(pattern, cmd, re.IGNORECASE):
                    return "deny", f"🚫 Command matches blocked pattern: {pattern}"

            # Step 2: 结构安全检查 (命令注入、数据外泄)
            if re.search(r'`[^`]+`', cmd) or re.search(r'\$\([^)]+\)', cmd):
                inner = re.findall(r'`([^`]+)`|\$\(([^)]+)\)', cmd)
                for groups in inner:
                    subcmd = groups[0] or groups[1]
                    sensitive_subcmd = any(
                        kw in subcmd.lower()
                        for kw in ("secret", "password", "token", "key",
                                   "credential", "/etc/shadow", "/etc/passwd",
                                   "ssh", ".env", "aws", "api_key")
                    )
                    if sensitive_subcmd:
                        return "deny", f"Command injection accessing sensitive data: {subcmd}"

            if re.search(r'\b(env|printenv|set)\b.*\|\s*(curl|wget|nc)',
                         cmd, re.IGNORECASE):
                return "deny", "Environment variable exfiltration attempt"

            if re.search(r'(base64\s+-d|xxd\s+-r|python.*-c.*exec|eval\s)', cmd,
                         re.IGNORECASE):
                return "needs_approval", "Command uses encoding/eval that may obfuscate intent"

            # Step 3: 删除类命令 → 红线
            # 覆盖: rm, xargs rm, find -delete, find -exec rm
            if re.search(r'\brm\s+', cmd):
                return "deny", "🚫 红线: rm (delete) commands are blocked by default"
            if re.search(r'\bxargs\s+rm\b', cmd):
                return "deny", "🚫 红线: xargs rm (piped delete) is blocked by default"
            if re.search(r'-delete\b', cmd):
                return "deny", "🚫 红线: find -delete is blocked by default"
            if re.search(r'-exec\s+rm\b', cmd):
                return "deny", "🚫 红线: find -exec rm is blocked by default"

            # Step 4: 智能子命令分析 — 拆分并逐个评估
            # 这里是核心改进: 不再一刀切，而是区分每条子命令的实际风险
            cmd_risk, cmd_reason = analyze_bash_command(cmd)

            if cmd_risk == "low":
                # 所有子命令都是只读/安全的 → 自动通过
                return "allow", f"Bash auto-approved: {cmd_reason}"
            elif cmd_risk == "moderate":
                # 包含构建/安装类操作 → 按中风险处理
                if self.auto_approve_moderate:
                    return "allow", f"Bash moderate auto-approved: {cmd_reason}"
                return "agent_approvable", f"Bash moderate: {cmd_reason}"
            # cmd_risk == "high" → 继续走下面的高风险流程

        # ── write_file / edit_file: sensitive path check ──
        if tool_name in ("write_file", "edit_file"):
            path = arguments.get("path", "")
            sensitive = ["/etc/", "/usr/", "/bin/", "/sbin/", "/boot/",
                         "/sys/", "/proc/", "~/.ssh/", "~/.bashrc",
                         ".env", "credentials", "secret", "password"]
            for s in sensitive:
                if s in path.lower():
                    return "needs_approval", f"Writing to sensitive path: {path}"

        # ── MODERATE — 中风险 ──
        if risk == "moderate":
            if self.auto_approve_moderate:
                return "allow", ""
            # Not auto-approved → agent with authority can approve
            return "agent_approvable", f"Tool '{tool_name}' is moderate risk — agent approval or admin approval needed"

        # ── HIGH — 高风险: admin/human approval ──
        return "needs_approval", f"⚠️ 高风险: Tool '{tool_name}' requires admin approval (risk: HIGH)"

    def check_skill_call(self, skill_id: str, skill_name: str,
                         agent_id: str, agent_role: str,
                         mcp_id: str = "", tool_name: str = "") -> tuple[str, str]:
        """技能调用前置审批检查。

        默认 allow；命中 skill_escalation 规则之一即返回 ('needs_approval', reason)。
        SkillRunner 通过 hub._skill_escalation_check 间接调用。
        """
        cfg = self.skill_escalation or {}

        # 1) skill 黑名单
        skills_list = cfg.get("escalate_skills") or []
        if skill_id in skills_list or skill_name in skills_list:
            return "needs_approval", f"skill '{skill_name}' is in escalation list"

        # 2) MCP 黑名单
        mcps_list = cfg.get("escalate_mcps") or []
        if mcp_id and mcp_id in mcps_list:
            return "needs_approval", f"MCP '{mcp_id}' requires approval"

        # 3) 角色限制
        roles_list = cfg.get("escalate_for_roles") or []
        if agent_role and agent_role in roles_list:
            return "needs_approval", f"role '{agent_role}' requires approval for skill calls"

        # 4) 时间窗
        hours = cfg.get("escalate_outside_hours")
        if hours and isinstance(hours, (list, tuple)) and len(hours) == 2:
            try:
                import datetime as _dt
                h = _dt.datetime.now().hour
                start, end = int(hours[0]), int(hours[1])
                in_window = (start <= h < end) if start <= end else (h >= start or h < end)
                if not in_window:
                    return "needs_approval", \
                        f"current hour {h} is outside allowed window {start}-{end}"
            except Exception:
                pass

        # 5) 频率限制
        max_per_hour = int(cfg.get("escalate_if_count_per_hour") or 0)
        if max_per_hour > 0:
            now = time.time()
            key = (skill_id, agent_id)
            with self._lock:
                log = self._skill_call_log.setdefault(key, [])
                # 清理 1 小时前的记录
                log[:] = [t for t in log if now - t < 3600]
                if len(log) >= max_per_hour:
                    return "needs_approval", \
                        f"skill call rate exceeded ({max_per_hour}/hour)"
                log.append(now)

        return "allow", ""

    # ── Sub-agent fork policy ──

    def check_fork_allowed(self, parent_id: str, parent_role: str,
                           parent_depth: int,
                           cost_last_hour_usd: float = 0.0,
                           child_role: str = "") -> tuple[bool, str]:
        """Check whether `parent_id` may spawn a new sub-agent right now.

        Returns (ok, reason). Does NOT mutate counters; call register_fork_start
        after the spawn actually proceeds.

        If `child_role` is provided, also enforces `allowed_role_edges`:
        the (parent_role -> child_role) edge must be in the configured graph.
        """
        cfg = self.fork_policy or {}
        # 1) 黑名单
        blocked = cfg.get("blocked_parent_ids") or []
        if parent_id in blocked:
            return False, f"agent {parent_id} is in fork blocklist"
        # 2) 角色白名单
        allowed = cfg.get("allowed_parent_roles") or []
        if allowed and parent_role and parent_role not in allowed:
            return False, f"role '{parent_role}' is not allowed to spawn sub-agents"
        # 2b) 角色委派图: parent_role -> child_role 边校验
        edges = cfg.get("allowed_role_edges") or {}
        if edges and parent_role and child_role and parent_role in edges:
            allowed_children = edges.get(parent_role) or []
            # 显式空列表 = 该角色不能委派任何子角色
            if not allowed_children:
                return False, (
                    f"role '{parent_role}' is not allowed to delegate to any role "
                    f"(allowed_role_edges['{parent_role}'] is empty)"
                )
            if child_role not in allowed_children:
                return False, (
                    f"role edge '{parent_role}' -> '{child_role}' is not allowed "
                    f"(allowed: {allowed_children})"
                )
        # 3) 深度
        max_depth = cfg.get("max_depth")
        if max_depth is not None and parent_depth >= int(max_depth):
            return False, f"delegate depth {parent_depth} >= max {max_depth}"
        # 4) 单 parent 并发
        with self._lock:
            per_parent = int(cfg.get("max_concurrent_per_parent") or 0)
            if per_parent > 0:
                cur = self._fork_active_counts.get(parent_id, 0)
                if cur >= per_parent:
                    return False, f"parent {parent_id} already has {cur} active children (max {per_parent})"
            # 5) 全局并发
            total_max = int(cfg.get("max_total_concurrent") or 0)
            if total_max > 0 and self._fork_active_total >= total_max:
                return False, f"total active children {self._fork_active_total} >= max {total_max}"
        # 6) 成本预算
        budget = float(cfg.get("cost_budget_per_hour_usd") or 0)
        if budget > 0 and cost_last_hour_usd > budget:
            return False, f"hourly cost ${cost_last_hour_usd:.4f} exceeds budget ${budget:.2f}"
        return True, ""

    def register_fork_start(self, parent_id: str):
        with self._lock:
            self._fork_active_counts[parent_id] = self._fork_active_counts.get(parent_id, 0) + 1
            self._fork_active_total += 1

    def register_fork_end(self, parent_id: str):
        with self._lock:
            cur = self._fork_active_counts.get(parent_id, 0)
            if cur > 0:
                self._fork_active_counts[parent_id] = cur - 1
                if self._fork_active_counts[parent_id] == 0:
                    self._fork_active_counts.pop(parent_id, None)
            if self._fork_active_total > 0:
                self._fork_active_total -= 1

    def get_fork_status(self) -> dict:
        with self._lock:
            return {
                "policy": dict(self.fork_policy or {}),
                "active_total": self._fork_active_total,
                "active_per_parent": dict(self._fork_active_counts),
            }

    def request_approval(self, tool_name: str, arguments: dict,
                         agent_id: str = "", agent_name: str = "",
                         reason: str = "") -> PendingApproval:
        """Create a pending approval request. Blocks until approved/denied/timeout."""
        approval = PendingApproval(
            agent_id=agent_id,
            agent_name=agent_name,
            tool_name=tool_name,
            arguments=arguments,
            reason=reason,
        )
        with self._lock:
            self.pending[approval.approval_id] = approval
        return approval

    def wait_for_approval(self, approval: PendingApproval) -> str:
        """Block until the approval is decided. Returns 'approved' or 'denied'."""
        decided = approval._event.wait(timeout=self.approval_timeout)
        if not decided:
            approval.status = "expired"
            with self._lock:
                self.pending.pop(approval.approval_id, None)
                self.history.append(approval)
            return "denied"
        with self._lock:
            self.pending.pop(approval.approval_id, None)
            self.history.append(approval)
            if len(self.history) > 5000:
                self.history = self.history[-3000:]
        return approval.status

    def approve(self, approval_id: str, decided_by: str = "",
                scope: str = "once") -> bool:
        with self._lock:
            approval = self.pending.get(approval_id)
            if not approval:
                return False
            approval.status = "approved"
            approval.decided_by = decided_by
            approval.decided_at = time.time()
            if scope == "session" and approval.agent_id and approval.tool_name:
                self.session_approvals.add(
                    (approval.agent_id, approval.tool_name)
                )
                # Persist so the approval survives a restart. User-
                # facing semantics: "until an admin revokes it."
                self._save_session_approvals()
        approval._event.set()
        return True

    # ── persistence for session_approvals + global_denylist ────────
    def set_persist_path(self, path: str) -> None:
        """Called by Auth on startup to bind the on-disk files and load
        whatever was persisted from a previous process.

        ``path`` is for session_approvals. The denylist uses a sibling
        file in the same directory.
        """
        self._session_approvals_file = path
        self._global_denylist_file = os.path.join(
            os.path.dirname(path) or ".", "tool_denylist.json"
        )
        self._load_session_approvals()
        self._load_global_denylist()

    def _load_global_denylist(self) -> None:
        p = self._global_denylist_file
        if not p or not os.path.isfile(p):
            # Auto-persist the factory default so the file exists for admin UI.
            self._save_global_denylist()
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            denied = data.get("denied") or []
            with self._lock:
                self.global_denylist = set(
                    str(x).strip() for x in denied if str(x).strip()
                )
        except Exception as e:
            logging.getLogger("tudou.auth").warning(
                "failed to load tool_denylist.json: %s", e)

    def _save_global_denylist(self) -> None:
        p = self._global_denylist_file
        if not p:
            return
        try:
            data = {"denied": sorted(self.global_denylist)}
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception as e:
            logging.getLogger("tudou.auth").warning(
                "failed to save tool_denylist.json: %s", e)

    def add_global_denied_tool(self, tool_name: str) -> bool:
        """Admin: add a tool to the global denylist."""
        tool_name = (tool_name or "").strip()
        if not tool_name:
            return False
        with self._lock:
            if tool_name in self.global_denylist:
                return False
            self.global_denylist.add(tool_name)
            self._save_global_denylist()
        return True

    def remove_global_denied_tool(self, tool_name: str) -> bool:
        """Admin: remove a tool from the global denylist."""
        tool_name = (tool_name or "").strip()
        with self._lock:
            if tool_name not in self.global_denylist:
                return False
            self.global_denylist.discard(tool_name)
            self._save_global_denylist()
        return True

    def list_global_denylist(self) -> list[str]:
        with self._lock:
            return sorted(self.global_denylist)

    def _load_session_approvals(self) -> None:
        p = self._session_approvals_file
        if not p or not os.path.isfile(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            pairs = data.get("approvals") or []
            with self._lock:
                for pair in pairs:
                    if isinstance(pair, list) and len(pair) == 2:
                        self.session_approvals.add((pair[0], pair[1]))
        except Exception as e:
            # Corrupt file should not crash the server; log and skip.
            logging.getLogger("tudou.auth").warning(
                "failed to load tool_approvals.json: %s", e)

    def _save_session_approvals(self) -> None:
        p = self._session_approvals_file
        if not p:
            return
        try:
            data = {"approvals": [list(pair) for pair in self.session_approvals]}
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception as e:
            logging.getLogger("tudou.auth").warning(
                "failed to save tool_approvals.json: %s", e)

    def revoke_session_approval(self, agent_id: str, tool_name: str) -> bool:
        """Admin action: undo a previously granted session approval.
        Returns True if it was present, False otherwise."""
        with self._lock:
            pair = (agent_id, tool_name)
            if pair in self.session_approvals:
                self.session_approvals.discard(pair)
                self._save_session_approvals()
                return True
        return False

    def list_session_approvals(self) -> list[dict]:
        """Admin UI: snapshot of current approvals."""
        with self._lock:
            return [{"agent_id": a, "tool_name": t}
                    for (a, t) in sorted(self.session_approvals)]

    def deny(self, approval_id: str, decided_by: str = "") -> bool:
        with self._lock:
            approval = self.pending.get(approval_id)
            if not approval:
                return False
            approval.status = "denied"
            approval.decided_by = decided_by
            approval.decided_at = time.time()
        approval._event.set()
        return True

    def list_pending(self) -> list[dict]:
        with self._lock:
            # Expire old ones
            now = time.time()
            expired = [aid for aid, a in self.pending.items()
                       if now - a.created_at > self.approval_timeout]
            for aid in expired:
                a = self.pending.pop(aid)
                a.status = "expired"
                a._event.set()
                self.history.append(a)
            return [a.to_dict() for a in self.pending.values()]

    def list_history(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [a.to_dict() for a in self.history[-limit:]]

    # ── Policy config (admin API) ──

    def get_policy_config(self) -> dict:
        """Return the full policy config for admin UI."""
        return {
            "tool_risks": self.get_all_risks(),
            "auto_approve_moderate": self.auto_approve_moderate,
            "approval_timeout": self.approval_timeout,
            "priority_approval_authority": {
                str(k): v for k, v in self.priority_approval_authority.items()
            },
            "agent_approval_overrides": dict(self.agent_approval_authority),
            "risk_levels": [
                {"id": "red", "label": "🚫 红线 Red Line",
                 "desc": "永久禁止，无法覆盖 — Permanently blocked"},
                {"id": "high", "label": "⚠️ 高风险 High",
                 "desc": "需要管理员人工审批 — Requires admin approval"},
                {"id": "moderate", "label": "🟡 中风险 Moderate",
                 "desc": "Agent 可审批或自动通过 — Agent-approvable or auto"},
                {"id": "low", "label": "✅ 低风险 Low",
                 "desc": "自动通过，无需审批 — Always auto-approved"},
            ],
            "priority_labels": {
                "0": "Admin (系统管理员)",
                "1": "CXO (高管)",
                "2": "PM (项目经理)",
                "3": "Team Member (团队成员)",
            },
            "skill_escalation": dict(self.skill_escalation or {}),
            "fork_policy": dict(self.fork_policy or {}),
            "fork_status": {
                "active_total": self._fork_active_total,
                "active_per_parent": dict(self._fork_active_counts),
            },
        }

    def update_policy_config(self, updates: dict):
        """Update policy config from admin UI.

        Accepted keys:
          tool_risks: {tool_name: risk_level, ...}
          auto_approve_moderate: bool
          approval_timeout: float
          priority_approval_authority: {priority_str: [risk_levels], ...}
          agent_approval_overrides: {agent_id: [risk_levels], ...}
        """
        with self._lock:
            if "tool_risks" in updates:
                valid = {"red", "high", "moderate", "low"}
                for tool, risk in updates["tool_risks"].items():
                    if risk in valid:
                        self.tool_risk[tool] = risk

            if "auto_approve_moderate" in updates:
                self.auto_approve_moderate = bool(updates["auto_approve_moderate"])

            if "approval_timeout" in updates:
                self.approval_timeout = max(30, min(
                    float(updates["approval_timeout"]), 3600
                ))

            if "priority_approval_authority" in updates:
                valid = {"red", "high", "moderate", "low"}
                for pri_str, levels in updates["priority_approval_authority"].items():
                    try:
                        pri = int(pri_str)
                        self.priority_approval_authority[pri] = [
                            l for l in levels if l in valid
                        ]
                    except (ValueError, TypeError):
                        pass

            if "agent_approval_overrides" in updates:
                valid = {"red", "high", "moderate", "low"}
                for agent_id, levels in updates["agent_approval_overrides"].items():
                    self.agent_approval_authority[agent_id] = [
                        l for l in levels if l in valid
                    ]

            if "fork_policy" in updates and isinstance(updates["fork_policy"], dict):
                fp = updates["fork_policy"]
                cur = self.fork_policy
                for k in ("max_depth", "max_concurrent_per_parent", "max_total_concurrent"):
                    if k in fp and fp[k] is not None:
                        try:
                            cur[k] = int(fp[k])
                        except (ValueError, TypeError):
                            pass
                if "allowed_parent_roles" in fp and isinstance(fp["allowed_parent_roles"], list):
                    cur["allowed_parent_roles"] = [str(x) for x in fp["allowed_parent_roles"]]
                if "blocked_parent_ids" in fp and isinstance(fp["blocked_parent_ids"], list):
                    cur["blocked_parent_ids"] = [str(x) for x in fp["blocked_parent_ids"]]
                if "cost_budget_per_hour_usd" in fp:
                    try:
                        cur["cost_budget_per_hour_usd"] = float(fp["cost_budget_per_hour_usd"])
                    except (ValueError, TypeError):
                        pass
                if "allowed_role_edges" in fp and isinstance(fp["allowed_role_edges"], dict):
                    cleaned: dict = {}
                    for k, v in fp["allowed_role_edges"].items():
                        if not isinstance(v, list):
                            continue
                        cleaned[str(k)] = [str(x) for x in v if x]
                    cur["allowed_role_edges"] = cleaned

            if "skill_escalation" in updates and isinstance(updates["skill_escalation"], dict):
                se = updates["skill_escalation"]
                cur = self.skill_escalation
                for k in ("escalate_skills", "escalate_mcps", "escalate_for_roles"):
                    if k in se and isinstance(se[k], list):
                        cur[k] = [str(x) for x in se[k]]
                if "escalate_outside_hours" in se:
                    v = se["escalate_outside_hours"]
                    if v is None or (isinstance(v, (list, tuple)) and len(v) == 2):
                        cur["escalate_outside_hours"] = list(v) if v else None
                if "escalate_if_count_per_hour" in se:
                    try:
                        cur["escalate_if_count_per_hour"] = max(0, int(se["escalate_if_count_per_hour"]))
                    except (ValueError, TypeError):
                        pass


# ---------------------------------------------------------------------------
# API Token
# ---------------------------------------------------------------------------

@dataclass
class APIToken:
    token_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "default"
    role: str = "operator"
    token_hash: str = ""
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0
    active: bool = True
    admin_user_id: str = ""  # Bound admin user — determines permissions
    _raw_token: str = field(default="", repr=False)

    def to_dict(self, include_token: bool = False) -> dict:
        d = {
            "token_id": self.token_id,
            "name": self.name,
            "role": self.role,
            "admin_user_id": self.admin_user_id,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "active": self.active,
        }
        if include_token and self._raw_token:
            d["token"] = self._raw_token
        return d


# ---------------------------------------------------------------------------
# Admin User
# ---------------------------------------------------------------------------

@dataclass
class AdminUser:
    """Admin user account for portal authentication."""
    user_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    username: str = ""
    password_hash: str = ""
    salt: str = ""
    role: str = "admin"  # AdminRole value
    display_name: str = ""
    agent_ids: list[str] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)  # Node-scope permission
    created_at: float = field(default_factory=time.time)
    active: bool = True
    last_login: float = 0.0

    def to_dict(self, include_secrets: bool = False) -> dict:
        d = {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "agent_ids": self.agent_ids,
            "node_ids": self.node_ids,
            "created_at": self.created_at,
            "active": self.active,
            "last_login": self.last_login,
        }
        if include_secrets:
            d["password_hash"] = self.password_hash
            d["salt"] = self.salt
        return d


# ---------------------------------------------------------------------------
# Web Session
# ---------------------------------------------------------------------------

@dataclass
class WebSession:
    session_id: str = field(default_factory=lambda: secrets.token_hex(32))
    token_id: str = ""
    role: str = "viewer"
    name: str = ""
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    ip_address: str = ""
    admin_user_id: str = ""  # Set if session is from admin login


# ---------------------------------------------------------------------------
# Audit Log Entry
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    timestamp: float = field(default_factory=time.time)
    action: str = ""
    actor: str = ""
    role: str = ""
    target: str = ""
    detail: str = ""
    ip_address: str = ""
    success: bool = True

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "role": self.role,
            "target": self.target,
            "detail": self.detail[:300],
            "ip_address": self.ip_address,
            "success": self.success,
        }


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateBucket:
    def __init__(self, rate: float = 60, burst: int = 120):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.time()

    def allow(self) -> bool:
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * (self.rate / 60.0))
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# ---------------------------------------------------------------------------
# Admin Manager
# ---------------------------------------------------------------------------

class AdminManager:
    """Manages admin user accounts and authentication."""

    def __init__(self, data_dir: str = ""):
        self._lock = threading.Lock()
        self.admins: dict[str, AdminUser] = {}
        self._data_dir = data_dir or str(Path(__file__).parent)
        self._admin_file: str = os.path.join(self._data_dir, ".tudou_admins.json")

    def init(self):
        """Load admins from disk and create default superAdmin if needed."""
        self._load_admins()
        # Create default superAdmin if none exists
        with self._lock:
            if not self.admins:
                # Generate a secure random password instead of hardcoded one
                generated_pw = secrets.token_urlsafe(16)
                default = self._create_admin_obj(
                    username="admin",
                    password=generated_pw,
                    role=AdminRole.SUPER_ADMIN.value,
                    display_name="System Administrator",
                    agent_ids=[],
                )
                self.admins[default.user_id] = default
                self._save_admins()
                # Print the generated password so the admin can save it
                print("\n" + "=" * 60)
                print("  !! FIRST-LAUNCH: Default SuperAdmin Created !!")
                print(f"  Username : admin")
                print(f"  Password : {generated_pw}")
                print("  ** Save this password — it will NOT be shown again **")
                print("=" * 60 + "\n")

    def _hash_password(self, salt: str, password: str) -> str:
        """Hash password with salt using SHA256."""
        return hashlib.sha256((salt + password).encode()).hexdigest()

    def _create_admin_obj(self, username: str, password: str, role: str,
                         display_name: str, agent_ids: list[str],
                         node_ids: list[str] = None) -> AdminUser:
        """Create an AdminUser object with hashed password."""
        salt = secrets.token_hex(16)
        password_hash = self._hash_password(salt, password)
        admin = AdminUser(
            username=username,
            password_hash=password_hash,
            salt=salt,
            role=role,
            display_name=display_name,
            agent_ids=agent_ids or [],
            node_ids=node_ids or [],
        )
        return admin

    def create_admin(self, username: str, password: str, display_name: str,
                     agent_ids: list[str] = None, node_ids: list[str] = None) -> AdminUser:
        """Create a new regular admin user."""
        with self._lock:
            # Check if username exists
            if any(a.username == username for a in self.admins.values()):
                raise ValueError(f"Admin with username '{username}' already exists")

            admin = self._create_admin_obj(
                username=username,
                password=password,
                role=AdminRole.ADMIN.value,
                display_name=display_name,
                agent_ids=agent_ids or [],
                node_ids=node_ids or [],
            )
            self.admins[admin.user_id] = admin
            self._save_admins()
            return admin

    def update_admin(self, user_id: str, **kwargs) -> AdminUser | None:
        """Update admin fields. Supports: password, display_name, agent_ids, node_ids, active."""
        with self._lock:
            admin = self.admins.get(user_id)
            if not admin:
                return None

            # Handle password separately (needs re-hashing)
            if "password" in kwargs:
                new_password = kwargs.pop("password")
                admin.salt = secrets.token_hex(16)
                admin.password_hash = self._hash_password(admin.salt, new_password)

            # Update other fields
            if "display_name" in kwargs:
                admin.display_name = kwargs["display_name"]
            if "agent_ids" in kwargs:
                admin.agent_ids = kwargs["agent_ids"] or []
            if "node_ids" in kwargs:
                admin.node_ids = kwargs["node_ids"] or []
            if "active" in kwargs:
                admin.active = kwargs["active"]

            self._save_admins()
            return admin

    def delete_admin(self, user_id: str) -> bool:
        """Delete an admin user. Cannot delete superAdmin."""
        with self._lock:
            admin = self.admins.get(user_id)
            if not admin:
                return False
            # Prevent deletion of superAdmin
            if admin.role == AdminRole.SUPER_ADMIN.value:
                raise ValueError("Cannot delete superAdmin")

            self.admins.pop(user_id)
            self._save_admins()
            return True

    def authenticate(self, username: str, password: str) -> AdminUser | None:
        """Authenticate admin by username/password. Returns AdminUser if successful."""
        with self._lock:
            for admin in self.admins.values():
                if admin.username == username and admin.active:
                    # Verify password
                    expected_hash = self._hash_password(admin.salt, password)
                    if admin.password_hash == expected_hash:
                        admin.last_login = time.time()
                        self._save_admins()
                        return admin
        return None

    def get_admin(self, user_id: str) -> AdminUser | None:
        """Get admin by user_id."""
        with self._lock:
            return self.admins.get(user_id)

    def list_admins(self) -> list[dict]:
        """List all admins as dicts (no secrets)."""
        with self._lock:
            return [admin.to_dict(include_secrets=False)
                    for admin in self.admins.values()]

    def bind_agents(self, user_id: str, agent_ids: list[str]) -> AdminUser | None:
        """Set which agents this admin can manage."""
        return self.update_admin(user_id, agent_ids=agent_ids)

    def can_manage_agent(self, user_id: str, agent_id: str, agent_node_id: str = "") -> bool:
        """Check if admin can manage an agent.

        Checks in order: SuperAdmin → node-level permission → agent-level permission.
        """
        admin = self.get_admin(user_id)
        if not admin or not admin.active:
            return False
        # superAdmin can manage all agents
        if admin.role == AdminRole.SUPER_ADMIN.value:
            return True
        # Check node-level permission if provided
        if agent_node_id and agent_node_id in admin.node_ids:
            return True
        # Regular admin can only manage bound agents
        return agent_id in admin.agent_ids

    def can_manage_node(self, user_id: str, node_id: str) -> bool:
        """Check if admin can manage a node."""
        admin = self.get_admin(user_id)
        if not admin or not admin.active:
            return False
        # superAdmin can manage all nodes
        if admin.role == AdminRole.SUPER_ADMIN.value:
            return True
        return node_id in admin.node_ids

    def _get_db(self):
        """获取数据库实例 (lazy)。"""
        try:
            from .infra.database import get_database  # noqa
            return get_database()
        except Exception:
            try:
                from app.infra.database import get_database  # noqa
                return get_database()
            except Exception:
                return None

    def _save_admins(self):
        """Save admins to SQLite + JSON backup."""
        db = self._get_db()
        if db:
            try:
                for admin in self.admins.values():
                    db.save_admin(admin.to_dict(include_secrets=True))
            except Exception:
                pass
        # JSON backup
        try:
            data = [admin.to_dict(include_secrets=True) for admin in self.admins.values()]
            with open(self._admin_file, "w") as f:
                json.dump(data, f, indent=2)
            os.chmod(self._admin_file, 0o600)
        except OSError:
            pass

    def _load_admins(self):
        """Load admins from SQLite (primary) or JSON (fallback)."""
        db = self._get_db()
        if db and db.count("admins") > 0:
            try:
                for item in db.load_admins():
                    admin = AdminUser(
                        user_id=item.get("user_id", ""),
                        username=item.get("username", ""),
                        password_hash=item.get("password_hash", ""),
                        salt=item.get("salt", ""),
                        role=item.get("role", AdminRole.ADMIN.value),
                        display_name=item.get("display_name", ""),
                        agent_ids=item.get("agent_ids", []),
                        created_at=item.get("created_at", time.time()),
                        active=item.get("active", True),
                        last_login=item.get("last_login", 0.0),
                    )
                    self.admins[admin.user_id] = admin
                return
            except Exception:
                pass
        # JSON fallback
        if not os.path.exists(self._admin_file):
            return
        try:
            with open(self._admin_file) as f:
                data = json.load(f)
            for item in data:
                admin = AdminUser(
                    user_id=item.get("user_id", ""),
                    username=item.get("username", ""),
                    password_hash=item.get("password_hash", ""),
                    salt=item.get("salt", ""),
                    role=item.get("role", AdminRole.ADMIN.value),
                    display_name=item.get("display_name", ""),
                    agent_ids=item.get("agent_ids", []),
                    created_at=item.get("created_at", time.time()),
                    active=item.get("active", True),
                    last_login=item.get("last_login", 0.0),
                )
                self.admins[admin.user_id] = admin
        except (json.JSONDecodeError, KeyError, OSError):
            pass


# ---------------------------------------------------------------------------
# Auth Manager (singleton)
# ---------------------------------------------------------------------------

class AuthManager:
    def __init__(self, data_dir: str = ""):
        self._lock = threading.Lock()
        self.tokens: dict[str, APIToken] = {}
        self._token_lookup: dict[str, str] = {}
        self.sessions: dict[str, WebSession] = {}
        self.audit_log: list[AuditEntry] = []
        self._rate_buckets: dict[str, _RateBucket] = {}
        self._shared_secret: str = ""
        self._data_dir = data_dir or str(Path(__file__).parent)
        self._audit_file: str = os.path.join(self._data_dir, "audit.log")
        self.tool_policy: ToolPolicy = ToolPolicy()
        # Bind tool_policy's session-approvals persistence now so the
        # set survives across process restarts. Without this the user
        # re-sees the same approval dialog every time the server restarts.
        self.tool_policy.set_persist_path(
            os.path.join(self._data_dir, "tool_approvals.json")
        )
        self.admin_mgr: AdminManager = AdminManager(data_dir=self._data_dir)
        self.session_ttl = SESSION_TTL
        self.rate_limit = RATE_LIMIT_RPS
        self.rate_burst = RATE_LIMIT_BURST

    def _get_db(self):
        """获取数据库实例 (lazy)。"""
        try:
            from .infra.database import get_database
            return get_database()
        except Exception:
            try:
                from app.infra.database import get_database
                return get_database()
            except Exception:
                return None

    def _load_audit_log(self):
        """Reload audit log from SQLite (primary) or flat file (fallback)."""
        # Try SQLite first
        db = self._get_db()
        if db:
            try:
                rows = db.get_audit_log(limit=2000)
                for d in rows:
                    data = d.get("data", {})
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except Exception:
                            data = {}
                    entry = AuditEntry(
                        timestamp=d.get("timestamp", 0),
                        action=d.get("action", data.get("action", "")),
                        actor=d.get("actor", data.get("actor", "")),
                        role=data.get("role", ""),
                        target=d.get("target", data.get("target", "")),
                        detail=data.get("detail", ""),
                        ip_address=data.get("ip_address", data.get("ip", "")),
                        success=data.get("success", True),
                    )
                    self.audit_log.append(entry)
                # DB returns newest first, reverse to oldest first
                self.audit_log.reverse()
                logger.info("Loaded %d audit entries from SQLite",
                            len(self.audit_log))
                return
            except Exception as e:
                logger.warning("Failed to load audit from SQLite: %s", e)

        # Fallback: read flat file
        if os.path.exists(self._audit_file):
            try:
                loaded = 0
                with open(self._audit_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            entry = AuditEntry(
                                timestamp=d.get("timestamp", 0),
                                action=d.get("action", ""),
                                actor=d.get("actor", ""),
                                role=d.get("role", ""),
                                target=d.get("target", ""),
                                detail=d.get("detail", ""),
                                ip_address=d.get("ip_address", ""),
                                success=d.get("success", True),
                            )
                            self.audit_log.append(entry)
                            loaded += 1
                        except Exception:
                            continue
                # Keep only most recent
                if len(self.audit_log) > 10000:
                    self.audit_log = self.audit_log[-8000:]
                logger.info("Loaded %d audit entries from flat file", loaded)
            except Exception as e:
                logger.warning("Failed to load audit from file: %s", e)

    def init(self, admin_token: str = "", shared_secret: str = ""):
        self._shared_secret = shared_secret or os.environ.get("TUDOU_SECRET", "")
        self._load_tokens()
        self.admin_mgr.init()
        # Reload audit log from persistent storage
        self._load_audit_log()

        # Find the superAdmin user_id for token binding
        super_admin_id = ""
        for a in self.admin_mgr.admins.values():
            if a.role == AdminRole.SUPER_ADMIN.value:
                super_admin_id = a.user_id
                break

        # One key = one token.  Always use the single name "admin".
        if admin_token:
            # Explicit token passed (--admin-token flag): upsert.
            existing = self.validate_token(admin_token)
            if not existing:
                tok = self._create_token_obj("admin", "admin", admin_token)
                tok.admin_user_id = super_admin_id
                self._save_tokens()
            elif not existing.admin_user_id and super_admin_id:
                existing.admin_user_id = super_admin_id
                self._save_tokens()
            return admin_token
        else:
            # No explicit token — generate a fresh one (replaces old "admin").
            raw = secrets.token_hex(24)
            tok = self._create_token_obj("admin", "admin", raw)
            tok.admin_user_id = super_admin_id
            self._save_tokens()
            return raw

    # ---- Token management ----

    def _hash_token(self, raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    def _create_token_obj(self, name: str, role: str, raw_token: str = "") -> APIToken:
        """Create or *replace* a token by name.

        One name ↔ one token.  If a token with the same *name* already
        exists it is removed first so there is never duplication.
        """
        raw = raw_token or secrets.token_hex(24)
        new_hash = self._hash_token(raw)
        token = APIToken(
            name=name, role=role,
            token_hash=new_hash,
            _raw_token=raw,
        )
        with self._lock:
            # Remove any existing token(s) with the same name
            to_remove = [
                tid for tid, t in self.tokens.items()
                if t.name == name
            ]
            for tid in to_remove:
                old = self.tokens.pop(tid)
                self._token_lookup.pop(old.token_hash, None)
            self.tokens[token.token_id] = token
            self._token_lookup[new_hash] = token.token_id
        self._save_tokens()
        return token

    def create_token(self, name: str, role: str,
                     admin_user_id: str = "") -> APIToken:
        if role not in ("admin", "operator", "viewer"):
            raise ValueError(f"Invalid role: {role}")
        tok = self._create_token_obj(name, role)
        if admin_user_id:
            tok.admin_user_id = admin_user_id
            self._save_tokens()
        return tok

    def validate_token(self, raw_token: str) -> APIToken | None:
        h = self._hash_token(raw_token)
        with self._lock:
            token_id = self._token_lookup.get(h)
            if not token_id:
                return None
            token = self.tokens.get(token_id)
            if not token or not token.active:
                return None
            token.last_used = time.time()
            return token

    def revoke_token(self, token_id: str) -> bool:
        with self._lock:
            token = self.tokens.get(token_id)
            if token:
                token.active = False
                self._save_tokens()
                return True
            return False

    def delete_token(self, token_id: str) -> bool:
        with self._lock:
            token = self.tokens.pop(token_id, None)
            if token:
                self._token_lookup.pop(token.token_hash, None)
                self._save_tokens()
                return True
            return False

    def list_tokens(self) -> list[dict]:
        return [t.to_dict() for t in self.tokens.values()]

    # ---- Token persistence ----

    def _save_tokens(self):
        path = os.path.join(self._data_dir, ".tudou_tokens.json")
        data = [{
            "token_id": t.token_id, "name": t.name, "role": t.role,
            "token_hash": t.token_hash, "created_at": t.created_at,
            "active": t.active, "admin_user_id": t.admin_user_id,
        } for t in self.tokens.values()]
        # SQLite primary — full rewrite (clear stale entries first)
        db = self.admin_mgr._get_db() if hasattr(self.admin_mgr, '_get_db') else None
        if db:
            try:
                # Remove tokens from DB that are no longer in memory
                existing_ids = {t["token_id"] for t in data}
                for old in db.load_tokens():
                    old_id = old.get("token_id", old.get("id", ""))
                    if old_id and old_id not in existing_ids:
                        db.delete_token(old_id)
                for t in data:
                    db.save_token(t)
            except Exception:
                pass
        # JSON backup
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _load_tokens(self):
        path = os.path.join(self._data_dir, ".tudou_tokens.json")
        db = self.admin_mgr._get_db() if hasattr(self.admin_mgr, '_get_db') else None
        if db and db.count("tokens") > 0:
            try:
                data = db.load_tokens()
                for item in data:
                    token = APIToken(
                        token_id=item.get("token_id", item.get("id", "")),
                        name=item.get("name", ""),
                        role=item.get("role", ""),
                        token_hash=item.get("token_hash", ""),
                        admin_user_id=item.get("admin_user_id", item.get("admin", "")),
                        created_at=item.get("created_at", time.time()),
                        active=bool(item.get("active", True)),
                    )
                    self.tokens[token.token_id] = token
                    self._token_lookup[token.token_hash] = token.token_id
                return
            except Exception:
                pass
        # JSON fallback
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            # Deduplicate: keep only the newest token per name
            seen_names: dict[str, float] = {}  # name → best created_at
            best: dict[str, dict] = {}         # name → item dict
            for item in data:
                name = item.get("name", "")
                created = item.get("created_at", 0)
                if name not in seen_names or created > seen_names[name]:
                    seen_names[name] = created
                    best[name] = item
            deduped = len(data) != len(best)
            for item in best.values():
                token = APIToken(
                    token_id=item["token_id"], name=item["name"],
                    role=item["role"], token_hash=item["token_hash"],
                    admin_user_id=item.get("admin_user_id", ""),
                    created_at=item.get("created_at", time.time()),
                    active=item.get("active", True),
                )
                self.tokens[token.token_id] = token
                self._token_lookup[token.token_hash] = token.token_id
            if deduped:
                # Rewrite cleaned file
                self._save_tokens()
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    # ---- Web sessions ----

    def create_session(self, token: APIToken, ip: str = "") -> WebSession:
        session = WebSession(
            token_id=token.token_id, role=token.role,
            name=token.name, ip_address=ip,
            admin_user_id=token.admin_user_id,  # Inherit admin binding from token
        )
        with self._lock:
            self.sessions[session.session_id] = session
        return session

    def login_admin(self, username: str, password: str, ip: str = "") -> WebSession | None:
        """
        Authenticate admin user and create a session.
        Returns WebSession if successful, None otherwise.
        """
        admin = self.admin_mgr.authenticate(username, password)
        if not admin:
            return None

        # Determine role based on admin role
        if admin.role == AdminRole.SUPER_ADMIN.value:
            role = Role.ADMIN.value
        else:
            role = Role.OPERATOR.value

        session = WebSession(
            token_id="",  # No token for admin sessions
            role=role,
            name=admin.username,
            ip_address=ip,
            admin_user_id=admin.user_id,
        )
        with self._lock:
            self.sessions[session.session_id] = session
        return session

    def validate_session(self, session_id: str) -> WebSession | None:
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            if time.time() - session.created_at > self.session_ttl:
                del self.sessions[session_id]
                return None
            # For admin sessions, no token_id check needed
            if session.admin_user_id:
                # Verify admin is still active
                admin = self.admin_mgr.get_admin(session.admin_user_id)
                if not admin or not admin.active:
                    del self.sessions[session_id]
                    return None
            else:
                # For token-based sessions, verify token
                token = self.tokens.get(session.token_id)
                if not token or not token.active:
                    del self.sessions[session_id]
                    return None
            session.last_active = time.time()
            return session

    def invalidate_session(self, session_id: str):
        with self._lock:
            self.sessions.pop(session_id, None)

    # ---- Shared secret ----

    def set_secret(self, secret: str):
        self._shared_secret = secret

    def verify_secret(self, provided: str) -> bool:
        if not self._shared_secret:
            return True
        return hmac.compare_digest(self._shared_secret, provided)

    # ---- Rate limiting ----

    def check_rate_limit(self, ip: str) -> bool:
        with self._lock:
            if ip not in self._rate_buckets:
                self._rate_buckets[ip] = _RateBucket(self.rate_limit, self.rate_burst)
            return self._rate_buckets[ip].allow()

    # ---- Audit logging ----

    def audit(self, action: str, actor: str = "", role: str = "",
              target: str = "", detail: str = "", ip: str = "",
              success: bool = True):
        entry = AuditEntry(
            action=action, actor=actor, role=role,
            target=target, detail=detail[:500],
            ip_address=ip, success=success,
        )
        with self._lock:
            self.audit_log.append(entry)
            if len(self.audit_log) > 10000:
                self.audit_log = self.audit_log[-8000:]
        # Persist to SQLite
        db = self._get_db()
        if db:
            try:
                db.add_audit(actor=actor, action=action, target=target,
                             data=entry.to_dict())
            except Exception:
                pass
        # Also write to flat file (backup)
        try:
            with open(self._audit_file, "a") as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass
        # Forward to upstream hub for cross-node aggregation (best-effort, non-blocking)
        try:
            import threading as _t
            _t.Thread(target=_forward_audit_to_hub,
                      args=(entry.to_dict(),), daemon=True).start()
        except Exception:
            pass

    def ingest_remote_audit(self, entries: list, source_node: str = ""):
        """Receive audit entries forwarded from a downstream node.
        Stores them tagged with the source node id, without re-forwarding.
        """
        if not entries:
            return 0
        count = 0
        with self._lock:
            for raw in entries:
                if not isinstance(raw, dict):
                    continue
                try:
                    e = AuditEntry(
                        action=raw.get("action", ""),
                        actor=raw.get("actor", ""),
                        role=raw.get("role", ""),
                        target=raw.get("target", ""),
                        detail=str(raw.get("detail", ""))[:500],
                        ip_address=raw.get("ip_address", ""),
                        success=bool(raw.get("success", True)),
                    )
                    if "timestamp" in raw:
                        try:
                            e.timestamp = float(raw["timestamp"])
                        except (ValueError, TypeError):
                            pass
                    # Tag origin node in detail prefix for filtering
                    if source_node:
                        e.detail = f"[node:{source_node}] {e.detail}"
                    self.audit_log.append(e)
                    count += 1
                except Exception:
                    continue
            if len(self.audit_log) > 10000:
                self.audit_log = self.audit_log[-8000:]
        # Persist
        db = self._get_db()
        if db:
            for raw in entries:
                if isinstance(raw, dict):
                    try:
                        db.add_audit(
                            actor=raw.get("actor", ""),
                            action=raw.get("action", ""),
                            target=raw.get("target", ""),
                            data={**raw, "_source_node": source_node},
                        )
                    except Exception:
                        pass
        return count

    def get_audit_log(self, limit: int = 200, action: str = "",
                      actor: str = "") -> list[dict]:
        with self._lock:
            entries = self.audit_log
            if action:
                entries = [e for e in entries if e.action == action]
            if actor:
                entries = [e for e in entries if e.actor == actor]
            return [e.to_dict() for e in entries[-limit:]]

    # ---- HTTP auth helpers ----

    def authenticate_request(self, handler: Any) -> tuple[str, str, str] | None:
        ip = handler.client_address[0] if hasattr(handler, "client_address") else ""
        if not self.check_rate_limit(ip):
            return None
        # Bearer token
        auth_header = handler.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            raw_token = auth_header[7:].strip()
            token = self.validate_token(raw_token)
            if token:
                return (token.name, token.role, ip)
        # Session cookie
        cookie_header = handler.headers.get("Cookie", "")
        if cookie_header:
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_header)
                if "td_sess" in cookie:
                    session_id = cookie["td_sess"].value
                    session = self.validate_session(session_id)
                    if session:
                        return (session.name, session.role, ip)
            except Exception:
                pass
        return None

    def require_auth(self, handler: Any, action: str = "") -> tuple[str, str, str] | None:
        result = self.authenticate_request(handler)
        if result is None:
            return None
        actor, role, ip = result
        if action and not Role(role).can(action):
            self.audit(action, actor, role, ip=ip, success=False,
                       detail=f"Permission denied: {action}")
            return None
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cross-node audit forwarding
# ---------------------------------------------------------------------------
# 节点本地 audit() 时把条目以 best-effort 方式 batch 发到 upstream hub。
# 启用条件：环境变量 TUDOU_UPSTREAM_HUB 已设置（当前节点是 remote 节点）。
# 失败重试由 _audit_forward_buffer 内部管理（最多保留最近 500 条）。

_audit_forward_buffer: list = []
_audit_forward_lock = threading.Lock()
_audit_forward_last_flush: float = 0.0
_AUDIT_FORWARD_FLUSH_INTERVAL = 5.0  # 秒
_AUDIT_FORWARD_MAX_BUFFER = 500


def _forward_audit_to_hub(entry_dict: dict):
    """Best-effort: 把单条 audit 加入缓冲，到达时间窗或缓冲上限时批量发送。"""
    import os as _os
    upstream = _os.environ.get("TUDOU_UPSTREAM_HUB", "").rstrip("/")
    if not upstream:
        return
    secret = _os.environ.get("TUDOU_UPSTREAM_SECRET", "")
    node_id = _os.environ.get("TUDOU_NODE_ID", "") or _os.environ.get("HOSTNAME", "")
    global _audit_forward_last_flush
    now = time.time()
    flush_now = False
    with _audit_forward_lock:
        _audit_forward_buffer.append(entry_dict)
        if len(_audit_forward_buffer) > _AUDIT_FORWARD_MAX_BUFFER:
            del _audit_forward_buffer[:len(_audit_forward_buffer) - _AUDIT_FORWARD_MAX_BUFFER]
        if (len(_audit_forward_buffer) >= 50 or
                now - _audit_forward_last_flush >= _AUDIT_FORWARD_FLUSH_INTERVAL):
            batch = list(_audit_forward_buffer)
            _audit_forward_buffer.clear()
            _audit_forward_last_flush = now
            flush_now = True
        else:
            batch = []
    if not flush_now or not batch:
        return
    try:
        import urllib.request as _ur
        import urllib.error as _ue
        payload = json.dumps({
            "source_node": node_id,
            "entries": batch,
        }).encode("utf-8")
        req = _ur.Request(
            f"{upstream}/api/hub/audit/ingest",
            data=payload,
            headers={"Content-Type": "application/json",
                     "X-Hub-Secret": secret},
            method="POST",
        )
        _ur.urlopen(req, timeout=5)
    except Exception:
        # Re-queue on failure (cap to buffer size)
        with _audit_forward_lock:
            _audit_forward_buffer[:0] = batch[:_AUDIT_FORWARD_MAX_BUFFER - len(_audit_forward_buffer)]


def _safe_truncate(d: dict, max_len: int = 200) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > max_len:
            out[k] = v[:max_len] + "..."
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_auth: AuthManager | None = None
_auth_lock = threading.Lock()


def get_auth() -> AuthManager:
    global _auth
    if _auth is None:
        with _auth_lock:
            if _auth is None:
                _auth = AuthManager()
    return _auth


def init_auth(data_dir: str = "", admin_token: str = "",
              shared_secret: str = "") -> tuple[AuthManager, str]:
    global _auth
    with _auth_lock:
        _auth = AuthManager(data_dir=data_dir)
        raw = _auth.init(admin_token=admin_token, shared_secret=shared_secret)
    return _auth, raw
