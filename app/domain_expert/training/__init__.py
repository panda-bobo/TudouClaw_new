"""Track C — eval & cleanup framework.

Public surface used by Track D specialty templates and SP-2 LoRA scoring:

    from app.domain_expert.training import trace_cleaner
    cleaned, report = trace_cleaner.clean(traces)

    from app.domain_expert.training import eval_suite
    runner = eval_suite.get("legalbench_zh")
    report = runner.run(model_callable, max_examples=100)
    reports = eval_suite.run_suite(model, [
        {"runner_id": "legalbench_zh"},
        {"runner_id": "citation_accuracy"},
    ])

Importing `eval_suite` triggers registration of the bundled runners.

The full RAFT synthesis pipeline + LoRA training scripts live in SP-2;
Track C only ships the trace cleaning + evaluation primitives those
phases depend on.
"""
from __future__ import annotations

# Re-export the four sub-modules so callers can write
# `from app.domain_expert.training import trace_cleaner` etc.
from . import trace_cleaner
from . import eval_suite
from . import eval_legalbench_zh
from . import eval_citation

__all__ = [
    "trace_cleaner",
    "eval_suite",
    "eval_legalbench_zh",
    "eval_citation",
]
