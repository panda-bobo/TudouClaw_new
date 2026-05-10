"""Red-line safety check for cultivated agents.

The PromptBlock on a SpecialtyTemplate carries a list of CoreRedLine
rules. Each rule may have an optional regex ``pattern`` that, when set,
triggers a hard refusal via :func:`find_red_line_hit`. The check runs
twice in the agent's chat flow (R4):

  * **pre**:  on the normalized user_text, before any LLM call —
              cheap and fast, protects against obviously off-limits
              user prompts.
  * **post**: on the LLM's final_content, before persistence — catches
              cases where the LLM produces forbidden content despite
              the system prompt instructing it not to.

A "hit" replaces the agent's reply with the rule's ``message``. Detail
rules (30-100+) live in the per-agent KB with ``metadata.type=red_line``
and reach the model via R5's typed RAG injection — those are guidance,
not enforcement. CoreRedLines are the 5-10 hard guardrails that the
system enforces deterministically.
"""
from __future__ import annotations

import logging
import re

from .template import CoreRedLine, SpecialtyTemplate

logger = logging.getLogger("tudouclaw.expert.safety")


def find_red_line_hit(
    text: str | None,
    template: SpecialtyTemplate | None,
    *,
    severity: str = "HARD_REFUSE",
) -> CoreRedLine | None:
    """Return the first CoreRedLine whose ``pattern`` matches ``text``.

    Iteration order is the order rules appear on the template, so YAML
    authors can put higher-priority rules first.

    Returns None when:
      - ``text`` is empty
      - ``template`` is None or has no PromptBlock
      - no rule matches at the requested severity
      - a rule has no ``pattern`` (rules without a pattern are
        instructional only — they show up in the system prompt but
        cannot trigger an automatic refusal)

    Invalid regex patterns are logged and skipped, so one broken rule
    can't take down the whole check.
    """
    if not text:
        return None
    if template is None or template.prompt is None:
        return None

    for rl in template.prompt.core_red_lines:
        if rl.severity != severity:
            continue
        if not rl.pattern:
            continue
        try:
            if re.search(rl.pattern, text, re.IGNORECASE):
                return rl
        except re.error as e:
            logger.warning(
                "red-line %s has invalid pattern %r: %s",
                rl.id, rl.pattern, e,
            )
            continue
    return None
