# Сторонний код и лицензии

## AgenticSeek (GPL-3.0)

Пакет `agenticseek/` — порт архитектуры проекта **AgenticSeek**:

* проект: <https://github.com/Fosowl/agenticSeek>
* автор: Martin Legrand (Fosowl) и участники проекта
* лицензия: **GNU General Public License v3.0** (полный текст — `agenticseek/LICENSE`)
* основа для порта: ветка `main`, коммит `68aa632b`, снимок от 2026-10-22

### Какие файлы являются производными от AgenticSeek

В шапке каждого такого файла есть строка «Derived from / Adapted from / Vendored
from AgenticSeek … (GPL-3.0, see NOTICE.md)». Список:

| Файл NovaMind | Источник в AgenticSeek |
|---|---|
| `agenticseek/core/agent.py` | `sources/agents/agent.py` |
| `agenticseek/core/browser.py` | `sources/browser.py` |
| `agenticseek/core/browser_agent.py` | `sources/agents/browser_agent.py` |
| `agenticseek/core/casual_agent.py` | `sources/agents/casual_agent.py` |
| `agenticseek/core/coder_agent.py` | `sources/agents/coder_agent.py` |
| `agenticseek/core/file_agent.py` | `sources/agents/file_agent.py` |
| `agenticseek/core/planner_agent.py` | `sources/agents/planner_agent.py` |
| `agenticseek/core/errors.py` | `sources/errors.py` |
| `agenticseek/core/logger.py` | `sources/logger.py` |
| `agenticseek/core/memory.py` | `sources/memory.py` |
| `agenticseek/core/utility.py` | `sources/utility.py` |
| `agenticseek/core/workspace.py` | `sources/workspace.py` |
| `agenticseek/router.py` | `sources/router.py` |
| `agenticseek/tools/tools.py` | `sources/tools/tools.py` |
| `agenticseek/tools/bash_interpreter.py` | `sources/tools/bash_interpreter.py` |
| `agenticseek/tools/py_interpreter.py` | `sources/tools/py_interpreter.py` |
| `agenticseek/tools/file_finder.py` | `sources/tools/file_finder.py` |
| `agenticseek/tools/safety.py` | `sources/tools/safety.py` (vendored без изменений) |
| `agenticseek/providers/nova_provider.py` | `sources/llm_provider.py` (форма интерфейса) |
| `agenticseek/prompts/base/*.txt` | `prompts/base/*.txt` (перевод и адаптация) |

Часть этих файлов переписана под NovaMind: локальная модель вместо облачных
провайдеров, блок-протокол вместо нативного tool calling, русские промпты,
жёсткая песочница. Изменения перечислены в шапке каждого файла и в
`docs/AGENTICSEEK_INTEGRATION.md`.

### Что написано для NovaMind и не является производным

`agenticseek/engine.py`, `agenticseek/state.py`, `agenticseek/policy.py`,
`agenticseek/offline.py`, `agenticseek/tools/nova_tools.py`,
`agenticseek/tools/project_tools.py`, `agenticseek/tools/sandbox.py`,
`agenticseek/core/project_agent.py`, `agenticseek/core/prompt_paths.py`,
`routes/agent.py`, `routes/coding_agent.py`, а также таблицы
`agent_runs`/`agent_run_steps` в `database.py` и весь код вне `agenticseek/`.

### Что это значит для распространения

GPL-3.0 — копилефт-лицензия. Пакет `agenticseek/` импортируется
непосредственно в процесс NovaMind и вызывает его код, поэтому **при
распространении собранного приложения (или его образа, архива, установщика)
на весь продукт распространяются требования GPLv3**: исходники должны быть
доступны получателю на условиях GPLv3, а производные файлы не могут быть
перелицензированы под более строгую лицензию. Полный текст лицензии —
`agenticseek/LICENSE`. Внутреннее использование и модификация без
распространения обязательств не создают.

Если проект должен остаться на несовместимой с GPL лицензии, есть два пути:
заменить `agenticseek/` на собственную реализацию (интерфейсы и контракты
описаны в `docs/AGENTICSEEK_INTEGRATION.md`, тесты — в `tests/`) либо
вынести слой в отдельный процесс/сервис, распространяемый отдельно под GPLv3.

## Прочие зависимости

Все остальные сторонние библиотеки подключаются как внешние пакеты
(`requirements.txt`) и не поставляются в этом репозитории; их лицензии
действуют на сами пакеты.
