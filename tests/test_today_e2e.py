"""End-to-end use cases covering everything wired up 2026-04-28.

Exercises the real Agent / Project / ProjectChatEngine code paths with
the LLM stubbed out, so we can assert:

  1. per-context message isolation (solo / project:{id} / meeting:{id})
  2. ProjectChatEngine.dispatch_to_agent posts chat + spawns scoped thread
     + propagates source metadata
  3. create_milestone with responsible_agent_id != caller fires the
     delegation chat message and triggers the responsible agent
  4. user_id flows through hub.project_chat → handle_user_message →
     agent.messages bucket source field
  5. agent.chat() injects "X 派单给你" hint into dynamic context when
     last user msg has source="agent:<id>"
  6. dynamic-context file selection: project ctx skips Tasks.md /
     Scheduled.md / Project.md (only MCP.md remains)
  7. granted_skills.md is NOT in dynamic ctx (moved to STATIC) and
     STATIC prompt hash invalidates when granted_skills changes

These are integration-level — they wire real Agent + ProjectChatEngine
together, replacing only the LLM call with a fake.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point TUDOU_CLAW_DATA_DIR at a tmpdir so workspace files / agent
    state don't bleed into the developer's home dir during tests."""
    monkeypatch.setenv("TUDOU_CLAW_DATA_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the LLM call inside agent.chat with a deterministic
    callable that records what it was given and returns a canned reply.

    The hook point is `agent_llm.AgentLLMMixin.chat` is too high; we
    intercept inside _direct_chat → agent.chat by mocking the agent's
    internal chat-loop driver. The simplest: monkey-patch
    `Agent.chat` itself to capture (message, context_id, source) and
    return a string."""
    calls = []

    def fake_chat(self, user_message, on_event=None, abort_check=None,
                  source: str = "admin", context_id: str = "solo") -> str:
        # Mimic what real chat does for our assertions:
        #   * switch to the right per-context bucket
        #   * append the user message with source field
        #   * record the call
        self._switch_context(context_id)
        self.messages.append({
            "role": "user",
            "content": user_message if isinstance(user_message, str) else str(user_message),
            "source": source,
        })
        # Pretend we replied
        reply = f"[fake reply from {self.name} ctx={context_id} src={source}]"
        self.messages.append({"role": "assistant", "content": reply})
        calls.append({
            "agent_id": self.id,
            "agent_name": self.name,
            "context_id": context_id,
            "source": source,
            "user_message": user_message[:200] if isinstance(user_message, str) else None,
        })
        return reply

    from app.agent import Agent
    monkeypatch.setattr(Agent, "chat", fake_chat, raising=True)
    return calls


@pytest.fixture
def hub_with_three_agents(isolated_data_dir, fake_llm):
    """Build a real Hub with three agents (小土 / 小刚 / 小专) wired up."""
    from app.hub import Hub
    from app.agent import Agent
    from app.project import Project, ProjectMember

    hub = Hub(data_dir=str(isolated_data_dir))
    # Install as the global singleton so tools' _get_hub() returns this one.
    import app.hub._core as _hub_core
    _hub_core._hub = hub
    # 3 agents
    a_xt = Agent(id="a_xiaotu", name="小土", role="general")
    a_xg = Agent(id="a_xiaogang", name="小刚", role="general")
    a_xz = Agent(id="a_xiaozhuan", name="小专", role="researcher")
    for ag in (a_xt, a_xg, a_xz):
        hub.agents[ag.id] = ag
    # Activate hub so granted-skills static block has a registry to look up
    import sys as _sys
    _llm_mod = _sys.modules.get("app.llm")
    if _llm_mod:
        _llm_mod._active_hub = hub
    # Project with all 3 as members
    proj = Project(
        id="p_market",
        name="中东中亚云市场深度研究",
    )
    proj.members = [
        ProjectMember(agent_id=a_xt.id, responsibility="协调汇总"),
        ProjectMember(agent_id=a_xg.id, responsibility="政策监管"),
        ProjectMember(agent_id=a_xz.id, responsibility="行业洞察"),
    ]
    hub.projects[proj.id] = proj
    return {"hub": hub, "project": proj, "xt": a_xt, "xg": a_xg, "xz": a_xz}


# ──────────────────────────────────────────────────────────────────────
# Test 1 — per-context isolation
# ──────────────────────────────────────────────────────────────────────

def test_per_context_message_isolation(hub_with_three_agents):
    """Messages in solo / project / meeting contexts must not bleed."""
    xt = hub_with_three_agents["xt"]
    proj = hub_with_three_agents["project"]
    hub = hub_with_three_agents["hub"]

    # Solo turn
    hub._direct_chat(xt.id, "hi solo", context_id="solo", source="user")
    assert len(xt._messages_by_context.get("solo", [])) == 2  # user + asst

    # Project turn — fresh bucket
    hub._direct_chat(xt.id, "hi project",
                     context_id=f"project:{proj.id}", source="user")
    proj_bucket = xt._messages_by_context.get(f"project:{proj.id}", [])
    assert len(proj_bucket) == 2
    assert proj_bucket[0]["content"] == "hi project"

    # Meeting turn — also fresh
    hub._direct_chat(xt.id, "hi meeting",
                     context_id="meeting:m1", source="user")
    meet_bucket = xt._messages_by_context.get("meeting:m1", [])
    assert len(meet_bucket) == 2

    # Solo bucket still has only the solo content
    solo_bucket = xt._messages_by_context["solo"]
    assert all("project" not in m["content"] and "meeting" not in m["content"]
               for m in solo_bucket)


# ──────────────────────────────────────────────────────────────────────
# Test 2 — dispatch_to_agent posts chat, propagates source, triggers reply
# ──────────────────────────────────────────────────────────────────────

def test_dispatch_to_agent_posts_and_triggers(hub_with_three_agents, fake_llm):
    """The unified entry should:
      * post the trigger message into project.chat_history
      * tag it with source metadata
      * spawn a thread that calls agent.chat with source propagated
    """
    hub = hub_with_three_agents["hub"]
    proj = hub_with_three_agents["project"]
    xg = hub_with_three_agents["xg"]
    xt = hub_with_three_agents["xt"]

    n_msgs_before = len(proj.chat_history)

    ok = hub.project_chat_engine.dispatch_to_agent(
        proj, xg.id, "请你研究中东云政策法规",
        source="agent",
        source_id=xt.id,
        source_label=f"{xt.role}-{xt.name}",
        msg_type="task_assignment",
    )
    assert ok is True

    # Trigger message should be in chat history
    assert len(proj.chat_history) == n_msgs_before + 1
    posted = proj.chat_history[-1]
    assert posted.content == "请你研究中东云政策法规"
    assert posted.msg_type == "task_assignment"
    assert posted.sender == xt.id
    assert "小土" in posted.sender_name

    # Wait for the dispatched daemon thread to land on fake LLM
    deadline = time.time() + 2.0
    while time.time() < deadline and not any(
            c["agent_id"] == xg.id for c in fake_llm):
        time.sleep(0.02)
    xg_calls = [c for c in fake_llm if c["agent_id"] == xg.id]
    assert xg_calls, "dispatched daemon should have hit fake_llm"

    call = xg_calls[0]
    assert call["context_id"] == f"project:{proj.id}"
    # Source format: "<kind>:<id>" — chain encoded "agent:<source_id>"
    assert call["source"].startswith("agent:")
    assert xt.id in call["source"]


# ──────────────────────────────────────────────────────────────────────
# Test 3 — create_milestone fires delegation when responsible != caller
# ──────────────────────────────────────────────────────────────────────

def test_create_milestone_fires_delegation(hub_with_three_agents, fake_llm,
                                            monkeypatch):
    """create_milestone(responsible_agent_id != caller) should:
      * write the milestone
      * post a delegate message into chat history
      * trigger the responsible agent's reply via dispatch_to_agent
    """
    hub = hub_with_three_agents["hub"]
    proj = hub_with_three_agents["project"]
    xt = hub_with_three_agents["xt"]
    xg = hub_with_three_agents["xg"]

    # The tool resolves project via thread-local; set it.
    from app.project_context import set_project_context
    set_project_context(proj.id)
    try:
        from app.tools_split.project import _tool_create_milestone
        # Caller is 小土; responsible is 小刚
        result = _tool_create_milestone(
            name="模块③④ 政策监管与技术趋势",
            responsible_agent_id=xg.id,
            description="重点关注沙特/阿联酋本地化要求 + 欧盟 GDPR 影响",
            _caller_agent_id=xt.id,
        )
    finally:
        set_project_context("")

    assert "Milestone created" in result
    assert "assigned to general-小刚" in result
    # Milestone exists
    assert len(proj.milestones) == 1
    ms = proj.milestones[0]
    assert ms.responsible_agent_id == xg.id

    # Delegation chat message posted
    delegate_msgs = [m for m in proj.chat_history
                     if m.msg_type == "task_assignment"]
    assert len(delegate_msgs) == 1
    delegate = delegate_msgs[0]
    assert "@general-小刚" in delegate.content
    assert ms.id in delegate.content
    assert "模块③④" in delegate.content

    # 小刚's daemon thread should fire fake LLM
    deadline = time.time() + 2.0
    while time.time() < deadline and not any(
            c["agent_id"] == xg.id for c in fake_llm):
        time.sleep(0.02)
    xg_calls = [c for c in fake_llm if c["agent_id"] == xg.id]
    assert xg_calls, "responsible agent should have been triggered"
    # source should encode "agent:<caller_id>"
    assert xg_calls[0]["source"].startswith("agent:")
    assert xt.id in xg_calls[0]["source"]


# ──────────────────────────────────────────────────────────────────────
# Test 4 — user_id flows through hub.project_chat
# ──────────────────────────────────────────────────────────────────────

def test_user_id_flows_to_source(hub_with_three_agents, fake_llm):
    hub = hub_with_three_agents["hub"]
    proj = hub_with_three_agents["project"]
    xt = hub_with_three_agents["xt"]

    hub.project_chat(
        proj.id,
        f"@general-小土 帮我列三个云厂商",
        target_agents=[xt.id],
        user_id="admin_42",
    )
    # Wait for async daemon
    deadline = time.time() + 2.0
    while time.time() < deadline and not fake_llm:
        time.sleep(0.02)
    assert fake_llm, "no LLM call recorded"
    call = fake_llm[-1]
    assert call["source"] == "user:admin_42", \
        f"expected source=user:admin_42, got {call['source']}"


# ──────────────────────────────────────────────────────────────────────
# Test 5 — source_hint section appears when last user msg is agent-sourced
# ──────────────────────────────────────────────────────────────────────

def test_source_hint_in_dynamic_context(hub_with_three_agents):
    xg = hub_with_three_agents["xg"]
    xt = hub_with_three_agents["xt"]

    # Switch xg to a project context and append a user msg with source=agent:xt
    xg._switch_context("project:p_market")
    xg.messages.append({
        "role": "user",
        "content": "请研究中东云政策",
        "source": f"agent:{xt.id}",
    })
    # Build dynamic context — source hint should appear
    ctx = xg._build_dynamic_context(current_query="请研究中东云政策")
    assert "当前任务来源" in ctx, \
        f"source_hint section missing; got first 200 chars: {ctx[:200]!r}"
    assert "小土" in ctx, "source agent name should appear in hint"

    # Reset: a user-sourced last msg should NOT inject the hint
    xg._messages_by_context["project:p_market"] = []
    xg.messages.append({
        "role": "user",
        "content": "user direct query",
        "source": "user",
    })
    ctx2 = xg._build_dynamic_context(current_query="user direct query")
    assert "当前任务来源" not in ctx2, \
        "user-sourced turn should not inject source_hint"


# ──────────────────────────────────────────────────────────────────────
# Test 6 — per-context file filtering (project skips Tasks/Scheduled/Project.md)
# ──────────────────────────────────────────────────────────────────────

def test_scheduled_context_per_context_filtering(hub_with_three_agents):
    xg = hub_with_three_agents["xg"]
    import re

    # Solo: all 4 personal-memo files (Project / MCP / Tasks / Scheduled)
    xg._switch_context("solo")
    solo_ctx = xg._get_scheduled_context()
    solo_files = set(re.findall(r'<\w+ file="workspace/([^"]+)">', solo_ctx))
    assert "Project.md" in solo_files
    assert "Tasks.md" in solo_files
    assert "Scheduled.md" in solo_files
    assert "MCP.md" in solo_files
    # granted_skills.md is NO LONGER injected here (moved to STATIC prompt)
    assert "granted_skills.md" not in solo_files

    # Project context: only MCP.md (capability file)
    xg._switch_context("project:p_market")
    proj_ctx = xg._get_scheduled_context()
    proj_files = set(re.findall(r'<\w+ file="workspace/([^"]+)">', proj_ctx))
    assert "MCP.md" in proj_files
    assert "Project.md" not in proj_files, \
        "Project.md is agent-personal — must not leak into project ctx"
    assert "Tasks.md" not in proj_files
    assert "Scheduled.md" not in proj_files

    # Meeting context: same as project
    xg._switch_context("meeting:m1")
    meet_ctx = xg._get_scheduled_context()
    meet_files = set(re.findall(r'<\w+ file="workspace/([^"]+)">', meet_ctx))
    assert meet_files == proj_files


# ──────────────────────────────────────────────────────────────────────
# Test 7 — granted_skills hash + cache invalidation
# ──────────────────────────────────────────────────────────────────────

def test_granted_skills_hash_invalidates_static_prompt(hub_with_three_agents):
    xg = hub_with_three_agents["xg"]

    h1 = xg._compute_static_prompt_hash()
    xg.granted_skills = ["skill_a"]
    h2 = xg._compute_static_prompt_hash()
    xg.granted_skills = ["skill_a", "skill_b"]
    h3 = xg._compute_static_prompt_hash()
    # Different sets → different hashes
    assert h1 != h2 != h3
    # Same set (different order) → same hash (sorted in hash builder)
    xg.granted_skills = ["skill_b", "skill_a"]
    h3b = xg._compute_static_prompt_hash()
    assert h3 == h3b, "hash must be order-independent for granted_skills"


# ──────────────────────────────────────────────────────────────────────
# Test 8 — persistence round-trip preserves all context buckets
# ──────────────────────────────────────────────────────────────────────

def test_update_milestone_responsibility_reassigns_and_notifies(
        hub_with_three_agents, fake_llm):
    """Reassigning an existing milestone should:
      * change milestone.responsible_agent_id
      * fire the delegation chat to the NEW owner
      * trigger the new owner's _agent_respond
      * post a courtesy notice to the old owner (notify_old=True default)
    """
    hub = hub_with_three_agents["hub"]
    proj = hub_with_three_agents["project"]
    xt = hub_with_three_agents["xt"]
    xg = hub_with_three_agents["xg"]
    xz = hub_with_three_agents["xz"]

    # Bootstrap: 小土 creates milestone assigned to 小刚
    from app.project_context import set_project_context
    set_project_context(proj.id)
    try:
        from app.tools_split.project import (
            _tool_create_milestone,
            _tool_update_milestone_responsibility,
        )
        _tool_create_milestone(
            name="模块④ 关键行业与客户需求",
            responsible_agent_id=xg.id,
            _caller_agent_id=xt.id,
        )
        # Wait for delegate dispatch to fire 小刚's first reply
        deadline = time.time() + 2.0
        while time.time() < deadline and not any(
                c["agent_id"] == xg.id for c in fake_llm):
            time.sleep(0.02)
        n_xg_first = sum(1 for c in fake_llm if c["agent_id"] == xg.id)
        assert n_xg_first >= 1, "小刚 should have been triggered by initial milestone"

        ms = proj.milestones[-1]
        assert ms.responsible_agent_id == xg.id

        # Now: 小土 reassigns the same milestone to 小专 (with reason)
        result = _tool_update_milestone_responsibility(
            milestone_id=ms.id,
            new_responsible_agent_id=xz.id,
            reason="小专更熟悉行业需求,小刚先专注基础设施",
            _caller_agent_id=xt.id,
        )
    finally:
        set_project_context("")

    assert "reassigned" in result
    assert ms.responsible_agent_id == xz.id

    # New-owner trigger message in chat history
    assignments = [m for m in proj.chat_history
                   if m.msg_type == "task_assignment"]
    # 1 from create_milestone + 1 from reassign = 2
    assert len(assignments) >= 2
    latest_assignment = assignments[-1]
    assert "@researcher-小专" in latest_assignment.content
    assert ms.id in latest_assignment.content
    assert "小专更熟悉行业需求" in latest_assignment.content

    # Old-owner courtesy notice (msg_type=system)
    sys_msgs = [m for m in proj.chat_history if m.msg_type == "system"]
    assert any("@general-小刚" in m.content
               and "已转交给" in m.content
               for m in sys_msgs), \
        "expected courtesy release notice to 小刚"

    # 小专 (new owner) was actually triggered by dispatch_to_agent
    deadline = time.time() + 2.0
    while time.time() < deadline and not any(
            c["agent_id"] == xz.id for c in fake_llm):
        time.sleep(0.02)
    xz_calls = [c for c in fake_llm if c["agent_id"] == xz.id]
    assert xz_calls, "new owner should have been triggered to respond"
    assert xz_calls[0]["source"].startswith("agent:")
    assert xt.id in xz_calls[0]["source"]


def test_at_mention_propagation_in_agent_reply(hub_with_three_agents,
                                                 fake_llm, monkeypatch):
    """When an agent's reply contains @-mentions of teammates, those
    teammates should be auto-triggered — same UX as a user @-mention.
    Source chain prevents bounce-back: A→B→A doesn't loop forever.
    """
    hub = hub_with_three_agents["hub"]
    proj = hub_with_three_agents["project"]
    xt = hub_with_three_agents["xt"]
    xg = hub_with_three_agents["xg"]
    xz = hub_with_three_agents["xz"]

    # Override fake chat: 小土's reply contains @-mentions to 小刚 and 小专;
    # 小刚 and 小专 reply with no further mentions (so chain terminates).
    def chat_with_mentions(self, user_message, on_event=None,
                            abort_check=None, source: str = "admin",
                            context_id: str = "solo") -> str:
        self._switch_context(context_id)
        self.messages.append({
            "role": "user",
            "content": user_message if isinstance(user_message, str) else str(user_message),
            "source": source,
        })
        if self.id == xt.id:
            reply = (
                "我把任务拆成两块,大家分头去做。\n"
                "@general-小刚 你负责模块③④ 政策监管,先列出本地化要求清单。\n"
                "@researcher-小专 你负责模块⑤⑥ 行业分析,先看 cloud spending 占比。"
            )
        else:
            reply = f"[{self.name} 收到任务,开始工作]"
        self.messages.append({"role": "assistant", "content": reply})
        return reply
    from app.agent import Agent
    monkeypatch.setattr(Agent, "chat", chat_with_mentions, raising=True)

    # User @s 小土
    hub.project_chat(
        proj.id,
        "@general-小土 你拆解一下市场研究,分给小刚和小专",
        target_agents=[xt.id],
        user_id="admin",
    )

    # Wait for the cascade: 小土 replies → @-mention triggers 小刚 + 小专
    deadline = time.time() + 3.0
    while time.time() < deadline:
        # We need 小土 (1 reply), 小刚 (1 reply), 小专 (1 reply) = at least 3
        # entries in chat history with role "agent" (msg_type chat)
        agent_replies = [m for m in proj.chat_history
                         if m.sender in (xt.id, xg.id, xz.id)
                         and m.msg_type == "chat"]
        if len(agent_replies) >= 3:
            break
        time.sleep(0.05)

    senders = {m.sender for m in proj.chat_history
               if m.msg_type == "chat" and m.sender != "user"}
    assert xt.id in senders, "小土 should have replied"
    assert xg.id in senders, "小刚 should have been triggered by @-mention"
    assert xz.id in senders, "小专 should have been triggered by @-mention"

    # Verify source chain: 小刚's turn should record source="agent:<xt.id>"
    xg_msgs = xg._messages_by_context.get(f"project:{proj.id}", [])
    user_msgs_to_xg = [m for m in xg_msgs if m.get("role") == "user"]
    assert user_msgs_to_xg, "小刚 should have a user msg in project bucket"
    assert user_msgs_to_xg[-1]["source"].startswith("agent:")
    assert xt.id in user_msgs_to_xg[-1]["source"]


def test_at_mention_bounce_back_prevented(hub_with_three_agents,
                                            fake_llm, monkeypatch):
    """Loop prevention: if A is triggered by source="agent:B" and A's
    reply @s B, B should NOT be re-triggered (or we'd have an infinite
    bounce A↔B)."""
    hub = hub_with_three_agents["hub"]
    proj = hub_with_three_agents["project"]
    xt = hub_with_three_agents["xt"]
    xg = hub_with_three_agents["xg"]

    # Both 小土 and 小刚 @ each other in their replies. Should terminate
    # after one round, not bounce.
    def chat_mutual(self, user_message, on_event=None, abort_check=None,
                     source: str = "admin", context_id: str = "solo") -> str:
        self._switch_context(context_id)
        self.messages.append({
            "role": "user",
            "content": str(user_message)[:200],
            "source": source,
        })
        if self.id == xt.id:
            reply = "@general-小刚 你来弄一下"
        elif self.id == xg.id:
            reply = "@general-小土 我做完了反馈给你"
        else:
            reply = "ok"
        self.messages.append({"role": "assistant", "content": reply})
        return reply
    from app.agent import Agent
    monkeypatch.setattr(Agent, "chat", chat_mutual, raising=True)

    hub.project_chat(proj.id, "@general-小土 你帮我搞一下",
                      target_agents=[xt.id], user_id="admin")
    # Give the cascade plenty of time — if bounce-back was broken, this
    # would still produce many turns.
    time.sleep(2.0)

    xt_replies = sum(1 for m in proj.chat_history
                     if m.sender == xt.id and m.msg_type == "chat")
    xg_replies = sum(1 for m in proj.chat_history
                     if m.sender == xg.id and m.msg_type == "chat")
    # Expected: 小土 1 (from user) + 小刚 1 (from 小土's mention).
    # Bounce-back prevention should stop 小刚's @小土 from re-triggering 小土.
    assert xt_replies <= 2, \
        f"bounce-back not prevented: 小土 replied {xt_replies} times"
    assert xg_replies <= 2


def test_mcp_call_redirects_intra_agent_names(hub_with_three_agents):
    """When LLM tries `mcp_call(tool='send_message' / 'send_email' / ...)`
    AND the recipient looks like a teammate name (no real email address),
    bounce back. Real external addresses (e.g. user's Gmail) MUST pass
    through — the 2026-04-29 fix corrected the previous over-block that
    flagged ALL send_email calls regardless of recipient."""
    from app.tools_split.mcp import _tool_mcp_call, _looks_like_external_email
    xt = hub_with_three_agents["xt"]

    # 1) Recipient looks like a teammate name → block
    for bad_tool in ("send_message", "send_email", "delegate",
                     "notify_agent", "handoff"):
        result = _tool_mcp_call(
            mcp_id="some_mcp_id",
            tool=bad_tool,
            arguments={"to": "小刚"},
            _caller_agent_id=xt.id,
        )
        assert result.startswith("Error:"), \
            f"intra-agent tool '{bad_tool}' with teammate-name recipient should be blocked"
        assert "@-mention" in result.lower() or "@<role>-<name>" in result
        assert "send_message" in result  # mentions the built-in alternative
        assert "[id=" in result          # tells LLM where to find ids

    # 2) Recipient is a real external email — MUST pass through.
    # We can't easily mock _stub.call here (the call goes to real router),
    # but we can verify the error message is NOT the intra-agent redirect.
    for ext_args in (
        {"to": "user@gmail.com"},
        {"recipients": ["pang.alano1983@gmail.com"]},
        {"to": "alice@example.org", "subject": "hi"},
        {"to_email": "bob@company.cn"},
        {"address": "team@startup.io"},
    ):
        assert _looks_like_external_email(ext_args), \
            f"external email {ext_args} should be detected as external"

    # And the inverse: teammate names / empty / agent IDs are NOT external
    for tm_args in (
        {"to": "小刚"},
        {"to": "@general-小刚"},
        {"recipient": "agent_a16c2710"},
        {"to": ""},
        {},
    ):
        assert not _looks_like_external_email(tm_args), \
            f"teammate-style recipient {tm_args} should NOT be detected as external"


def test_team_lines_includes_agent_id(hub_with_three_agents):
    """Project chat prompt must expose [id=...] for every member so the LLM
    can pass agent_id into create_milestone / send_message / etc. Without
    this the LLM will revert to text-only "I assign X to 小刚" plans
    that never trigger anyone (the original 嘴上派单 bug)."""
    hub = hub_with_three_agents["hub"]
    proj = hub_with_three_agents["project"]
    xt = hub_with_three_agents["xt"]

    member = next(m for m in proj.members if m.agent_id == xt.id)
    prompt = hub.project_chat_engine._build_chat_prompt(
        proj, xt, member, "test message",
    )
    # Every member's id should appear in the team list
    for m in proj.members:
        assert f"[id={m.agent_id}]" in prompt, \
            f"member id {m.agent_id} missing from team list"
    # The delegation-rules block must be present and mention all 4 paths
    # (@-mention preferred + 4 tool-call alternatives).
    assert "派单/通知队员的方式" in prompt
    assert "在你的回复正文里直接 @ 对方" in prompt
    assert "create_milestone" in prompt
    assert "send_message" in prompt
    assert "update_milestone_responsibility" in prompt


def test_clear_project_chat_endpoint(hub_with_three_agents):
    """The new DELETE /chat backend should:
      * empty project.chat_history
      * drop each member agent's project:{id} bucket
      * leave solo / other-project / meeting buckets on each agent alone
      * leave tasks / milestones / members untouched
    Wires the actual route handler against a live Hub.
    """
    hub = hub_with_three_agents["hub"]
    proj = hub_with_three_agents["project"]
    xt = hub_with_three_agents["xt"]
    xg = hub_with_three_agents["xg"]

    # Populate state in three buckets per agent so we can assert isolation.
    for ag in (xt, xg):
        ag._switch_context("solo")
        ag.messages.append({"role": "user", "content": "solo persists",
                             "source": "user"})
        ag._switch_context(f"project:{proj.id}")
        ag.messages.extend([
            {"role": "user", "content": "should be cleared",
             "source": "user"},
            {"role": "assistant", "content": "asst reply"},
        ])
        ag._switch_context("meeting:m1")
        ag.messages.append({"role": "user", "content": "meeting persists",
                             "source": "user"})
    proj.chat_history = []
    proj.post_message(sender="user", sender_name="User",
                      content="msg 1", msg_type="chat")
    proj.post_message(sender=xt.id, sender_name="general-小土",
                      content="reply 1", msg_type="chat")
    # Sanity before clear
    assert len(proj.chat_history) == 2
    assert len(xt._messages_by_context[f"project:{proj.id}"]) == 2

    # Stash a milestone too — should survive the clear.
    from app.project import ProjectMilestone
    proj.milestones.append(ProjectMilestone(id="ms1", name="m1"))

    # Invoke the route handler synchronously.
    import asyncio
    from fastapi import HTTPException
    from app.api.routers.projects import clear_project_chat

    class _U:
        user_id = "test_admin"
        role = "superAdmin"
    try:
        result = asyncio.run(clear_project_chat(proj.id, hub=hub, user=_U()))
    except HTTPException as e:
        pytest.fail(f"clear_project_chat raised HTTP {e.status_code}: {e.detail}")

    assert result["ok"] is True
    assert result["ui_messages_cleared"] == 2
    # Both xt and xg had project bucket → 2 agents cleared
    assert result["agents_cleared"] == 2
    # xt: 2 msgs, xg: 2 msgs, total 4
    assert result["agent_messages_cleared"] == 4

    # Effect: project chat_history empty
    assert proj.chat_history == []
    # Effect: project bucket dropped from both agents
    assert f"project:{proj.id}" not in xt._messages_by_context
    assert f"project:{proj.id}" not in xg._messages_by_context
    # Solo + meeting buckets on each agent UNTOUCHED
    assert xt._messages_by_context["solo"][0]["content"] == "solo persists"
    assert xt._messages_by_context["meeting:m1"][0]["content"] == "meeting persists"
    assert xg._messages_by_context["solo"][0]["content"] == "solo persists"
    assert xg._messages_by_context["meeting:m1"][0]["content"] == "meeting persists"
    # Milestone preserved
    assert len(proj.milestones) == 1
    assert proj.milestones[0].id == "ms1"


def test_messages_by_context_persistence_roundtrip(hub_with_three_agents):
    from app.agent import Agent
    xt = hub_with_three_agents["xt"]
    # Populate three contexts
    xt._switch_context("solo")
    xt.messages.append({"role": "user", "content": "solo msg",
                         "source": "user"})
    xt._switch_context("project:p_market")
    xt.messages.append({"role": "user", "content": "proj msg",
                         "source": "user:admin"})
    xt._switch_context("meeting:m1")
    xt.messages.append({"role": "user", "content": "meet msg",
                         "source": "agent:other"})

    d = xt.to_persist_dict()
    assert "messages_by_context" in d
    assert set(d["messages_by_context"].keys()) == \
        {"solo", "project:p_market", "meeting:m1"}

    # Reload — buckets restored, source preserved
    xt2 = Agent.from_persist_dict(d)
    assert xt2._messages_by_context["solo"][0]["source"] == "user"
    assert xt2._messages_by_context["project:p_market"][0]["source"] == "user:admin"
    assert xt2._messages_by_context["meeting:m1"][0]["source"] == "agent:other"
