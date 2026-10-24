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

_PSEUDO_WRAP = re.compile(r"</?(?:tool_code|tool_call|function_call)>", re.I)


def normalize_tag_calls(text: str, tag: str) -> str:
    """<web_search>q</web_search> -> ```web_search\nq\n```"""
    text = _PSEUDO_WRAP.sub("", text)
    pat = re.compile(rf"<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>", re.S | re.I)
    return pat.sub(lambda m: f"```{tag}\n{m.group(1).strip()}\n```", text)


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
        #: Путь, в который движок только что записал этот блок (``python:app.py``).
        #: Инструмент решает сам, что с этим делать: исполнитель кода не
        #: запускает записанный файл, а сообщает, как проверить его отдельно.
        self.saved_path = ""

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
    def load_blocks(self, llm_text: str) -> list[tuple[str, "str | None"]]:
        """Все блоки инструмента в порядке ответа: ``[(payload, путь|None), …]``.

        У каждого блока свой путь: раньше на весь ответ возвращался один
        ``save_path``, поэтому несколько блоков ```python:app.py```,
        ```python:templates/base.html``` записывались в последний файл — модель
        «создавала проект», а на диске оставался один файл.
        """
        if self.tag == "undefined":
            raise RuntimeError("Tool tag is not defined")
        llm_text = str(llm_text or "")
        if self.risk == "safe":
            llm_text = normalize_tag_calls(llm_text, self.tag)
        start_tag = f"```{self.tag}"
        end_tag = "```"
        if start_tag not in llm_text:
            return []

        found: list[tuple[str, "str | None"]] = []
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
            save_path = candidate if candidate and _SAVE_PATH.match(candidate) else None
            if content.strip():
                found.append((content, save_path))
            index = end + len(end_tag)
        self.executable_blocks_found = bool(found)
        return found

    def load_exec_block(self, llm_text: str):
        """Extract this tool's blocks from a model answer (старый API).

        Returns:
            (list[str], str | None): payloads and the last save path, or
            (None, None) when the answer contains no block with this tag.
            Новый код использует :meth:`load_blocks`: у каждого блока свой путь.
        """
        if self.tag == "undefined":
            raise RuntimeError("Tool tag is not defined")
        llm_text = str(llm_text or "")
        start_tag = f"```{self.tag}"
        end_tag = "```"
        if start_tag not in llm_text:
            return None, None

        found = self.load_blocks(llm_text)
        if not found:
            return None, None
        return [payload for payload, _ in found], found[-1][1]

    # ── workspace writing ─────────────────────────────────────────────────
    def save_block(self, payload: str, save_path: str) -> str:
        """Write a payload into the workspace; returns the relative path."""
        target = self.resolve_path(save_path)
        os.makedirs(os.path.dirname(target) or self.work_dir, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return os.path.relpath(target, self.work_dir)
