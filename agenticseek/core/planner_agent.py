"""Planner agent: divide and conquer with the specialist agents.

Derived from AgenticSeek ``sources/agents/planner_agent.py`` (GPL-3.0, see
NOTICE.md). The plan format is upstream's:

.. code-block:: json

    {"plan": [{"agent": "Web", "id": "1", "need": [], "task": "найди цены"}]}

Each task is executed by the matching specialist; ``need`` lists the ids of
earlier tasks whose answers are handed to the agent, exactly like upstream
``make_prompt``/``agents_work_result``.

NovaMind adaptations:

* the plan is bounded (``AGENTICSEEK_MAX_PLAN``) and so is the number of
  recovery plans (``AGENTICSEEK_PLAN_UPDATES``), so a confused model cannot
  turn one request into an endless chain;
* every step emits ``plan``/``plan_progress`` events for the web UI;
* the final answer is written by a concluding model call over the collected
  results (upstream relies on the last agent's answer, which for a "search then
  save to file" plan is a file listing, not an answer);
* when the model cannot produce a valid plan, the planner answers the request
  directly and says the plan failed instead of pretending to have one;
* a sub-agent failure is recorded and reported; the planner never invents its
  output.
"""

from __future__ import annotations

import json
import os
import re

from agenticseek.core.agent import Agent, clip
from agenticseek.core.errors import AgenticSeekError, AgentStopped
from agenticseek.core.casual_agent import CasualAgent
from agenticseek.core.coder_agent import CoderAgent
from agenticseek.core.file_agent import FileAgent
from agenticseek.core.prompt_paths import prompt_path

MAX_PLAN_TASKS = max(1, min(int(os.getenv("AGENTICSEEK_MAX_PLAN", "6")), 12))
MAX_PLAN_UPDATES = max(0, min(int(os.getenv("AGENTICSEEK_PLAN_UPDATES", "2")), 6))
PLAN_ATTEMPTS = max(1, min(int(os.getenv("AGENTICSEEK_PLAN_ATTEMPTS", "3")), 6))

_ALIASES = {
    "coder": "coder", "code": "coder", "coding": "coder", "programmer": "coder",
    "file": "file", "files": "file", "finder": "file",
    "web": "web", "browser": "web", "search": "web",
    "casual": "casual", "talk": "casual", "chat": "casual",
}


class PlannerAgent(Agent):
    type = "planner_agent"
    role = "planification"

    def __init__(self, name, planner_prompt, provider, verbose=False, browser=None,
                 web_agent_factory=None):
        # ``planner_prompt`` is the planner prompt; the specialists load their own
        # files through ``prompt_path`` so the names stay in one place.
        super().__init__(name, planner_prompt, provider, verbose, browser)
        self.agents = {
            "coder": CoderAgent(name, prompt_path("coder_agent"), provider),
            "file": FileAgent(name, prompt_path("file_agent"), provider),
            "casual": CasualAgent(name, prompt_path("casual_agent"), provider),
        }
        if web_agent_factory is not None:
            self.agents["web"] = web_agent_factory(name, prompt_path("browser_agent"), provider)
        self.shared_tools: dict = {}
        self.tasks: list[dict] = []
        self.max_plan_tasks = MAX_PLAN_TASKS
        self.plan_updates_left = MAX_PLAN_UPDATES

    # ── wiring ────────────────────────────────────────────────────────────
    def children(self) -> list:
        return list(self.agents.values())

    def add_shared_tools(self, tools: dict) -> None:
        """Give every specialist the NovaMind tools (search, MCP, media, memory…)."""
        self.shared_tools = dict(tools or {})
        for agent in self.agents.values():
            for tag, tool in self.shared_tools.items():
                agent.register_tool(tag, tool)

    # ── plan parsing ──────────────────────────────────────────────────────
    def parse_plan(self, text: str) -> list[dict]:
        """Extract and validate the plan from a model answer."""
        raw = str(text or "")
        candidates = re.findall(r"```json\s*(.*?)```", raw, re.S | re.I)
        if not candidates:
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                candidates = [raw[start:end + 1]]
        for candidate in candidates:
            try:
                data = json.loads(candidate.strip())
            except ValueError:
                continue
            items = data.get("plan") if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            plan = []
            for index, item in enumerate(items, 1):
                if not isinstance(item, dict):
                    continue
                task = str(item.get("task") or item.get("text") or "").strip()
                agent = _ALIASES.get(str(item.get("agent") or "").strip().lower())
                if not task or not agent:
                    continue
                needs = item.get("need") or []
                if isinstance(needs, (str, int)):
                    needs = [needs]
                plan.append({
                    "id": str(item.get("id") or index),
                    "agent": agent,
                    "task": task[:1200],
                    "need": [str(need) for need in needs if str(need).strip()],
                })
                if len(plan) >= self.max_plan_tasks:
                    break
            if plan:
                return plan
        return []

    def emit_plan(self, plan: list[dict], done: int = 0) -> None:
        text = "\n".join(f"{index}. [{item['agent']}] {item['task']}"
                         for index, item in enumerate(plan, 1))
        self.tasks = plan
        self.emit({"type": "plan", "text": clip(text, 1200), "steps": plan,
                   "done": done, "total": len(plan), "engine": "agenticseek"})

    # ── planning ──────────────────────────────────────────────────────────
    def make_plan(self, goal: str) -> list[dict]:
        """Ask the model for a plan (upstream ``make_plan`` with its retries)."""
        prompt = goal
        for attempt in range(1, PLAN_ATTEMPTS + 1):
            self.check_limits()
            self.memory.push("user", prompt)
            answer = self.llm_request()
            self.memory.push("assistant", answer)
            plan = self.parse_plan(answer)
            if plan:
                self.emit_plan(plan)
                self.emit({"type": "step", "icon": "🗺",
                           "text": f"План из {len(plan)} шагов"})
                return plan
            self.emit({"type": "step", "icon": "⚠️",
                       "text": f"Модель не дала план (попытка {attempt}/{PLAN_ATTEMPTS})"})
            prompt = ("Твой предыдущий ответ не содержал плана в формате ```json. "
                      "Напиши план заново: {\"plan\": [{\"agent\": \"Web\", \"id\": \"1\", "
                      "\"need\": [], \"task\": \"…\"}]} внутри блока ```json. "
                      f"Задача: {goal}")
        return []

    # ── execution ─────────────────────────────────────────────────────────
    def task_prompt(self, task: dict, results: dict) -> str:
        """Prompt for one specialist (upstream ``make_prompt``)."""
        parts = []
        needs = [need for need in task.get("need") or [] if need in results]
        if needs:
            parts.append("Результаты предыдущих шагов:")
            for need in needs:
                result = results[need]
                parts.append(f"[{result['agent']} · шаг {need}] {clip(result['answer'], 1500)}")
        else:
            parts.append("Дополнительных данных нет.")
        parts.append(f"\nТвоя задача:\n{task['task']}")
        parts.append("\nКогда закончишь, ответь без блока-инструмента: что именно сделано "
                     "и какой результат получен.")
        return "\n".join(parts)

    def process(self, goal: str, speech_module=None):
        """Plan → run each step → conclude. Returns ``(answer, summary)``."""
        plan = self.make_plan(goal)
        if not plan:
            self.emit({"type": "step", "icon": "💬",
                       "text": "План не получился — отвечаю напрямую"})
            answer = self._direct_answer(goal)
            return answer, ""

        results: dict[str, dict] = {}
        failures = 0
        for index, task in enumerate(plan, 1):
            self.check_limits()
            agent = self.agents.get(task["agent"]) or self.agents["casual"]
            self.emit({"type": "step", "icon": "▶️",
                       "text": f"Шаг {index}/{len(plan)}: {agent.label}"})
            prompt = self.task_prompt(task, results)
            try:
                answer, _feedback = agent.process(prompt, None)
            except AgentStopped:
                raise
            except AgenticSeekError as error:
                failures += 1
                answer = f"Шаг не выполнен: {error.message}"
                self.emit({"type": "step", "icon": "⚠️",
                           "text": f"Шаг {index} не выполнен: {clip(error.message, 200)}"})
            results[task["id"]] = {"agent": agent.type, "task": task["task"],
                                   "answer": str(answer or "")}
            self.emit({"type": "plan_progress", "done": index, "total": len(plan),
                       "marked": task["task"][:160]})

        answer = self.conclude(goal, results)
        summary = "\n".join(f"{key}: {clip(value['answer'], 300)}" for key, value in results.items())
        if failures:
            answer += f"\n\n(Часть шагов не удалась: {failures} из {len(plan)}.)"
        return answer, summary

    def conclude(self, goal: str, results: dict) -> str:
        """Write the user-facing answer from the collected results."""
        body = [f"Запрос пользователя:\n{goal}", "\nРезультаты шагов:"]
        for key, value in results.items():
            body.append(f"\n### Шаг {key} ({value['agent']})\n"
                        f"Задача: {value['task']}\nРезультат:\n{clip(value['answer'], 2000)}")
        system = (
            "Ты — NovaMind. Ниже результаты работы агентов по запросу пользователя. "
            "Дай итоговый ответ по-русски: что получилось, конкретные данные и где они лежат. "
            "Не пересказывай процесс, не выдумывай факты, которых нет в результатах. "
            "Если что-то не удалось — честно скажи это. Если созданы файлы или медиа, "
            "упомяни их одной фразой без ссылок.")
        try:
            self.check_limits()
            text = self.provider.respond([
                {"role": "system", "content": system},
                {"role": "user", "content": "\n".join(body)[:12000]},
            ])
            text = str(text or "").strip()
            if text:
                return text
        except AgentStopped:
            raise
        except Exception as error:  # noqa: BLE001 — fall back to the raw results
            self.logger.warning(f"conclude failed: {type(error).__name__}")
        return self._summary_answer(results)

    def _summary_answer(self, results: dict) -> str:
        parts = [f"Шаг {key} ({value['agent']}):\n{clip(value['answer'], 800)}"
                 for key, value in results.items()]
        return "Результаты шагов:\n\n" + "\n\n".join(parts)

    def _direct_answer(self, goal: str) -> str:
        """Fallback when planning failed: answer like the casual agent."""
        try:
            self.check_limits()
            text = self.provider.respond([
                {"role": "system", "content": self.prompt.strip()},
                {"role": "user", "content": f"Ответь пользователю напрямую, без плана:\n{goal}"},
            ])
            return str(text or "").strip() or "Не удалось составить план и ответить."
        except AgentStopped:
            raise
        except Exception as error:  # noqa: BLE001
            raise AgenticSeekError(f"Direct answer failed: {error}",
                                   "Модель не ответила.") from error
