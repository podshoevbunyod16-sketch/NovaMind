"""AgenticSeek agent engine, vendored into NovaMind.

The code in this package is derived from the AgenticSeek project
(https://github.com/Fosowl/agenticSeek), Copyright (C) 2025 Fosowl and the
AgenticSeek contributors, licensed under the GNU General Public License v3.0.
See ``agenticseek/LICENSE`` and ``NOTICE.md`` in the repository root.

What is derived and what is NovaMind's own is documented per file and in
``docs/AGENTICSEEK_INTEGRATION.md``.

Public entry point:

    from agenticseek.engine import Agent, EngineContext, run_stream

    for event in run_stream(goal, context):
        ...  # NDJSON-ready dicts: stage / plan / step / tool / confirm / ...
"""

__all__ = ["engine"]
__version__ = "1.0.0-novamind"
