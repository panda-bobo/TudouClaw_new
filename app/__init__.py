# Tudou Claws AI Programming Assistant
__version__ = "0.1.0"

# Default runtime data directory — ONE ROOT for everything.
# Override with --data-dir CLI flag or TUDOU_CLAW_DATA_DIR env var.
#
# Directory layout under this root:
#   ~/.tudou_claw/
#   ├── workspaces/
#   │   ├── agents/{agent_id}/        ← each agent's private workspace
#   │   │   ├── workspace/            ← working files, Scheduled.md, Tasks.md
#   │   │   ├── session/
#   │   │   ├── memory/
#   │   │   └── logs/
#   │   └── shared/{project_id}/      ← project shared workspace (all members see)
#   ├── agents.json                   ← agent persistence
#   ├── projects.json                 ← project persistence
#   ├── skills/                       ← global skill files
#   ├── experience/                   ← experience library data
#   └── ...
import os as _os

USER_HOME = _os.path.expanduser("~")
DEFAULT_DATA_DIR = _os.path.join(USER_HOME, ".tudou_claw")

# ── Suppress tqdm progress bars globally (Nov 2026) ────────────────────
# chromadb → sentence-transformers → .encode(show_progress_bar=True) by
# default, which floods logs with
#   Batches: 100%|████| 1/1 [00:00<00:00, 89.93it/s]
# one line per embedding batch. Users reported it drowns real signal.
#
# Defense in depth — TWO layers, both cheap, because either alone has
# escape hatches:
#
#   1. monkey-patch tqdm.__init__(default disable=True). Catches anyone
#      that lets disable default. Loses to callers that pass
#      `disable=False` explicitly — sentence-transformers IS one, since
#      it does `trange(..., disable=not show_progress_bar)`.
#
#   2. force sentence-transformers' logger level to WARNING. Its
#      `encode()` default is "show progress iff our logger is INFO/DEBUG"
#      (sentence_transformers/SentenceTransformer.py: encode()), so
#      WARNING+ silences it without touching tqdm.
#
# Escape hatch: TUDOU_TQDM=1 keeps everything alive (debugging).
if _os.environ.get("TUDOU_TQDM", "0") != "1":
    try:
        from functools import partialmethod as _pm
        # tqdm has MULTIPLE concrete classes and `tqdm.auto.tqdm` is usually
        # a DIFFERENT object from `tqdm.tqdm` (it picks notebook/asyncio/std
        # at import time). sentence-transformers does
        # `from tqdm.autonotebook import trange` → hits auto, bypasses std.
        # So we patch every known tqdm class.
        import tqdm as _tqdm
        import tqdm.std as _tqdm_std   # noqa
        import tqdm.auto as _tqdm_auto  # noqa
        import tqdm.autonotebook as _tqdm_ann  # noqa
        import tqdm.asyncio as _tqdm_ai  # noqa
        _candidates = []
        for _mod, _attr in [
            (_tqdm, "tqdm"), (_tqdm_std, "tqdm"),
            (_tqdm_auto, "tqdm"), (_tqdm_ann, "tqdm"),
            (_tqdm_ai, "tqdm_asyncio"),
        ]:
            _c = getattr(_mod, _attr, None)
            if _c is not None and _c not in _candidates:
                _candidates.append(_c)
        for _cls in _candidates:
            try:
                _cls.__init__ = _pm(_cls.__init__, disable=True)
            except Exception:
                pass
    except Exception:
        pass
    # Layer 2 — sentence-transformers checks ITS OWN logger level for the
    # show_progress_bar default. WARNING silences the default; we don't
    # touch its other log lines (model load info etc. — those stay).
    try:
        import logging as _logging
        _logging.getLogger("sentence_transformers").setLevel(_logging.WARNING)
        # Also the parent `sentence_transformers.SentenceTransformer`
        # logger that the encode() method uses.
        _logging.getLogger(
            "sentence_transformers.SentenceTransformer"
        ).setLevel(_logging.WARNING)
    except Exception:
        pass
