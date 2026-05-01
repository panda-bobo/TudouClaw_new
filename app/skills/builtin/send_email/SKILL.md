# send_email

Send an email through a bound email MCP (`agentmail` preferred, falls back to `smtp-server`). The skill wraps the MCP call so you don't juggle `mcp_call` parameters yourself.

## Prerequisites

The agent must have at least one of these MCPs bound:
- `agentmail` (recommended — supports HTML, CC/BCC, attachments)
- `smtp-server` (fallback — plain SMTP)

If neither is bound, the skill errors with `No email MCP available`.

## 工作流（4 步，不要跳步）

1. **Echo the send plan to the user first.** Show exact `to` / `cc` / `bcc` / `subject` and a 1-line body summary, then wait for user confirmation. Especially when a recipient was parsed from earlier conversation rather than typed by the user this turn — never send to an address you only inferred.
2. **Pre-flight validate** with the script in 质量门 below. Any `problems` line means STOP and fix the args.
3. **Call `skill(send_email, …)`** with the validated args.
4. **Report the returned `message_id` back to the user.** A successful send the user doesn't see is the same bug as not sending.

## Canonical call

```python
skill(send_email, {
    "to":      ["alice@example.com"],
    "subject": "Project update — Q2",
    "body":    "Hi Alice,\n\nHere is the latest status...\n\n— Bot"
})
```

That's the minimum. `to` / `subject` / `body` are the only required fields.

## Full shape

```python
skill(send_email, {
    "to":          ["a@example.com", "b@example.com"],   # string OR array
    "subject":     "…",
    "body":        "…",                                   # plain text
    "cc":          ["c@example.com"],                     # optional, same shape as `to`
    "bcc":         ["d@example.com"],                     # optional
    "attachments": ["report.pdf", "/abs/path/chart.png"]  # optional, see below
})
```

## Field notes

- **to / cc / bcc**: a single string is accepted and auto-wrapped into a list. Prefer passing arrays.
- **body**: plain text. HTML is NOT supported by this skill yet — if you need HTML, call the `agentmail` MCP directly via `mcp_call`.
- **from**: do NOT pass. The MCP uses the sender address from its own configuration. Passing `from` here does nothing.
- **attachments**:
  - Relative paths resolve against the agent's sandbox (`workspace/` searched first, then sandbox root).
  - Absolute paths pass through unchanged.
  - Missing files are sent as-is to the MCP, which will return a clear error.

## Returns

```python
{
    "message_id": "abc-123-…",   # set when the MCP returns one
    "sent_count": 2               # len(to)
}
```

## 质量门（发送前必须通过）

Run this on the args you're about to send. Any non-empty `problems` list = STOP and fix the input — do NOT call the skill.

```python
import os, re
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

def validate_send_email(to, subject, body, cc=None, bcc=None, attachments=None):
    problems = []
    # Recipients (to / cc / bcc)
    for label, lst in (("to", to), ("cc", cc), ("bcc", bcc)):
        if lst is None: continue
        if isinstance(lst, str): lst = [lst]
        if label == "to" and not lst:
            problems.append("`to` is empty — at least one recipient required")
        for addr in lst:
            a = (addr or "").strip()
            if not a or not EMAIL_RE.match(a):
                problems.append(f"{label}: invalid email {addr!r}")
    # Subject
    s = (subject or "").strip()
    if not s:
        problems.append("subject is empty")
    elif len(subject) > 200:
        problems.append(f"subject too long ({len(subject)} chars > 200)")
    # Body
    if not (body or "").strip():
        problems.append("body is empty")
    # Attachments — only check absolute paths here; skill resolves relative ones from sandbox
    if attachments:
        items = [attachments] if isinstance(attachments, str) else attachments
        for p in items:
            if os.path.isabs(p) and not os.path.isfile(p):
                problems.append(f"attachment file not found: {p}")
    return problems

problems = validate_send_email(to, subject, body, cc, bcc, attachments)
if problems:
    print("❌ send_email QA failed:")
    for p in problems: print("  -", p)
    raise SystemExit(1)
print(f"✓ send_email QA passed — {len(to) if isinstance(to, list) else 1} recipient(s)")
```

**为什么有这个 gate（incident, 2026-04-30）:** an agent sent an email to a fabricated address because it inferred the recipient from earlier chat without re-confirming. Step 1 of the workflow (echo plan) + this validator together close that hole. The validator alone isn't enough — `foo@bar.com` is valid format but may still be the wrong person.

## Failure modes

| Symptom | Meaning | Fix |
|---|---|---|
| `No email MCP available` | No `agentmail` or `smtp-server` bound to this agent | Bind one in the admin UI, or ask the user to bind it |
| `Invalid recipient` / `550 …` | MCP rejected an address | Check the `to` list; no typos; no trailing spaces |
| Attachment-related error from MCP | Resolved path didn't exist on disk | Either pass an absolute path, or put the file in `workspace/` first |

## Related paths

- Raw MCP call when you need HTML / per-message sender override:
  ```python
  mcp_call(mcp_id="agentmail", tool="send_email", arguments={...})
  ```
- Skill source: `app/skills/builtin/send_email/main.py`
