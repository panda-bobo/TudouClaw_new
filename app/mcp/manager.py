"""
MCP Manager — Node-level Model Context Protocol (MCP) configuration and management system.

Provides central management for MCP servers across nodes, including:
- MCP capability discovery from built-in catalog
- Per-node MCP installation and configuration
- Agent-to-MCP binding and environment overrides
- Persistence and synchronization with remote nodes

Uses JSON persistence to ~/.tudou_claw/mcp_configs.json
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("tudou.mcp_manager")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.DEBUG)

from app.agent import MCPServerConfig  # absolute: app/agent.py, sibling of app/mcp/


# ---------------------------------------------------------------------------
# MCP Capability — Describes what an MCP server can do
# ---------------------------------------------------------------------------

@dataclass
class MCPCapability:
    """Describes the capabilities and configuration of an MCP server type.

    scope:
      - "global": 调用外部 API / 云服务，配置一次即可同步到所有 Node
                   (例: Slack, GitHub, Email — 只需 API Token，无需本地安装)
      - "node":   需要本地安装二进制或依赖本机资源，每个 Node 独立安装
                   (例: Filesystem, Docker, Browser, Memory — 依赖本地进程)
    """
    id: str
    name: str
    description: str
    server_type: str  # "filesystem" | "database" | "api" | "search" | "communication" | "custom"
    transport: str    # "stdio" | "sse" | "streamable-http"
    command_template: str  # e.g. "npx @modelcontextprotocol/server-filesystem {working_dir}"
    url_template: str = ""  # e.g. "http://localhost:{port}/mcp"
    required_env: list[str] = field(default_factory=list)  # ["API_KEY", ...]
    optional_env: list[str] = field(default_factory=list)  # ["TIMEOUT", ...]
    tools_provided: list[str] = field(default_factory=list)  # ["read_file", "write_file", ...]
    compatible_roles: list[str] = field(default_factory=list)  # ["dev", "analyst", ...]
    install_command: str = ""
    version: str = "1.0.0"
    notes: str = ""  # Extra info (e.g. community vs official package, caveats)
    scope: str = "node"  # "global" | "node"

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> MCPCapability:
        """Deserialize from dictionary."""
        return MCPCapability(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            server_type=d.get("server_type", "custom"),
            transport=d.get("transport", "stdio"),
            command_template=d.get("command_template", ""),
            url_template=d.get("url_template", ""),
            required_env=d.get("required_env", []),
            optional_env=d.get("optional_env", []),
            tools_provided=d.get("tools_provided", []),
            compatible_roles=d.get("compatible_roles", []),
            install_command=d.get("install_command", ""),
            version=d.get("version", "1.0.0"),
            notes=d.get("notes", ""),
            scope=d.get("scope", "node"),
        )


# ---------------------------------------------------------------------------
# Node-level MCP Configuration
# ---------------------------------------------------------------------------

@dataclass
class NodeMCPConfig:
    """MCP configuration scoped to a specific node."""
    node_id: str
    available_mcps: dict[str, MCPServerConfig] = field(default_factory=dict)  # mcp_id -> MCPServerConfig
    agent_bindings: dict[str, list[str]] = field(default_factory=dict)  # agent_id -> [mcp_ids]
    env_overrides: dict[str, dict[str, str]] = field(default_factory=dict)  # mcp_id -> {key: value}
    # Per-agent MCP env overrides: agent_id -> mcp_id -> {key: value}
    # Allows different agents to use the same MCP with different configs
    # (e.g., Agent A uses Gmail, Agent B uses QQ Mail, both via "email" MCP)
    agent_env_overrides: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def add_mcp(self, config: MCPServerConfig) -> MCPServerConfig:
        """添加或更新MCP配置到此节点。(Add or update MCP config to this node.)"""
        self.available_mcps[config.id] = config
        self.updated_at = time.time()
        logger.info(f"Added MCP {config.id} ({config.name}) to node {self.node_id}")
        return config

    def remove_mcp(self, mcp_id: str) -> bool:
        """从节点移除MCP。(Remove MCP from this node.)"""
        if mcp_id not in self.available_mcps:
            return False
        del self.available_mcps[mcp_id]
        # Also remove any bindings for this MCP
        for agent_id in list(self.agent_bindings.keys()):
            if mcp_id in self.agent_bindings[agent_id]:
                self.agent_bindings[agent_id].remove(mcp_id)
        # Remove env overrides
        self.env_overrides.pop(mcp_id, None)
        # Remove agent-specific env overrides for this MCP
        for agent_id in list(self.agent_env_overrides.keys()):
            self.agent_env_overrides[agent_id].pop(mcp_id, None)
            if not self.agent_env_overrides[agent_id]:
                del self.agent_env_overrides[agent_id]
        self.updated_at = time.time()
        logger.info(f"Removed MCP {mcp_id} from node {self.node_id}")
        return True

    def bind_agent(self, agent_id: str, mcp_id: str) -> bool:
        """绑定MCP到代理。(Bind MCP to an agent.)"""
        if mcp_id not in self.available_mcps:
            logger.error(f"MCP {mcp_id} not available on node {self.node_id}")
            return False
        if agent_id not in self.agent_bindings:
            self.agent_bindings[agent_id] = []
        if mcp_id not in self.agent_bindings[agent_id]:
            self.agent_bindings[agent_id].append(mcp_id)
            logger.info(f"Bound MCP {mcp_id} to agent {agent_id} on node {self.node_id}")
        return True

    def unbind_agent(self, agent_id: str, mcp_id: str) -> bool:
        """从代理解绑MCP。(Unbind MCP from an agent.)"""
        if agent_id in self.agent_bindings and mcp_id in self.agent_bindings[agent_id]:
            self.agent_bindings[agent_id].remove(mcp_id)
            if not self.agent_bindings[agent_id]:
                del self.agent_bindings[agent_id]
            logger.info(f"Unbound MCP {mcp_id} from agent {agent_id} on node {self.node_id}")
            return True
        return False

    def get_agent_mcps(self, agent_id: str) -> list[MCPServerConfig]:
        """获取代理绑定的MCP列表。(Get list of MCPs bound to this agent.)"""
        mcp_ids = self.agent_bindings.get(agent_id, [])
        return [self.available_mcps[mid] for mid in mcp_ids if mid in self.available_mcps]

    def set_env_override(self, mcp_id: str, key: str, value: str) -> None:
        """为此节点的MCP设置环境变量覆盖。(Set node-specific env var override for an MCP.)"""
        if mcp_id not in self.env_overrides:
            self.env_overrides[mcp_id] = {}
        self.env_overrides[mcp_id][key] = value
        self.updated_at = time.time()
        logger.debug(f"Set env override {key} for MCP {mcp_id} on node {self.node_id}")

    def get_env_overrides(self, mcp_id: str) -> dict[str, str]:
        """获取特定MCP的环境变量覆盖。(Get env var overrides for specific MCP.)"""
        return self.env_overrides.get(mcp_id, {})

    def set_agent_env_override(self, agent_id: str, mcp_id: str, key: str, value: str) -> None:
        """为特定Agent的MCP设置环境变量覆盖。
        (Set agent-specific env override for an MCP, e.g. different SMTP credentials per agent.)"""
        if agent_id not in self.agent_env_overrides:
            self.agent_env_overrides[agent_id] = {}
        if mcp_id not in self.agent_env_overrides[agent_id]:
            self.agent_env_overrides[agent_id][mcp_id] = {}
        self.agent_env_overrides[agent_id][mcp_id][key] = value
        self.updated_at = time.time()
        logger.debug(f"Set agent env override {key} for agent {agent_id} MCP {mcp_id}")

    def get_agent_env_overrides(self, agent_id: str, mcp_id: str) -> dict[str, str]:
        """获取特定Agent特定MCP的环境变量覆盖。
        (Get agent-specific env overrides for a specific MCP.)"""
        return self.agent_env_overrides.get(agent_id, {}).get(mcp_id, {})

    def get_all_agent_env_overrides(self, agent_id: str) -> dict[str, dict[str, str]]:
        """获取特定Agent所有MCP的环境变量覆盖。
        (Get all agent-specific env overrides keyed by mcp_id.)"""
        return self.agent_env_overrides.get(agent_id, {})

    def remove_agent_env_overrides(self, agent_id: str, mcp_id: str) -> None:
        """移除特定Agent特定MCP的所有覆盖。(Remove all agent-specific overrides for a MCP.)"""
        if agent_id in self.agent_env_overrides:
            self.agent_env_overrides[agent_id].pop(mcp_id, None)
            if not self.agent_env_overrides[agent_id]:
                del self.agent_env_overrides[agent_id]
            self.updated_at = time.time()

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "node_id": self.node_id,
            "available_mcps": {
                mid: cfg.to_dict() for mid, cfg in self.available_mcps.items()
            },
            "agent_bindings": self.agent_bindings,
            "env_overrides": self.env_overrides,
            "agent_env_overrides": self.agent_env_overrides,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: dict) -> NodeMCPConfig:
        """Deserialize from dictionary."""
        nc = NodeMCPConfig(node_id=d.get("node_id", ""))
        nc.created_at = d.get("created_at", 0)
        nc.updated_at = d.get("updated_at", 0)
        for mid, cfg in d.get("available_mcps", {}).items():
            nc.available_mcps[mid] = MCPServerConfig.from_dict(cfg)
        nc.agent_bindings = d.get("agent_bindings", {})
        nc.env_overrides = d.get("env_overrides", {})
        nc.agent_env_overrides = d.get("agent_env_overrides", {})
        return nc


# ---------------------------------------------------------------------------
# Built-in MCP Catalog
# ---------------------------------------------------------------------------

MCP_CATALOG: dict[str, MCPCapability] = {
    # ══════════════════════════════════════════════════════════════════
    # Node-level MCPs — 需要本地安装，依赖本机进程/资源
    # ══════════════════════════════════════════════════════════════════
    "filesystem": MCPCapability(
        id="filesystem",
        name="Filesystem Access",
        description="File system read/write/list operations",
        server_type="filesystem",
        transport="stdio",
        command_template="npx @modelcontextprotocol/server-filesystem {working_dir}",
        required_env=[],
        optional_env=["MAX_FILE_SIZE"],
        tools_provided=["read_file", "write_file", "list_files", "create_directory"],
        compatible_roles=["dev", "analyst", "admin"],
        install_command="npm install -g @modelcontextprotocol/server-filesystem",
        scope="node",
    ),
    "docker": MCPCapability(
        id="docker",
        name="Docker Container Management",
        description="Docker container and image operations",
        server_type="api",
        transport="stdio",
        command_template="uvx docker-mcp",
        required_env=[],
        optional_env=["DOCKER_HOST", "DOCKER_TLS_VERIFY"],
        tools_provided=["run_container", "build_image", "list_containers", "logs"],
        compatible_roles=["dev", "devops", "admin"],
        install_command="uv tool install docker-mcp",
        notes="社区实现 (非官方)。需先安装 uv 且本机有 docker daemon。",
        scope="node",
    ),
    "kubernetes": MCPCapability(
        id="kubernetes",
        name="Kubernetes Cluster",
        description="Kubernetes cluster management and operations",
        server_type="api",
        transport="stdio",
        command_template="npx mcp-server-kubernetes",
        required_env=[],
        optional_env=["KUBECONFIG", "K8S_NAMESPACE"],
        tools_provided=["apply_manifest", "get_pods", "delete_resource", "scale_deployment"],
        compatible_roles=["devops", "admin", "sre"],
        install_command="npm install -g mcp-server-kubernetes",
        notes="社区实现 (非官方)。会读取本机 ~/.kube/config。",
        scope="node",
    ),
    "browser": MCPCapability(
        id="browser",
        name="Web Browser (Puppeteer)",
        description="Web browsing and content extraction via Puppeteer",
        server_type="api",
        transport="stdio",
        command_template="npx -y @modelcontextprotocol/server-puppeteer",
        required_env=[],
        optional_env=["PUPPETEER_HEADLESS", "PUPPETEER_ARGS"],
        tools_provided=["puppeteer_navigate", "puppeteer_screenshot",
                        "puppeteer_click", "puppeteer_fill", "puppeteer_evaluate"],
        compatible_roles=["analyst", "dev", "test"],
        install_command="npm install -g @modelcontextprotocol/server-puppeteer",
        notes="官方 MCP server。首次运行会下载 Chromium (~170MB)。",
        scope="node",
    ),
    "browser_automation": MCPCapability(
        id="browser_automation",
        name="Web Browser Automation (Playwright)",
        description="网页自动化: 导航、登录、填表、点击、截图、提取文本、下载文件。基于 Playwright。",
        server_type="api",
        transport="stdio",
        command_template="python -m app.mcp.builtins.browser_automation",
        required_env=[],
        optional_env=["BROWSER_HEADLESS"],
        tools_provided=["browser_navigate", "browser_screenshot", "browser_get_text",
                        "browser_fill", "browser_click", "browser_evaluate",
                        "browser_download", "browser_close"],
        compatible_roles=["dev", "analyst", "test", "admin"],
        install_command="pip install playwright && playwright install chromium",
        notes="内置 MCP。需安装 playwright + chromium。支持登录、表单填写、页面操作、文件下载等 web 自动化。",
        scope="node",
    ),
    "memory": MCPCapability(
        id="memory",
        name="Persistent Memory",
        description="Long-term knowledge and memory storage",
        server_type="custom",
        transport="stdio",
        command_template="npx @modelcontextprotocol/server-memory",
        required_env=[],
        optional_env=["MEMORY_BACKEND"],
        tools_provided=["store_fact", "recall_fact", "list_facts"],
        compatible_roles=["all"],
        install_command="npm install -g @modelcontextprotocol/server-memory",
        scope="node",
    ),
    "chromadb": MCPCapability(
        id="chromadb",
        name="ChromaDB Vector Search",
        description="向量语义搜索 (ChromaDB + Sentence Transformers)。"
                    "为 Agent 记忆系统提供高质量语义检索，自动 fallback 到 FTS5 关键词搜索。",
        server_type="search",
        transport="stdio",
        command_template="python -m app.mcp.builtins.chromadb",
        required_env=[],
        optional_env=["CHROMA_PERSIST_DIR", "CHROMA_EMBEDDING_MODEL", "CHROMA_COLLECTION_PREFIX"],
        tools_provided=["vector_search", "vector_store", "vector_delete",
                        "collection_list", "collection_stats"],
        compatible_roles=["all"],
        install_command="pip install chromadb sentence-transformers",
        notes=("内置 MCP server。安装 chromadb + sentence-transformers 后自动启用。"
               "默认使用 all-MiniLM-L6-v2 模型 (约 90MB)。首次使用会下载模型。"),
        scope="node",
    ),

    # ══════════════════════════════════════════════════════════════════
    # Global-level MCPs — 调用外部 API / 云服务，配置一次同步所有 Node
    # ══════════════════════════════════════════════════════════════════
    "postgres": MCPCapability(
        id="postgres",
        name="PostgreSQL Database",
        description="PostgreSQL database access and query execution",
        server_type="database",
        transport="stdio",
        command_template="npx @modelcontextprotocol/server-postgres",
        required_env=["DB_URL"],
        optional_env=["DB_POOL_SIZE", "QUERY_TIMEOUT"],
        tools_provided=["execute_query", "list_tables", "describe_table", "transaction"],
        compatible_roles=["dev", "analyst", "dba"],
        install_command="npm install -g @modelcontextprotocol/server-postgres",
        scope="global",
    ),
    "mysql": MCPCapability(
        id="mysql",
        name="MySQL Database",
        description="MySQL/MariaDB database access and query execution",
        server_type="database",
        transport="stdio",
        command_template="uvx mysql-mcp-server",
        required_env=["MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"],
        optional_env=["MYSQL_PORT"],
        tools_provided=["execute_sql", "list_tables", "describe_table"],
        compatible_roles=["dev", "analyst", "dba"],
        install_command="uv tool install mysql-mcp-server",
        notes="社区实现 (非官方)。需先安装 uv。",
        scope="global",
    ),
    "redis": MCPCapability(
        id="redis",
        name="Redis Cache",
        description="Redis key-value store operations",
        server_type="database",
        transport="stdio",
        command_template="npx @modelcontextprotocol/server-redis",
        required_env=["REDIS_URL"],
        optional_env=["REDIS_PASSWORD"],
        tools_provided=["get_key", "set_key", "delete_key", "list_keys"],
        compatible_roles=["dev", "admin"],
        install_command="npm install -g @modelcontextprotocol/server-redis",
        scope="global",
    ),
    "elasticsearch": MCPCapability(
        id="elasticsearch",
        name="Elasticsearch Search",
        description="Elasticsearch search and analytics operations",
        server_type="search",
        transport="stdio",
        command_template="# 需自备 MCP server (例: uvx elasticsearch-mcp-server)",
        required_env=["ES_HOST"],
        optional_env=["ES_PORT", "ES_AUTH"],
        tools_provided=["search", "index_document", "delete_document"],
        compatible_roles=["analyst", "dev"],
        install_command="",
        notes="⚠️ 暂无现成官方/标准社区包，请自行提供 MCP 服务器命令。",
        scope="global",
    ),
    "slack": MCPCapability(
        id="slack",
        name="Slack Messaging",
        description="Send and receive Slack messages",
        server_type="communication",
        transport="stdio",
        command_template="npx @modelcontextprotocol/server-slack",
        required_env=["SLACK_BOT_TOKEN"],
        optional_env=["SLACK_SIGNING_SECRET"],
        tools_provided=["send_message", "read_message", "list_channels", "post_thread"],
        compatible_roles=["team", "manager", "admin"],
        install_command="npm install -g @modelcontextprotocol/server-slack",
        scope="global",
    ),
    "github": MCPCapability(
        id="github",
        name="GitHub Repository",
        description="GitHub repository operations (push, PR, issues)",
        server_type="api",
        transport="stdio",
        command_template="npx @modelcontextprotocol/server-github",
        required_env=["GITHUB_TOKEN"],
        optional_env=["GITHUB_OWNER", "GITHUB_REPO"],
        tools_provided=["create_pr", "list_issues", "push_code", "create_release"],
        compatible_roles=["dev"],
        install_command="npm install -g @modelcontextprotocol/server-github",
        scope="global",
    ),
    "email": MCPCapability(
        id="email",
        name="Email (SMTP/IMAP)",
        description="Send and receive email via SMTP/IMAP",
        server_type="communication",
        transport="stdio",
        command_template="uvx mcp-email-server stdio",
        required_env=["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"],
        optional_env=["IMAP_HOST", "SMTP_PORT"],
        tools_provided=["send_email", "read_email", "list_mailbox", "create_draft", "download_attachment"],
        compatible_roles=["admin", "team", "manager"],
        install_command="uv tool install mcp-email-server  # or: pipx install mcp-email-server",
        notes=("社区实现 (非官方)。需要先安装 uv: `pip install uv`，然后 "
               "`uv tool install mcp-email-server`。Gmail 用户需用应用专用密码而非账号密码。"),
        scope="global",
    ),
    "agentmail": MCPCapability(
        id="agentmail",
        name="AgentMail",
        description="Send and receive email via AgentMail API (supports attachments, HTML, CC/BCC)",
        server_type="communication",
        transport="stdio",
        command_template="python -m app.mcp.builtins.agentmail",
        required_env=["AGENTMAIL_API_KEY"],
        optional_env=["AGENTMAIL_INBOX_ID"],
        tools_provided=["send_email", "read_email", "list_inbox", "download_attachment"],
        compatible_roles=["admin", "team", "manager"],
        install_command="pip install agentmail --break-system-packages",
        notes=("AgentMail API 邮件服务 (https://agentmail.to)。内置 MCP，无需额外安装服务端。"
               "需要 API Key（以 am_ 开头）和 inbox ID。支持附件、HTML、CC/BCC。"),
        scope="global",
    ),
}


# ---------------------------------------------------------------------------
# ToolManifestCache — runtime fact: "what tools does this MCP expose"
# ---------------------------------------------------------------------------
#
# Invariant (#4 — Tool manifest authority)
# ────────────────────────────────────────
# Any MCP that has completed a successful handshake at least once must
# have its tool manifest known to the system and readable from here.
# Agents, the Portal, and the ``list_mcps`` client all consult this
# cache so that no component ever asks the agent (or the user) to guess
# tool names.
#
# Trust boundary
# ──────────────
# The tool names, descriptions, and inputSchemas come from the MCP
# server itself, which is untrusted data. This cache is strictly
# documentation — no authorization decision is ever made on the
# contents. Authorization is still per-MCP, not per-tool. Before
# rendering any cached value into an agent prompt or HTML page, the
# consumer MUST HTML-escape / markdown-escape the strings.
#
# Cache lifecycle
# ───────────────
# - Populated by: ``test_mcp_connection`` (on success), boot-time
#   background preload, and on-demand ``refresh_tool_manifest``.
# - Invalidated by: any CRUD on the MCP config (add/update/delete at
#   node or global level), or explicit ``invalidate_tool_manifest``.
# - Keyed by: the MCP id alone. Node-level bindings share the cache
#   entry because the dispatcher path is the same.
# - Persistence: in-memory only in this cut. Re-probed on next boot by
#   the startup preload thread. Persistence is a clean add later
#   without touching any reader.

from dataclasses import dataclass as _dc_cache, field as _fld_cache


@_dc_cache
class ToolManifestEntry:
    """One MCP's discovered tool list + the signature it was fetched for.

    ``config_sig`` is a stable fingerprint of the MCPServerConfig that
    produced this entry. A config whose ``command`` or ``env`` drifts
    from the cached ``config_sig`` forces a re-probe. Values (including
    secrets) are already in the manager's memory, so hashing them here
    does not expand the secret's blast radius.
    """
    mcp_id: str = ""
    tools: list[dict] = _fld_cache(default_factory=list)   # [{name, description, inputSchema}, ...]
    server_info: dict = _fld_cache(default_factory=dict)
    fetched_at: float = 0.0
    config_sig: str = ""
    # Last error string if the most recent refresh failed. An entry
    # can have both ``tools`` (from a previous success) and an
    # ``error`` (from a later failed refresh) — consumers should show
    # the tools and annotate them as "stale since: <error>".
    error: str = ""


class ToolManifestCache:
    """In-memory map mcp_id → ToolManifestEntry."""

    def __init__(self) -> None:
        import threading as _t
        self._lock = _t.RLock()
        self._entries: dict[str, ToolManifestEntry] = {}

    @staticmethod
    def compute_sig(config: "MCPServerConfig") -> str:
        """Stable fingerprint of the fields that can change tool list.

        ``command`` is obvious. ``env`` matters because API keys can
        unlock or hide tools on some servers. ``transport`` matters
        because a switch from stdio to http changes the probe path.
        """
        import hashlib
        parts = [
            getattr(config, "transport", "") or "",
            getattr(config, "command", "") or "",
        ]
        env = getattr(config, "env", {}) or {}
        for k in sorted(env.keys()):
            parts.append(f"{k}={env.get(k, '')}")
        blob = "\x1f".join(parts).encode("utf-8", errors="replace")
        return hashlib.sha1(blob).hexdigest()[:16]

    def put(
        self,
        mcp_id: str,
        tools: list[dict],
        server_info: dict,
        config_sig: str,
    ) -> None:
        with self._lock:
            self._entries[mcp_id] = ToolManifestEntry(
                mcp_id=mcp_id,
                tools=list(tools or []),
                server_info=dict(server_info or {}),
                fetched_at=time.time(),
                config_sig=config_sig,
                error="",
            )

    def put_error(self, mcp_id: str, error: str, config_sig: str) -> None:
        """Record a failed refresh without evicting any prior success.

        The UI can still show the old tool list but annotate it as
        stale; the agent still knows what tools to call.
        """
        with self._lock:
            existing = self._entries.get(mcp_id)
            if existing is None:
                self._entries[mcp_id] = ToolManifestEntry(
                    mcp_id=mcp_id, config_sig=config_sig,
                    fetched_at=time.time(), error=error,
                )
            else:
                existing.error = error
                existing.fetched_at = time.time()

    def get(self, mcp_id: str) -> ToolManifestEntry | None:
        with self._lock:
            return self._entries.get(mcp_id)

    def invalidate(self, mcp_id: str) -> None:
        with self._lock:
            self._entries.pop(mcp_id, None)

    def all_ids(self) -> list[str]:
        with self._lock:
            return list(self._entries.keys())


# ---------------------------------------------------------------------------
# MCPManager — Central management system
# ---------------------------------------------------------------------------

class MCPManager:
    """Central management for MCP servers across nodes.

    MCP 分两个级别:
      - Global: 配置存储在 global_mcps 中，可同步到所有需要的 Node
      - Node:   配置存储在 node_configs[node_id] 中，每个 Node 独立管理
    """

    def __init__(self, data_dir: str | None = None):
        """Initialize MCPManager with optional data directory."""
        if data_dir is None:
            data_dir = str(Path.home() / ".tudou_claw")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.config_path = self.data_dir / "mcp_configs.json"
        self.node_configs: dict[str, NodeMCPConfig] = {}
        # Global-level MCPs: 配置一次，可同步到所有 Node
        self.global_mcps: dict[str, MCPServerConfig] = {}
        self._lock = threading.RLock()

        # Tool manifest cache: the single source of truth for "what
        # tools does MCP <X> expose". See ToolManifestCache docstring
        # for the invariant this implements.
        self.tool_manifests = ToolManifestCache()

        self._load()
        self._load_global_mcps()
        self._cleanup_builtin_mcps()
        logger.info(f"MCPManager initialized: {len(self.global_mcps)} global, "
                    f"{len(self.node_configs)} node configs")

        # Boot-time background preload of tool manifests for every
        # enabled stdio MCP. Runs off the main thread because probing
        # is I/O-bound (subprocess spawn + JSON-RPC round trip); hub
        # init should not block on it. Consumers that query the cache
        # before preload finishes simply see "no tools yet" and the
        # agent prompt will pick them up on the next regeneration.
        try:
            threading.Thread(
                target=self._preload_tool_manifests,
                name="mcp-tool-manifest-preload",
                daemon=True,
            ).start()
        except Exception:
            logger.exception("failed to start tool manifest preload thread")

    # ------------------------------------------------------------------
    # Tool manifest refresh / preload
    # ------------------------------------------------------------------

    def _all_stdio_configs(self) -> list["MCPServerConfig"]:
        """Return every enabled stdio MCPServerConfig known to the
        manager, de-duped by id. Used by the preload thread."""
        seen: dict[str, MCPServerConfig] = {}
        with self._lock:
            for cfg in self.global_mcps.values():
                if cfg.enabled and cfg.transport == "stdio":
                    seen[cfg.id] = cfg
            for ncfg in self.node_configs.values():
                for cfg in ncfg.available_mcps.values():
                    if cfg.enabled and cfg.transport == "stdio":
                        # Prefer node-level over global if both present
                        seen[cfg.id] = cfg
        return list(seen.values())

    def _find_config_by_id(self, mcp_id: str) -> "MCPServerConfig | None":
        with self._lock:
            if mcp_id in self.global_mcps:
                return self.global_mcps[mcp_id]
            for ncfg in self.node_configs.values():
                cfg = ncfg.available_mcps.get(mcp_id)
                if cfg is not None:
                    return cfg
        return None

    def refresh_tool_manifest(
        self,
        config: "MCPServerConfig",
        timeout: float = 10.0,
    ) -> ToolManifestEntry | None:
        """Probe one MCP and write its tool manifest into the cache.

        Fully synchronous — caller decides whether to run it on a
        background thread. Safe to call concurrently; the cache has
        its own lock.
        """
        sig = ToolManifestCache.compute_sig(config)
        try:
            # Lazy import to avoid a manager→router→manager cycle.
            from .router import MCPCallRouter
            router = MCPCallRouter(hub=None)
            cr = router.probe(config, timeout_s=timeout)
        except Exception as e:
            logger.warning("tool manifest probe crashed for %s: %s", config.id, e)
            self.tool_manifests.put_error(config.id, f"probe crashed: {e}", sig)
            return self.tool_manifests.get(config.id)

        if not cr.ok:
            self.tool_manifests.put_error(
                config.id,
                cr.error_message or cr.error_kind,
                sig,
            )
            return self.tool_manifests.get(config.id)

        content = cr.content if isinstance(cr.content, dict) else {}
        manifests = list(content.get("tool_manifests") or [])
        server_info = content.get("server_info") or {}
        self.tool_manifests.put(config.id, manifests, server_info, sig)
        return self.tool_manifests.get(config.id)

    def get_tool_manifest(self, mcp_id: str) -> ToolManifestEntry | None:
        """Read-only accessor. Returns None if the MCP has never been
        probed successfully. Callers must tolerate None and render a
        "not yet discovered" hint rather than crashing."""
        return self.tool_manifests.get(mcp_id)

    def invalidate_tool_manifest(self, mcp_id: str) -> None:
        """Drop cached tools for an MCP. Called from every CRUD
        operation that can change what tools an MCP exposes."""
        self.tool_manifests.invalidate(mcp_id)

    def _preload_tool_manifests(self) -> None:
        """Boot-time background job: probe every known stdio MCP."""
        try:
            configs = self._all_stdio_configs()
        except Exception:
            logger.exception("preload: failed to enumerate stdio configs")
            return
        if not configs:
            return
        logger.info("tool-manifest preload: probing %d MCPs", len(configs))
        for cfg in configs:
            try:
                self.refresh_tool_manifest(cfg, timeout=8.0)
            except Exception:
                logger.exception("preload: refresh crashed for %s", cfg.id)
        ok_count = sum(
            1 for mid in self.tool_manifests.all_ids()
            if (e := self.tool_manifests.get(mid)) and e.tools
        )
        logger.info("tool-manifest preload: %d/%d MCPs have tools",
                    ok_count, len(configs))

    def _get_db(self):
        try:
            from .database import get_database
            return get_database()
        except Exception:
            return None

    def _load(self) -> None:
        """从 SQLite (primary) 或 JSON (fallback) 加载配置。

        SQLite rows are shaped ``(node_id, mcp_id, data)`` where ``data`` is
        the JSON-serialized :class:`MCPServerConfig`. :meth:`Database._row_to_dict`
        already merges that JSON into the row dict, but it also leaves the
        SQLite integer primary key in ``row["id"]``, which would shadow the
        real string id of the MCPServerConfig. We restore the real id from
        ``mcp_id`` before constructing the config, and we skip rows whose
        ``mcp_id`` matches a historical garbage pattern (created by an older
        migration that iterated NodeMCPConfig's top-level fields as if they
        were mcp ids). See database.py migrate_legacy_json for context.
        """
        # Names that were historically written as fake "mcp ids" by the
        # buggy migration. Any row with one of these ids is corrupt and must
        # be ignored on load (and actively deleted at save time).
        _GARBAGE_MCP_IDS = {
            "node_id", "available_mcps", "agent_bindings",
            "env_overrides", "agent_env_overrides",
            "created_at", "updated_at",
        }
        db = self._get_db()
        if db:
            try:
                rows = db.load_mcp_configs()
            except Exception as e:
                rows = None
                logger.warning(f"SQLite MCP load failed, trying JSON: {e}")
            # Filter out the garbage rows and any row whose payload is not
            # a well-formed MCPServerConfig dict. Only if we actually see
            # at least one real row do we trust SQLite; otherwise fall
            # through to the JSON file so a cleanly-wiped DB doesn't mask
            # an existing JSON config.
            if rows:
                valid_rows: list[dict] = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    mid = r.get("mcp_id", "")
                    if not mid or mid in _GARBAGE_MCP_IDS:
                        continue
                    # An MCPServerConfig must have at least one of these
                    # fields. Rows that are pure column-data (no unpacked
                    # JSON) indicate a corrupt write.
                    if not any(k in r for k in ("name", "transport", "command", "url", "env")):
                        continue
                    valid_rows.append(r)
                if valid_rows:
                    try:
                        by_node: dict[str, dict] = {}
                        for r in valid_rows:
                            nid = r.get("node_id", "") or ""
                            mid = r.get("mcp_id", "")
                            # Restore the real id (string) from mcp_id,
                            # shadowing the SQLite integer primary key.
                            cfg_dict = dict(r)
                            cfg_dict["id"] = mid
                            # Drop schema-only noise so from_dict doesn't
                            # pick them up by accident.
                            cfg_dict.pop("node_id", None)
                            cfg_dict.pop("mcp_id", None)
                            cfg_dict.pop("data", None)
                            by_node.setdefault(nid, {})[mid] = cfg_dict
                        for node_id, mcps in by_node.items():
                            self.node_configs[node_id] = NodeMCPConfig.from_dict(
                                {"node_id": node_id, "available_mcps": mcps})
                        logger.info(
                            f"Loaded {len(self.node_configs)} node MCP configs "
                            f"from SQLite ({len(valid_rows)} valid rows, "
                            f"{len(rows) - len(valid_rows)} garbage skipped)"
                        )
                        return
                    except Exception as e:
                        logger.warning(f"SQLite MCP row rebuild failed, trying JSON: {e}")
        if not self.config_path.exists():
            logger.debug(f"Config file {self.config_path} does not exist, starting fresh")
            return
        try:
            with open(self.config_path, 'r') as f:
                data = json.load(f)
            for node_id, node_data in data.get("node_configs", {}).items():
                self.node_configs[node_id] = NodeMCPConfig.from_dict(node_data)
            logger.info(f"Loaded {len(self.node_configs)} node MCP configs from {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to load MCP configs: {e}")

        # ── One-shot migration: encrypt any sensitive plaintext env ──
        # Older configs (pre-2026-04-29 encryption support) keep
        # SMTP_PASSWORD / API_KEY / etc. in cleartext. On every load we
        # walk the in-memory MCPs once and re-save if anything was
        # plaintext-sensitive. Idempotent — second run is a no-op.
        try:
            from .secrets import migrate_plaintext_env
            total_migrated = 0
            for _node_id, _ncfg in self.node_configs.items():
                for _mid, _mcfg in (_ncfg.available_mcps or {}).items():
                    if not isinstance(_mcfg.env, dict):
                        continue
                    new_env, n = migrate_plaintext_env(_mcfg.env)
                    if n > 0:
                        _mcfg.env = new_env
                        total_migrated += n
            if total_migrated > 0:
                logger.info(
                    "MCP secrets: encrypted %d previously-plaintext env "
                    "value(s) on load; rewriting config to disk",
                    total_migrated,
                )
                self._save()
        except Exception as _me:
            logger.warning("MCP secret migration on load failed: %s", _me)

    def _save(self) -> None:
        """保存配置到 SQLite + JSON backup。"""
        db = self._get_db()
        if db:
            try:
                for node_id, cfg in self.node_configs.items():
                    cfg_dict = cfg.to_dict()
                    for mid, mdata in cfg_dict.get("available_mcps", {}).items():
                        db.save_mcp_config(node_id, mid, mdata)
            except Exception as e:
                logger.warning(f"SQLite MCP save failed: {e}")
        try:
            data = {
                "node_configs": {
                    node_id: cfg.to_dict()
                    for node_id, cfg in self.node_configs.items()
                }
            }
            with open(self.config_path, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Saved MCP configs to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to save MCP configs: {e}")

    # ------------------------------------------------------------------
    # Global MCP 存储
    # ------------------------------------------------------------------

    def _global_mcps_path(self) -> Path:
        return self.data_dir / "global_mcps.json"

    def _load_global_mcps(self):
        """加载 Global MCP 配置。

        Mirrors the hardening in :meth:`_load`: the SQLite integer PK in
        ``row["id"]`` shadows the real MCPServerConfig string id, so we
        restore it from ``mcp_id`` before constructing the config, and we
        skip rows that look like garbage (no payload fields). Only if we
        find at least one valid row do we trust SQLite; otherwise we fall
        through to the JSON file.
        """
        _GARBAGE_MCP_IDS = {
            "node_id", "available_mcps", "agent_bindings",
            "env_overrides", "agent_env_overrides",
            "created_at", "updated_at",
        }
        db = self._get_db()
        loaded_from_sqlite = False
        if db:
            try:
                rows = db.load_mcp_configs("__global__")
            except Exception as e:
                rows = []
                logger.warning(f"SQLite global MCP load failed: {e}")
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                mid = r.get("mcp_id", "")
                if not mid or mid in _GARBAGE_MCP_IDS:
                    continue
                if not any(k in r for k in ("name", "transport", "command", "url", "env")):
                    continue
                cfg_dict = dict(r)
                cfg_dict["id"] = mid
                cfg_dict.pop("node_id", None)
                cfg_dict.pop("mcp_id", None)
                cfg_dict.pop("data", None)
                try:
                    self.global_mcps[mid] = MCPServerConfig.from_dict(cfg_dict)
                    loaded_from_sqlite = True
                except Exception as e:
                    logger.warning(f"Skipping malformed global MCP row {mid}: {e}")
            if loaded_from_sqlite:
                logger.info(
                    f"Loaded {len(self.global_mcps)} global MCPs from SQLite")
                return
        # JSON fallback (also used when SQLite was empty or all garbage)
        gpath = self._global_mcps_path()
        if not gpath.exists():
            return
        try:
            with open(gpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            for mid, mdata in data.get("global_mcps", {}).items():
                self.global_mcps[mid] = MCPServerConfig.from_dict(mdata)
            logger.info(f"Loaded {len(self.global_mcps)} global MCPs from JSON")
        except Exception as e:
            logger.warning(f"Failed to load global MCPs: {e}")

    def _save_global_mcps(self):
        """保存 Global MCP 配置到 SQLite + JSON。"""
        db = self._get_db()
        if db:
            try:
                for mid, cfg in self.global_mcps.items():
                    db.save_mcp_config("__global__", mid, cfg.to_dict())
            except Exception:
                pass
        try:
            data = {"global_mcps": {mid: cfg.to_dict()
                                     for mid, cfg in self.global_mcps.items()}}
            with open(self._global_mcps_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save global MCPs: {e}")

    # ------------------------------------------------------------------
    # Global MCP CRUD
    # ------------------------------------------------------------------

    def add_global_mcp(self, config: MCPServerConfig) -> MCPServerConfig:
        """Add a Global MCP and eagerly propagate it to every node.

        Invariant (D.1) — Global visibility:
            A Global MCP is *physically* present in every node's
            available_mcps table at storage time, not lazily merged at
            resolution time. The node's view of its own MCPs is always
            complete: `node_cfg.available_mcps` is the single source
            every enumeration path can trust.
        """
        with self._lock:
            config.scope = "global"
            self.global_mcps[config.id] = config
            self._save_global_mcps()
            logger.info(f"Added global MCP: {config.id} ({config.name})")
        # Any existing manifest is for a previous config; force re-probe.
        self.invalidate_tool_manifest(config.id)
        # Eager propagation: copy into every known node. Each copy keeps
        # scope="global" so UI can render it with a Global badge and
        # refuse per-node edits to the base config.
        try:
            self.sync_global_to_all_nodes([config.id])
        except Exception as e:
            logger.warning(f"Global MCP propagation to nodes failed: {e}")
        # Background probe so ToolManifestCache warms up without
        # blocking the caller.
        try:
            threading.Thread(
                target=self.refresh_tool_manifest,
                args=(config,),
                daemon=True,
            ).start()
        except Exception:
            pass
        return config

    def remove_global_mcp(self, mcp_id: str) -> bool:
        """Remove a Global MCP AND its eager copies from every node.

        Symmetric to :meth:`add_global_mcp`: the global source is
        removed from ``global_mcps`` and the eager copies in each
        ``node_cfg.available_mcps`` that were tagged scope='global' are
        also removed. Node-local MCPs with the same id (if any) are
        left alone — they were never part of the global stream.
        """
        with self._lock:
            if mcp_id not in self.global_mcps:
                return False
            del self.global_mcps[mcp_id]
            self._save_global_mcps()
            # Remove the eager copies from every node.
            for node_id, node_cfg in self.node_configs.items():
                existing = node_cfg.available_mcps.get(mcp_id)
                if existing is not None and getattr(existing, "scope", "") == "global":
                    node_cfg.remove_mcp(mcp_id)
                    logger.info(f"Removed global MCP copy from node {node_id}: {mcp_id}")
            self._save()
            logger.info(f"Removed global MCP: {mcp_id}")
        self.invalidate_tool_manifest(mcp_id)
        return True

    # ------------------------------------------------------------------
    # Scope mutation — admin can re-scope an MCP between Node / Global
    # ------------------------------------------------------------------

    def change_mcp_scope(
        self,
        mcp_id: str,
        new_scope: str,
        target_nodes: list[str] | None = None,
    ) -> dict:
        """Move an MCP between scopes. Admin-only.

        Supported transitions:

          * ``"global"``      — promote the MCP to Global. It will be
            eagerly propagated to every known node. ``target_nodes`` is
            ignored. If the MCP currently lives on a specific node, its
            config (including env) is taken as the source of truth.

          * ``"node"``        — demote a Global MCP to node-local.
            ``target_nodes`` must contain exactly one node id. The
            MCP becomes local to that node; eager copies on other
            nodes are removed.

          * ``"multi_node"``  — the MCP is made available on an
            explicit whitelist of nodes. ``target_nodes`` must be a
            non-empty list of node ids. The source is removed from
            ``global_mcps`` (if present) and nodes outside the list
            have their copies removed.

        Returns: ``{"ok": bool, "scope": str, "nodes": [...], "error": str}``
        """
        if new_scope not in ("global", "node", "multi_node"):
            return {"ok": False, "error": f"unknown scope: {new_scope}"}
        target_nodes = list(target_nodes or [])
        with self._lock:
            # Find the source config: prefer global, fall back to first
            # node that owns it.
            source: MCPServerConfig | None = self.global_mcps.get(mcp_id)
            source_node: str | None = None
            if source is None:
                for nid, ncfg in self.node_configs.items():
                    if mcp_id in ncfg.available_mcps:
                        source = ncfg.available_mcps[mcp_id]
                        source_node = nid
                        break
            if source is None:
                return {"ok": False, "error": f"mcp {mcp_id} not found in any scope"}

            # Snapshot the base config so we can re-stamp it with a new
            # scope without accidentally aliasing.
            def _clone(cfg: MCPServerConfig, scope: str) -> MCPServerConfig:
                return MCPServerConfig(
                    id=cfg.id,
                    name=cfg.name,
                    transport=cfg.transport,
                    command=cfg.command,
                    url=cfg.url,
                    env=dict(cfg.env),
                    enabled=cfg.enabled,
                    scope=scope,
                    install_status=getattr(cfg, "install_status", ""),
                    install_command=getattr(cfg, "install_command", ""),
                )

            affected_nodes: list[str] = []

            if new_scope == "global":
                self.global_mcps[mcp_id] = _clone(source, "global")
                self._save_global_mcps()
                # Propagate to every known node (eager).
                sync_res = self.sync_global_to_all_nodes([mcp_id])
                affected_nodes = list(sync_res.keys())
            elif new_scope == "node":
                if len(target_nodes) != 1:
                    return {
                        "ok": False,
                        "error": "scope=node requires exactly one target_node",
                    }
                target = target_nodes[0]
                # Remove from global first.
                self.global_mcps.pop(mcp_id, None)
                self._save_global_mcps()
                # Remove from any other node.
                for nid, ncfg in self.node_configs.items():
                    if nid == target:
                        continue
                    if mcp_id in ncfg.available_mcps:
                        ncfg.remove_mcp(mcp_id)
                # Install on the target node.
                tgt_cfg = self.get_node_mcp_config(target)
                tgt_cfg.add_mcp(_clone(source, "node"))
                self._save()
                affected_nodes = [target]
            else:  # multi_node
                if not target_nodes:
                    return {
                        "ok": False,
                        "error": "scope=multi_node requires a non-empty target_nodes list",
                    }
                # Remove from global.
                self.global_mcps.pop(mcp_id, None)
                self._save_global_mcps()
                allowed = set(target_nodes)
                # Remove from nodes not in the allow-list.
                for nid, ncfg in self.node_configs.items():
                    if nid in allowed:
                        continue
                    if mcp_id in ncfg.available_mcps:
                        ncfg.remove_mcp(mcp_id)
                # Install on each allowed node.
                for nid in target_nodes:
                    tgt_cfg = self.get_node_mcp_config(nid)
                    tgt_cfg.add_mcp(_clone(source, "multi_node"))
                self._save()
                affected_nodes = list(target_nodes)

        self.invalidate_tool_manifest(mcp_id)
        return {
            "ok": True,
            "scope": new_scope,
            "nodes": affected_nodes,
            "source_node": source_node,
        }

    def list_global_mcps(self) -> dict[str, MCPServerConfig]:
        """列出所有 Global MCP。"""
        return dict(self.global_mcps)

    # ------------------------------------------------------------------
    # Global → Node 同步
    # ------------------------------------------------------------------

    def sync_global_to_node(self, node_id: str,
                            mcp_ids: list[str] | None = None) -> dict:
        """
        将 Global MCP 同步到指定 Node。

        如果 mcp_ids 为 None，同步所有 global_mcps。
        Node 上已存在同 ID 的 MCP 会被更新。

        对于远程 Node，会通过 Hub 推送配置。
        对于本地 Node，需在本地执行 install_command。

        Returns: {synced: [mcp_id, ...], skipped: [...], errors: [...]}
        """
        result = {"synced": [], "skipped": [], "errors": []}
        ids_to_sync = mcp_ids or list(self.global_mcps.keys())

        with self._lock:
            node_cfg = self.get_node_mcp_config(node_id)

            for mid in ids_to_sync:
                if mid not in self.global_mcps:
                    result["skipped"].append(mid)
                    continue

                global_mcp = self.global_mcps[mid]
                # 克隆配置，保留 scope=global 标记
                synced_config = MCPServerConfig(
                    id=global_mcp.id,
                    name=global_mcp.name,
                    transport=global_mcp.transport,
                    command=global_mcp.command,
                    url=global_mcp.url,
                    env=dict(global_mcp.env),
                    enabled=global_mcp.enabled,
                    scope="global",
                    install_status=global_mcp.install_status,
                    install_command=global_mcp.install_command,
                )

                # 如果 Node 上已有，保留 Node 的 env_overrides
                existing = node_cfg.available_mcps.get(mid)
                if existing:
                    # 保留 node 侧的安装状态
                    synced_config.install_status = existing.install_status
                    synced_config.installed_at = existing.installed_at

                node_cfg.add_mcp(synced_config)
                result["synced"].append(mid)

            self._save()

        # 对远程 Node 推送
        for mid in result["synced"]:
            try:
                self.deploy_mcp_to_node(node_id, mid)
            except Exception as e:
                result["errors"].append(f"{mid}: {e}")

        logger.info(f"Synced {len(result['synced'])} global MCPs to node {node_id}")
        return result

    def sync_global_to_all_nodes(self, mcp_ids: list[str] | None = None) -> dict:
        """将 Global MCP 同步到所有已注册的 Node。"""
        results = {}
        for node_id in list(self.node_configs.keys()):
            results[node_id] = self.sync_global_to_node(node_id, mcp_ids)
        return results

    def get_node_global_mcps(self, node_id: str) -> list[MCPServerConfig]:
        """获取 Node 上来自 Global 的 MCP 列表。"""
        node_cfg = self.node_configs.get(node_id)
        if not node_cfg:
            return []
        return [m for m in node_cfg.available_mcps.values()
                if m.scope == "global"]

    def get_node_local_mcps(self, node_id: str) -> list[MCPServerConfig]:
        """获取 Node 上的本地 MCP 列表（scope=node）。"""
        node_cfg = self.node_configs.get(node_id)
        if not node_cfg:
            return []
        return [m for m in node_cfg.available_mcps.values()
                if m.scope != "global"]

    def _cleanup_builtin_mcps(self) -> None:
        """Remove legacy __builtin_tts__/__builtin_stt__ entries (TTS/STT is browser-side, not MCP)."""
        changed = False
        for node_cfg in self.node_configs.values():
            for bid in ["__builtin_tts__", "__builtin_stt__"]:
                if bid in node_cfg.available_mcps:
                    node_cfg.remove_mcp(bid)
                    changed = True
                    logger.info(f"Removed legacy builtin MCP: {bid}")
        if changed:
            self._save()

    def get_node_mcp_config(self, node_id: str) -> NodeMCPConfig:
        """Get or create MCP config for a node.

        When a brand-new node is registered, every existing Global MCP
        is eagerly copied into its ``available_mcps``. This keeps the
        invariant "every node physically owns its global MCPs" true
        even for nodes that register after the global was added.
        """
        with self._lock:
            if node_id not in self.node_configs:
                self.node_configs[node_id] = NodeMCPConfig(node_id=node_id)
                newly_created = True
                self._save()
                logger.info(f"Created new MCP config for node {node_id}")
            else:
                newly_created = False
            cfg = self.node_configs[node_id]
        # Pull globals outside the lock to avoid sync_global_to_node
        # reacquiring it (it uses ``with self._lock`` internally).
        if newly_created and self.global_mcps:
            try:
                self.sync_global_to_node(node_id)
            except Exception as e:
                logger.warning(
                    f"Global MCP propagation to new node {node_id} failed: {e}"
                )
        return cfg

    def add_mcp_to_node(self, node_id: str, config: MCPServerConfig) -> MCPServerConfig:
        """向节点添加 / 覆盖 MCP。(Add or update MCP on a node.)

        Sensitive env values that come back as the mask placeholder
        (the admin form didn't touch them) are merged in from the
        existing config, so an edit-and-save round-trip doesn't
        accidentally null out passwords / API keys the admin never saw
        in plaintext.
        """
        with self._lock:
            node_cfg = self.get_node_mcp_config(node_id)
            # Merge unchanged-mask fields from existing entry (if any).
            try:
                from .secrets import merge_unchanged_from_existing
                existing = node_cfg.available_mcps.get(config.id)
                if existing is not None and isinstance(getattr(existing, "env", None), dict):
                    config.env = merge_unchanged_from_existing(
                        config.env, existing.env,
                    )
            except Exception:
                pass
            node_cfg.add_mcp(config)
            self._save()
        # Config changed → any cached manifest is potentially stale.
        self.invalidate_tool_manifest(config.id)
        # Re-probe on a background thread so the next MCP.md render
        # already has fresh tools for this MCP.
        try:
            threading.Thread(
                target=self.refresh_tool_manifest,
                args=(config,),
                daemon=True,
            ).start()
        except Exception:
            pass
        return config

    def remove_mcp_from_node(self, node_id: str, mcp_id: str) -> bool:
        """从节点移除MCP。(Remove MCP from a node.)"""
        with self._lock:
            if node_id not in self.node_configs:
                return False
            result = self.node_configs[node_id].remove_mcp(mcp_id)
            if result:
                self._save()
        if result:
            self.invalidate_tool_manifest(mcp_id)
        return result

    def bind_mcp_to_agent(self, node_id: str, agent_id: str, mcp_id: str) -> bool:
        """绑定MCP到代理。(Bind MCP to an agent on a node.)"""
        with self._lock:
            node_cfg = self.get_node_mcp_config(node_id)
            result = node_cfg.bind_agent(agent_id, mcp_id)
            if result:
                self._save()
            return result

    def unbind_mcp_from_agent(self, node_id: str, agent_id: str, mcp_id: str) -> bool:
        """从代理解绑MCP。(Unbind MCP from an agent on a node.)"""
        with self._lock:
            if node_id not in self.node_configs:
                return False
            result = self.node_configs[node_id].unbind_agent(agent_id, mcp_id)
            if result:
                self._save()
            return result

    def set_agent_mcp_env(self, node_id: str, agent_id: str, mcp_id: str,
                          env: dict[str, str]) -> bool:
        """设置Agent专属MCP环境变量覆盖。
        (Set agent-specific MCP env overrides. Allows same MCP, different config per agent.)"""
        with self._lock:
            node_cfg = self.get_node_mcp_config(node_id)
            if mcp_id not in node_cfg.available_mcps:
                logger.error(f"MCP {mcp_id} not available on node {node_id}")
                return False
            for k, v in env.items():
                node_cfg.set_agent_env_override(agent_id, mcp_id, k, v)
            self._save()
            logger.info(f"Set agent env overrides for {agent_id}/{mcp_id}: {list(env.keys())}")
        return True

    def get_agent_mcp_env(self, node_id: str, agent_id: str, mcp_id: str) -> dict[str, str]:
        """获取Agent专属MCP环境变量覆盖。(Get agent-specific MCP env overrides.)"""
        with self._lock:
            node_cfg = self.get_node_mcp_config(node_id)
            return node_cfg.get_agent_env_overrides(agent_id, mcp_id)

    def get_agent_effective_mcps(self, node_id: str, agent_id: str) -> list[MCPServerConfig]:
        """Return the agent's effective MCP set with env overrides applied.

        Architectural invariant (D) — MCP scope hierarchy:

            Effective set = (Global MCPs) ∪ (Node-level MCPs for `node_id`),
                            filtered by the per-agent binding table.

        Global MCPs are defined once on the manager and are visible to
        every node. Node-level MCPs are local to one node only. When the
        same id exists in both, Global takes precedence (per explicit
        user spec: "Global > Node").

        The router / dispatcher MUST consult this method and nothing
        else when asking "what can this agent call". Reading
        `agent.profile.mcp_servers` directly is a legacy path and is
        subject to staleness — do not use it as the source of truth.

        Env override priority (highest wins):
          1. agent_env_overrides[agent_id][mcp_id]  — per-agent per-MCP
          2. env_overrides[mcp_id]                  — per-node per-MCP
          3. mcp.env                                — base MCP config
        """
        with self._lock:
            node_cfg = self.get_node_mcp_config(node_id)
            # Support wildcard "*" binding: MCPs bound to "*" apply to ALL agents.
            # Agent-specific bindings are merged on top of wildcard bindings.
            wildcard_ids = list(node_cfg.agent_bindings.get("*", []))
            agent_specific_ids = list(node_cfg.agent_bindings.get(agent_id, []))
            # Merge: wildcard first, then agent-specific (dedup, preserve order)
            seen = set()
            bound_ids = []
            for mid in wildcard_ids + agent_specific_ids:
                if mid not in seen:
                    seen.add(mid)
                    bound_ids.append(mid)

            # Resolve each bound id from the merged pool. Global wins
            # on collision: if a node-level MCP and a global MCP share
            # the same id, we use the global one. This matches the
            # user's stated hierarchy (Global > Node).
            def _resolve(mid: str) -> MCPServerConfig | None:
                if mid in self.global_mcps:
                    return self.global_mcps[mid]
                return node_cfg.available_mcps.get(mid)

            result: list[MCPServerConfig] = []
            for mid in bound_ids:
                base = _resolve(mid)
                if base is None:
                    # Binding references an MCP that no longer exists in
                    # either scope. Silently skip — removing stale binds
                    # is a separate concern.
                    continue
                mcp_copy = MCPServerConfig(
                    id=base.id,
                    name=base.name,
                    transport=base.transport,
                    command=base.command,
                    url=base.url,
                    env=dict(base.env),
                    enabled=base.enabled,
                )
                # Layer 1: node-level overrides (even for global MCPs —
                # a node can still tweak VOLC_REGION for one instance
                # without touching the global base config).
                mcp_copy.env.update(node_cfg.get_env_overrides(mid))
                # Layer 2: agent-specific overrides — highest priority.
                mcp_copy.env.update(node_cfg.get_agent_env_overrides(agent_id, mid))
                result.append(mcp_copy)
            return result

    def sync_agent_mcps(self, agent: Any) -> None:
        """将MCP配置同步到代理。(Sync MCPServerConfigs into agent.profile.mcp_servers.)"""
        if not hasattr(agent, 'profile') or not hasattr(agent.profile, 'mcp_servers'):
            logger.warning(f"Agent {agent.id} does not have profile.mcp_servers")
            return

        # Assume agent has node_id and id attributes; default to 'local'
        node_id = getattr(agent, 'node_id', None) or 'local'
        agent_id = agent.id

        with self._lock:
            mcps = self.get_agent_effective_mcps(node_id, agent_id)
            agent.profile.mcp_servers = mcps
            logger.info(f"Synced {len(mcps)} MCPs to agent {agent_id}")

    def list_all_node_mcps(self) -> dict[str, Any]:
        """获取所有节点的MCP概览。(Get overview of MCPs across all nodes.)"""
        with self._lock:
            overview = {}
            for node_id, node_cfg in self.node_configs.items():
                overview[node_id] = {
                    "mcp_count": len(node_cfg.available_mcps),
                    "agent_bindings": len(node_cfg.agent_bindings),
                    "mcps": [
                        {
                            "id": mid,
                            "name": cfg.name,
                            "enabled": cfg.enabled,
                        }
                        for mid, cfg in node_cfg.available_mcps.items()
                    ]
                }
            return overview

    def deploy_mcp_to_node(self, node_id: str, mcp_id: str) -> bool:
        """部署MCP到远程节点。(Push MCP config to remote node via Hub.)

        For the local node this is a no-op: the config is already in
        ``self.node_configs[node_id].available_mcps``, and the hub-based
        HTTP push is only meaningful for *remote* nodes. We short-circuit
        before importing the hub so local deploys never depend on hub
        wiring being available.
        """
        with self._lock:
            if node_id not in self.node_configs:
                return False
            node_cfg = self.node_configs[node_id]
            if mcp_id not in node_cfg.available_mcps:
                return False
            mcp = node_cfg.available_mcps[mcp_id]

        # Local node: config is already authoritative in-process, nothing to push.
        if node_id == "local":
            return True

        # 通过 Hub 推送 MCP 配置到远程节点
        # NOTE: Hub module lives at app.hub (not app.mcp.hub), so use `..hub`.
        try:
            from ..hub import get_hub
            hub = get_hub()
            # If this process IS the node being deployed to, it's local — no push.
            if getattr(hub, "node_id", None) == node_id:
                return True
            node = hub.remote_nodes.get(node_id)
            if not node or not node.url:
                logger.warning("Node %s not found or no URL, skip MCP deploy", node_id)
                return False

            import requests as http_req
            headers = {"Content-Type": "application/json"}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            resp = http_req.post(
                f"{node.url}/api/hub/apply-mcp",
                headers=headers,
                json={"mcp_id": mcp_id, "config": mcp.to_dict()},
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info("MCP %s deployed to node %s", mcp_id, node_id)
                return True
            else:
                logger.error("MCP deploy failed: %s %s", resp.status_code, resp.text[:200])
                return False
        except Exception as e:
            logger.error("MCP deploy to %s failed: %s", node_id, e)
            return False

    def resolve_mcp_for_role(self, role: str) -> list[MCPCapability]:
        """获取推荐的MCP。(Get recommended MCPs for a role.)"""
        recommendations = []
        for capability in MCP_CATALOG.values():
            if "all" in capability.compatible_roles or role in capability.compatible_roles:
                recommendations.append(capability)
        return recommendations

    def generate_mcp_config(self, capability_id: str, env_values: dict[str, str] | None = None) -> MCPServerConfig | None:
        """从能力生成MCP配置。(Generate MCPServerConfig from capability with env values.)"""
        if capability_id not in MCP_CATALOG:
            logger.error(f"Unknown MCP capability: {capability_id}")
            return None

        capability = MCP_CATALOG[capability_id]

        # Validate required env vars
        if env_values is None:
            env_values = {}

        for required in capability.required_env:
            if required not in env_values:
                logger.warning(f"Missing required env var {required} for {capability_id}")

        # Build command/url from templates
        command = capability.command_template
        url = capability.url_template

        # Basic template expansion
        for key, value in env_values.items():
            command = command.replace(f"{{{key}}}", value)
            url = url.replace(f"{{{key}}}", value)

        config = MCPServerConfig(
            name=capability.name,
            transport=capability.transport,
            command=command,
            url=url,
            env=env_values.copy(),
            enabled=True,
            scope=capability.scope,
        )
        return config

    def validate_mcp_config(self, config: MCPServerConfig) -> tuple[bool, str]:
        """验证MCP配置。(Check if an MCP config is valid.)"""
        if not config.name:
            return False, "MCP name is required"

        if config.transport not in ("stdio", "sse", "streamable-http"):
            return False, f"Invalid transport: {config.transport}"

        if config.transport == "stdio" and not config.command:
            return False, "Command is required for stdio transport"

        if config.transport in ("sse", "streamable-http") and not config.url:
            return False, "URL is required for {config.transport} transport"

        return True, "Valid"

    # ------------------------------------------------------------------
    # MCP 一键安装 — Install MCP packages to node
    # ------------------------------------------------------------------

    # 安装任务跟踪: {task_id: {status, output, ...}}
    _install_tasks: dict[str, dict] = {}

    @staticmethod
    def check_prerequisites() -> dict[str, bool]:
        """检查安装前置工具是否可用。"""
        import shutil
        return {
            "npm": shutil.which("npm") is not None,
            "npx": shutil.which("npx") is not None,
            "node": shutil.which("node") is not None,
            "uv": shutil.which("uv") is not None,
            "uvx": shutil.which("uvx") is not None,
            "pip": shutil.which("pip") is not None or shutil.which("pip3") is not None,
            "python": shutil.which("python3") is not None or shutil.which("python") is not None,
        }

    def _detect_installer(self, install_cmd: str) -> str:
        """从安装命令中检测需要的包管理器。"""
        cmd_lower = install_cmd.lower().strip()
        if cmd_lower.startswith("npm "):
            return "npm"
        elif cmd_lower.startswith("uv "):
            return "uv"
        elif cmd_lower.startswith("pip"):
            return "pip"
        elif cmd_lower.startswith("npx "):
            return "npx"
        return "unknown"

    def install_mcp(self, node_id: str, capability_id: str,
                    env_values: dict[str, str] | None = None) -> dict:
        """
        一键安装 MCP 到节点。

        根据 scope 区分:
          - scope=global: 先存入 global_mcps，然后同步到指定 node，
                          远程 node 通过 Hub 推送配置。
          - scope=node:   直接在本地 node 执行 install_command。

        Returns: {ok, task_id, status, message, config?, output?, scope?}
        """
        import subprocess as _sp
        import shlex as _shlex

        # 1. 验证 capability
        if capability_id not in MCP_CATALOG:
            return {"ok": False, "status": "error",
                    "message": f"未知的 MCP: {capability_id}"}

        cap = MCP_CATALOG[capability_id]
        install_cmd = cap.install_command.strip()

        if not install_cmd:
            # 没有安装命令，直接生成配置
            config = self.generate_mcp_config(capability_id, env_values)
            if not config:
                return {"ok": False, "status": "error",
                        "message": "配置生成失败"}
            config.install_status = "installed"
            config.install_command = ""
            # Global MCP → 存入 global_mcps + 同步到 node
            if cap.scope == "global":
                self.add_global_mcp(config)
                self.sync_global_to_node(node_id, [config.id])
                return {"ok": True, "status": "installed", "scope": "global",
                        "message": f"{cap.name} 已添加为全局 MCP（无需安装）",
                        "config": config.to_dict()}
            else:
                self.add_mcp_to_node(node_id, config)
                return {"ok": True, "status": "installed", "scope": "node",
                        "message": f"{cap.name} 已添加（无需安装）",
                        "config": config.to_dict()}

        # 2. 检查前置工具
        installer = self._detect_installer(install_cmd)
        prereqs = self.check_prerequisites()

        if installer == "npm" and not prereqs.get("npm"):
            return {"ok": False, "status": "missing_tool",
                    "message": "需要安装 Node.js 和 npm。请运行: brew install node (Mac) 或 apt install nodejs npm (Linux)"}
        if installer == "uv" and not prereqs.get("uv"):
            return {"ok": False, "status": "missing_tool",
                    "message": "需要安装 uv。请运行: pip install uv"}

        # 3. 生成配置（先创建，标记为 installing）
        config = self.generate_mcp_config(capability_id, env_values)
        if not config:
            return {"ok": False, "status": "error",
                    "message": "配置生成失败"}
        config.install_status = "installing"
        config.install_command = install_cmd

        # Global MCP → 先存入 global_mcps 注册表
        is_global = cap.scope == "global"
        if is_global:
            self.add_global_mcp(config)
            self.sync_global_to_node(node_id, [config.id])
        else:
            self.add_mcp_to_node(node_id, config)

        # 4. 创建安装任务
        task_id = f"install_{capability_id}_{int(time.time())}"
        self._install_tasks[task_id] = {
            "status": "running",
            "capability_id": capability_id,
            "node_id": node_id,
            "mcp_id": config.id,
            "command": install_cmd,
            "scope": cap.scope,
            "output": "",
            "started_at": time.time(),
        }

        # 5. 启动后台安装线程
        def _run_install():
            result = self._execute_install(install_cmd, timeout=120)
            task = self._install_tasks[task_id]
            task["output"] = result.get("output", "")
            task["ended_at"] = time.time()

            if result["ok"]:
                task["status"] = "completed"
                # 更新 MCP 状态
                with self._lock:
                    # 更新 global 注册表
                    if is_global and config.id in self.global_mcps:
                        self.global_mcps[config.id].install_status = "installed"
                        self.global_mcps[config.id].installed_at = time.time()
                        self.global_mcps[config.id].install_error = ""
                        self._save_global_mcps()
                    # 更新 node 上的副本
                    nc = self.node_configs.get(node_id)
                    if nc and config.id in nc.available_mcps:
                        nc.available_mcps[config.id].install_status = "installed"
                        nc.available_mcps[config.id].installed_at = time.time()
                        nc.available_mcps[config.id].install_error = ""
                        self._save()
                logger.info("MCP %s installed successfully on node %s (scope=%s)",
                            capability_id, node_id, cap.scope)
            else:
                task["status"] = "failed"
                task["error"] = result.get("error", "Unknown error")
                with self._lock:
                    if is_global and config.id in self.global_mcps:
                        self.global_mcps[config.id].install_status = "failed"
                        self.global_mcps[config.id].install_error = result.get("error", "")[:500]
                        self._save_global_mcps()
                    nc = self.node_configs.get(node_id)
                    if nc and config.id in nc.available_mcps:
                        nc.available_mcps[config.id].install_status = "failed"
                        nc.available_mcps[config.id].install_error = result.get("error", "")[:500]
                        self._save()
                logger.error("MCP %s install failed (scope=%s): %s",
                             capability_id, cap.scope, result.get("error", ""))

        import threading as _th
        t = _th.Thread(target=_run_install, daemon=True,
                       name=f"mcp-install-{capability_id}")
        t.start()

        return {
            "ok": True,
            "status": "installing",
            "task_id": task_id,
            "scope": cap.scope,
            "message": f"正在安装 {cap.name} ({'全局' if is_global else '本地'})...",
            "config": config.to_dict(),
        }

    def _execute_install(self, install_cmd: str, timeout: float = 120) -> dict:
        """执行安装命令，返回 {ok, output, error}。"""
        import subprocess as _sp
        import shlex as _shlex

        logger.info("Executing install command: %s", install_cmd)
        try:
            # 处理可能带注释的命令（如 "uv tool install x  # or: pipx ..."）
            cmd = install_cmd.split("#")[0].strip()
            if not cmd:
                return {"ok": False, "error": "安装命令为空"}

            result = _sp.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ},
            )

            output = result.stdout + result.stderr
            if result.returncode == 0:
                return {"ok": True, "output": output}
            else:
                # npm install -g 如果包已存在会返回 0，但 uv 有时候返回非 0
                # 检查是否是 "already installed" 类似信息
                combined = output.lower()
                if ("already" in combined and "install" in combined) or \
                   ("up to date" in combined) or \
                   ("nothing to do" in combined):
                    return {"ok": True, "output": output}
                return {
                    "ok": False,
                    "output": output,
                    "error": f"Exit code {result.returncode}: {result.stderr[:300]}",
                }
        except _sp.TimeoutExpired:
            return {"ok": False, "error": f"安装超时 ({timeout}s)"}
        except FileNotFoundError as e:
            return {"ok": False, "error": f"命令不存在: {e}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_install_task(self, task_id: str) -> dict | None:
        """查询安装任务状态。"""
        return self._install_tasks.get(task_id)

    def get_install_tasks(self, node_id: str = "") -> list[dict]:
        """获取所有安装任务（可按 node 过滤）。"""
        tasks = list(self._install_tasks.values())
        if node_id:
            tasks = [t for t in tasks if t.get("node_id") == node_id]
        return sorted(tasks, key=lambda t: t.get("started_at", 0), reverse=True)

    def retry_install(self, node_id: str, mcp_id: str) -> dict:
        """重试失败的安装。"""
        with self._lock:
            nc = self.node_configs.get(node_id)
            if not nc or mcp_id not in nc.available_mcps:
                return {"ok": False, "message": "MCP not found"}
            mcp = nc.available_mcps[mcp_id]
            if not mcp.install_command:
                return {"ok": False, "message": "No install command"}

        # 找到对应的 capability_id
        cap_id = None
        for cid, cap in MCP_CATALOG.items():
            if cap.install_command and cap.install_command.split("#")[0].strip() == \
               mcp.install_command.split("#")[0].strip():
                cap_id = cid
                break

        if not cap_id:
            # 直接用存储的命令重新安装
            return self._retry_direct(node_id, mcp_id, mcp.install_command)

        return self.install_mcp(node_id, cap_id, dict(mcp.env))

    def _retry_direct(self, node_id: str, mcp_id: str, install_cmd: str) -> dict:
        """直接重试安装命令。"""
        with self._lock:
            nc = self.node_configs.get(node_id)
            if nc and mcp_id in nc.available_mcps:
                nc.available_mcps[mcp_id].install_status = "installing"
                nc.available_mcps[mcp_id].install_error = ""
                self._save()

        task_id = f"retry_{mcp_id}_{int(time.time())}"
        self._install_tasks[task_id] = {
            "status": "running",
            "node_id": node_id,
            "mcp_id": mcp_id,
            "command": install_cmd,
            "output": "",
            "started_at": time.time(),
        }

        def _run():
            result = self._execute_install(install_cmd)
            task = self._install_tasks[task_id]
            task["output"] = result.get("output", "")
            task["ended_at"] = time.time()
            with self._lock:
                nc = self.node_configs.get(node_id)
                if nc and mcp_id in nc.available_mcps:
                    if result["ok"]:
                        task["status"] = "completed"
                        nc.available_mcps[mcp_id].install_status = "installed"
                        nc.available_mcps[mcp_id].installed_at = time.time()
                        nc.available_mcps[mcp_id].install_error = ""
                    else:
                        task["status"] = "failed"
                        nc.available_mcps[mcp_id].install_status = "failed"
                        nc.available_mcps[mcp_id].install_error = result.get("error", "")[:500]
                    self._save()

        import threading as _th
        _th.Thread(target=_run, daemon=True, name=f"mcp-retry-{mcp_id}").start()

        return {"ok": True, "status": "installing", "task_id": task_id,
                "message": "重新安装中..."}

    def test_mcp_connection(self, config: MCPServerConfig, timeout: float = 15.0) -> dict:
        """Health-check an MCP config by running the real dispatch path.

        Architectural note: this method used to re-implement subprocess
        launch + JSON-RPC handshake inline. That was the reason
        "test connection" could succeed while real calls failed (or
        vice versa) — it was a second, parallel code path. The
        implementation now delegates to ``NodeMCPDispatcher.probe``
        through the router, so *exactly* the same launch/env/
        handshake code runs for tests and real calls. A test that
        passes guarantees a real call will make it past the
        handshake; a test that fails tells you the real call has no
        chance.

        Returns a legacy-shaped dict so existing portal code keeps
        working without changes::

            {"ok": bool, "message": str, "tools": [str], "server_info": dict, "stderr": str}
        """
        result: dict = {"ok": False, "message": "", "tools": [],
                        "server_info": {}, "stderr": ""}

        valid, msg = self.validate_mcp_config(config)
        if not valid:
            result["message"] = f"配置无效: {msg}"
            return result

        # Delegate to the router/dispatcher. The router owns the
        # probe path just like it owns the real call path.
        try:
            from .router import MCPCallRouter
            # The router only needs a hub for authorized calls; probe
            # is admin-initiated and has no agent context, so passing
            # hub=None is fine. (Router.probe does not touch hub.)
            router = MCPCallRouter(hub=None)
            cr = router.probe(config, timeout_s=timeout)
        except Exception as e:
            result["message"] = f"probe crashed: {e}"
            return result

        result["stderr"] = cr.stderr_tail or ""

        if not cr.ok:
            # Stable message for the portal UI. Do NOT translate
            # error_kind into Chinese here — the kind is an API
            # contract, not a user string. The message is the
            # human-readable part.
            result["message"] = cr.error_message or cr.error_kind
            return result

        # Success path — pull tools + server_info out of the probe
        # result content. The dispatcher's probe returns a dict with
        # ``tools`` (names), ``tool_manifests`` (full manifests), and
        # ``server_info``.
        content = cr.content if isinstance(cr.content, dict) else {}
        result["tools"] = list(content.get("tools") or [])
        # Full manifests are forwarded too so the Portal UI can show
        # each tool's description and parameter schema right on the
        # test dialog, saving the operator a round trip.
        result["tool_manifests"] = list(content.get("tool_manifests") or [])
        si = content.get("server_info") or {}
        if isinstance(si, dict):
            result["server_info"] = {
                "protocolVersion": si.get("protocolVersion", "") if isinstance(si, dict) else "",
                "serverInfo": si,
            }
        result["ok"] = True
        tcount = len(result["tools"])
        result["message"] = (f"连接成功 ✓ 共 {tcount} 个工具"
                             + (f": {', '.join(result['tools'][:6])}"
                                + ("..." if tcount > 6 else "") if tcount else ""))

        # A successful test is also a valid cache population event:
        # same subprocess, same handshake, same JSON-RPC — the tool
        # list we just observed IS the ground truth. Write it through
        # so the operator's "test connection" click simultaneously
        # warms the manifest cache for downstream readers (MCP.md,
        # list_mcps, bind dialog).
        try:
            self.tool_manifests.put(
                config.id,
                result["tool_manifests"],
                result["server_info"].get("serverInfo", {}) or {},
                ToolManifestCache.compute_sig(config),
            )
        except Exception:
            logger.exception("test_mcp_connection: cache write failed")

        return result


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_mcp_manager_instance: MCPManager | None = None
_mcp_manager_lock = threading.Lock()


def init_mcp_manager(data_dir: str | None = None) -> MCPManager:
    """初始化全局MCP管理器。(Initialize global MCPManager singleton.)"""
    global _mcp_manager_instance
    with _mcp_manager_lock:
        if _mcp_manager_instance is None:
            _mcp_manager_instance = MCPManager(data_dir)
        return _mcp_manager_instance


def get_mcp_manager() -> MCPManager:
    """获取全局MCP管理器。(Get global MCPManager singleton.)"""
    global _mcp_manager_instance
    if _mcp_manager_instance is None:
        return init_mcp_manager()
    return _mcp_manager_instance
