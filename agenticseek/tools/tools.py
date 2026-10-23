"""Tool base class and the block parser.

Derived from AgenticSeek ``sources/tools/tools.py`` (GPL-3.0, see NOTICE.md).

A tool is addressed by a fenced block:

.. code-block:: text

    ```python:hello.py
    print("hello")
    ```

``load_exec_block`` extracts every block with the tool's tag; a first line
``name:path`` (after the tag) means "save the payload to that file inside the
workspace" — that is how upstream models are told to write files.

NovaMind adaptations:

* no ``config.ini`` — the vendored engine has no configuration file. Behaviour
  is controlled by environment variables (documented in
  ``docs/AGENTICSEEK_INTEGRATION.md``);
* ``save_block`` resolves the path inside the NovaMind workspace and refuses
  escapes; the sandbox runner owns execution, so tools never spawn processes
  themselves.
"""

from __future__ import annotations

import os
import re

from agenticseek.core.logger import Logger
from agenticseek.core.workspace import get_work_dir, resolve_workspace_path

#: A save path must look like a relative file name, not a sentence.
_SAVE_PATH = re.compile(r"^[A-Za-z0-9_./\-]{1,200}$")


class Tools:
    """Base class of every tool the agents can call."""

    tag = "undefined"
    name = "undefined"
    description = "undefined"
    #: ``safe`` (read-only), ``write`` (needs a confirmation by default),
    #: ``danger`` (always confirmed, irreversible or outward-facing).
    risk = "write"

    def __init__(self):
        self.logger = Logger("tools.log")
        self.work_dir = get_work_dir()
        self.executable_blocks_found = False

    def get_work_dir(self) -> str:
        return self.work_dir

    def resolve_path(self, path: str) -> str:
        """Resolve ``path`` inside the workspace, refusing escapes."""
        return resolve_workspace_path(path, self.work_dir)

    def execute(self, payload: str) -> str:
        """Run one block. Returns text for the model (``[success]``/``[failure]``)."""
        raise NotImplementedError

    def execution_failure_check(self, output: str) -> bool:
        """True when the output means the action failed."""
        return str(output or "").lstrip().startswith("[failure]")

    def interpreter_feedback(self, output: str) -> str:
        """Feedback message handed back to the model (upstream name)."""
        return str(output or "")

    # ── block parsing ─────────────────────────────────────────────────────
    def load_exec_block(self, llm_text: str):
        """Extract this tool's blocks from a model answer.

        Returns:
            (list[str], str | None): payloads and the save path, or (None, None)
            when the answer contains no block with this tag.
        """
        if self.tag == "undefined":
            raise RuntimeError("Tool tag is not defined")
        llm_text = str(llm_text or "")
        start_tag = f"```{self.tag}"
        end_tag = "```"
        if start_tag not in llm_text:
            return None, None

        blocks: list[str] = []
        save_path = None
        index = 0
        while True:
            start = llm_text.find(start_tag, index)
            if start == -1:
                break
            line_start = llm_text.rfind("\n", 0, start) + 1
            leading = llm_text[line_start:start]
            end = llm_text.find(end_tag, start + len(start_tag))
            if end == -1:
                break
            content = llm_text[start + len(start_tag):end]
            if leading:
                content = "\n".join(
                    line[len(leading):] if line.startswith(leading) else line
                    for line in content.split("\n"))
            header, separator, body = content.partition("\n")
            candidate = ""
            if header.strip().startswith(":"):
                candidate = header.strip()[1:].strip()
                content = body if separator else ""
            elif ":" in header and _SAVE_PATH.match(header.split(":", 1)[1].strip() or ""):
                # Upstream also accepts "hello.py:print(...)" style first lines.
                candidate = header.split(":", 1)[1].strip()
                content = body if separator else ""
            if candidate and _SAVE_PATH.match(candidate):
                save_path = candidate
            if content.strip():
                blocks.append(content)
            index = end + len(end_tag)
        self.executable_blocks_found = bool(blocks)
        return (blocks or None), save_path

    # ── workspace writing ─────────────────────────────────────────────────
    def save_block(self, payload: str, save_path: str) -> str:
        """Write a payload into the workspace; returns the relative path."""
        target = self.resolve_path(save_path)
        os.makedirs(os.path.dirname(target) or self.work_dir, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return os.path.relpath(target, self.work_dir)
