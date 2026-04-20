"""
Drop-in directory for user-supplied tool-call parsers.

Add a ``.py`` file here with at least one ``@register(match=...)``-
decorated class and the parser is picked up on next bootstrap — no
edits to core files required.

Example::

    # my_model.py
    from ..base import NormalizedMessage, register

    @register(match="my-weird-*")
    class MyWeirdParser:
        name = "my_weird"
        def parse(self, raw):
            content = raw.get("content") or ""
            # ...custom extraction here...
            return NormalizedMessage(content=content, tool_calls=[])

See ``base.ToolCallParser`` for the protocol contract.
"""
