"""Result aggregator — close the long-task loop.

When a parent task's children all reach DONE, automatically combine
their results into a final deliverable on the parent. Supported modes:

  • ``concat_markdown`` (default for reports) — concatenate child
    results in declared order, with chapter headers from titles.
  • ``merge_code`` — for code projects; files are already in their
    output_path locations (isolation guaranteed by the write-path
    middleware), so "merge" = light static checks + integrity report.
    No content rewriting.
  • ``llm_summarize`` (small content only) — feed concatenated
    children to LLM for a polished summary. Only valid when total
    aggregate < 4K chars (avoid context blowout).
  • ``skill:<name>`` — invoke a registered skill that knows how to
    stitch the kind of output (e.g. ``skill:pptx-stitcher``).
    NOT IMPLEMENTED in MVP — stubbed to fall back to concat_markdown.

Trigger model (caller wires this):
  * Hub heartbeat tick checks each project's parent tasks (those with
    ``decomp_metadata`` set) — if all children are DONE and the parent
    isn't already aggregated, fires the aggregator.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tudouclaw.long_task.aggregate")


def _children_for_parent(project, parent_task_id: str) -> list:
    """Return the project's tasks that point at this parent via
    ``parent_task_id``, sorted by ``decomp_metadata.order`` (falls
    back to creation time)."""
    if not parent_task_id or not getattr(project, "tasks", None):
        return []
    children = [t for t in project.tasks
                if getattr(t, "parent_task_id", "") == parent_task_id]
    def _sort_key(t):
        meta = getattr(t, "decomp_metadata", None) or {}
        return (
            int(meta.get("order", 0) or 0),
            float(getattr(t, "created_at", 0) or 0),
        )
    return sorted(children, key=_sort_key)


def _all_children_done(children: list) -> bool:
    from ..project import ProjectTaskStatus
    if not children:
        return False
    return all(t.status == ProjectTaskStatus.DONE for t in children)


def _already_aggregated(parent_task) -> bool:
    """Aggregator is idempotent — flag it via metadata so a hub
    heartbeat tick doesn't re-aggregate the same parent every loop."""
    meta = getattr(parent_task, "metadata", None) or {}
    return bool(meta.get("aggregated"))


def _mark_aggregated(parent_task, result_text: str, mode: str,
                      child_count: int) -> None:
    if not hasattr(parent_task, "metadata"):
        parent_task.metadata = {}
    parent_task.metadata["aggregated"] = True
    parent_task.metadata["aggregated_at"] = time.time()
    parent_task.metadata["aggregator_mode"] = mode
    parent_task.metadata["aggregator_child_count"] = child_count
    # Truncate the result on the parent's `result` field so it shows
    # in the project UI without bloating storage.
    parent_task.result = result_text[:8000]
    # Move parent BLOCKED → DONE so users see a clean checkmark.
    try:
        from ..project import ProjectTaskStatus
        parent_task.status = ProjectTaskStatus.DONE
    except Exception:
        pass
    parent_task.updated_at = time.time()


# ── Mode: concat_markdown ──────────────────────────────────────────

def _aggregate_concat_markdown(parent_task, children: list,
                                project_root: Path) -> str:
    """Concatenate child results in order, with chapter headers from
    each child's title. Trims internal heading levels so the parent's
    final doc has consistent depth (## for child titles, ### for
    nested headings inside)."""
    parts: list[str] = []
    parent_title = (parent_task.title or "").strip() or "聚合产出"
    parts.append(f"# {parent_title}\n")
    parts.append(f"_(自动合成 · {len(children)} 个子任务)_\n\n")
    for i, t in enumerate(children, 1):
        body = (t.result or "").strip()
        title = (t.title or f"第 {i} 部分").strip()
        parts.append(f"## {i}. {title}\n")
        if body:
            # Demote any H1 in child content to H3 to keep hierarchy
            body_demoted = body.replace("\n# ", "\n### ").replace(
                "\n## ", "\n### ")
            parts.append(body_demoted)
        else:
            parts.append("_(子任务无产出文本)_")
        parts.append("\n\n")
    out = "".join(parts).rstrip() + "\n"
    # Also drop the file to disk so users can download without UI
    try:
        out_path = project_root / "aggregated_output.md"
        out_path.write_text(out, encoding="utf-8")
    except OSError as e:
        logger.warning("aggregate concat: write file failed: %s", e)
    return out


# ── Mode: merge_code ───────────────────────────────────────────────

def _aggregate_merge_code(parent_task, children: list,
                           project_root: Path) -> str:
    """For code projects: each child wrote its files to its isolated
    output_path. We don't move files (they're already in the right
    place). Just produce a structured report of what's there + run
    light static checks if available.
    """
    parts: list[str] = [f"# Code Merge Report — {parent_task.title or 'parent'}\n"]
    parts.append(f"_(自动生成 · {len(children)} 个子模块)_\n\n")
    for t in children:
        meta = t.decomp_metadata or {}
        wd = meta.get("agent_wd") or meta.get("output_path", "(未知)")
        parts.append(f"## ▸ {t.title}")
        parts.append(f"  - wd: `{wd}`")
        parts.append(f"  - assigned: {t.assigned_to or '(未派单)'}")
        # File count under wd
        try:
            wd_p = Path(wd)
            if wd_p.exists() and wd_p.is_dir():
                files = [p for p in wd_p.rglob("*") if p.is_file()]
                parts.append(f"  - files: {len(files)}")
                for f in files[:10]:
                    parts.append(
                        f"    - `{f.relative_to(wd_p)}` "
                        f"({f.stat().st_size}B)")
                if len(files) > 10:
                    parts.append(f"    - ... and {len(files)-10} more")
            else:
                parts.append(f"  - ⚠️ wd 不存在或非目录")
        except Exception as _se:
            parts.append(f"  - ⚠️ scan failed: {_se}")
        if t.result:
            parts.append(
                f"  - last note: {(t.result or '').splitlines()[0][:120]}")
        parts.append("")

    parts.append("\n## 集成提示\n")
    parts.append("- 文件已落到各模块 wd 下,无需 copy")
    parts.append("- 建议手动跑:`pytest` / `tsc --noEmit` / `eslint .` 看是否通过")
    parts.append("- 子模块间共享类型在 `interfaces/` 下(若有)")
    return "\n".join(parts).rstrip() + "\n"


# ── Public API ─────────────────────────────────────────────────────

VALID_MODES = ("concat_markdown", "merge_code", "llm_summarize", "skill")


def aggregate_parent_task(project, parent_task,
                          mode: str = "concat_markdown",
                          project_root_hint: str = "",
                          force: bool = False) -> dict:
    """Run aggregation for a single parent task. Idempotent unless
    ``force=True``. Returns ``{ok, mode, child_count, result_chars,
    skipped_reason?}``.
    """
    if not force and _already_aggregated(parent_task):
        return {"ok": True, "skipped_reason": "already_aggregated",
                "mode": (parent_task.metadata or {}).get("aggregator_mode", "?")}

    children = _children_for_parent(project, parent_task.id)
    if not children:
        return {"ok": False, "skipped_reason": "no_children"}
    if not _all_children_done(children):
        n_done = sum(1 for t in children
                     if t.status.value == "done")
        return {"ok": False, "skipped_reason": "children_not_all_done",
                "done_count": n_done, "total_count": len(children)}

    # Resolve project root (where to drop the merged file)
    root = Path(project_root_hint).expanduser() if project_root_hint else None
    if root is None or not root.exists():
        wd = (getattr(project, "working_directory", "") or "").strip()
        if wd:
            root = Path(wd).expanduser()
        else:
            root = (Path.home() / ".tudou_claw" / "workspaces" /
                    "projects" / project.id)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Mode dispatch
    mode_norm = (mode or "concat_markdown").lower()
    if mode_norm not in VALID_MODES and not mode_norm.startswith("skill:"):
        mode_norm = "concat_markdown"

    if mode_norm == "merge_code":
        result_text = _aggregate_merge_code(parent_task, children, root)
    elif mode_norm == "llm_summarize":
        # MVP: not implemented — fall back to concat with a warning prefix
        logger.info("llm_summarize mode not yet implemented, using concat")
        result_text = ("> _(llm_summarize 模式暂未实现,先用 concat_markdown 兜底)_\n\n"
                       + _aggregate_concat_markdown(parent_task, children, root))
    elif mode_norm.startswith("skill:"):
        logger.info("skill: aggregator mode not yet implemented, using concat")
        result_text = ("> _(skill: 模式暂未实现,先用 concat_markdown 兜底)_\n\n"
                       + _aggregate_concat_markdown(parent_task, children, root))
    else:  # concat_markdown
        result_text = _aggregate_concat_markdown(parent_task, children, root)

    _mark_aggregated(parent_task, result_text, mode_norm, len(children))
    logger.info(
        "Aggregated parent_task=%s mode=%s children=%d result_chars=%d",
        parent_task.id, mode_norm, len(children), len(result_text))
    return {
        "ok": True,
        "mode": mode_norm,
        "child_count": len(children),
        "result_chars": len(result_text),
        "parent_task_id": parent_task.id,
    }


def tick_aggregate(hub) -> int:
    """Hub heartbeat hook — scan every project for parent tasks whose
    children are all DONE but parent isn't yet aggregated. Fires
    aggregator with the mode declared on the original draft (default
    ``concat_markdown``).

    Returns the count of parents aggregated this tick.
    """
    if not hasattr(hub, "list_projects"):
        return 0
    fired = 0
    try:
        # Raw Project objects — list_projects() returns dicts but
        # aggregator needs .tasks / .metadata / ProjectTaskStatus enum.
        projects = list((hub.projects or {}).values())
    except Exception:
        return 0
    for project in projects:
        try:
            # Find parent tasks (those with at least one child via
            # parent_task_id pointer).
            tasks_by_id = {t.id: t for t in (project.tasks or [])}
            parent_ids: set[str] = set()
            for t in project.tasks or []:
                pid = getattr(t, "parent_task_id", "") or ""
                if pid and pid in tasks_by_id:
                    parent_ids.add(pid)
            for pid in parent_ids:
                parent = tasks_by_id.get(pid)
                if not parent or _already_aggregated(parent):
                    continue
                children = _children_for_parent(project, pid)
                if not children or not _all_children_done(children):
                    continue
                # Mode hint: look up draft via metadata or default
                meta = parent.metadata or {}
                mode = meta.get("aggregator_mode_hint", "concat_markdown")
                outcome = aggregate_parent_task(project, parent, mode=mode)
                if outcome.get("ok") and not outcome.get("skipped_reason"):
                    fired += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "aggregate tick: project %s failed: %s",
                getattr(project, "id", "?"), e)
            continue

    if fired > 0:
        # Persist project state since we mutated parent_task.result + metadata
        try:
            if hasattr(hub, "_save_projects"):
                hub._save_projects()
        except Exception:
            pass
        logger.info("aggregate tick: fired %d parent task(s)", fired)
    return fired
