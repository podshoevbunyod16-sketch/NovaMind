# NovaMind 2.1 — Architecture Hardening

Дата: 2026-11-01. База: ветка `arena/438d2e2b-novamind`, коммит 2.0
(`edb40a1`, PR #22). Проверка: `pytest tests -q` и
`node tests/frontend/smoke.mjs` на настоящем `index.html`, `app.js`, `linux.js`.

## Коротко

2.0 закрыла четыре разрыва: единый каталог инструментов, изоляция записей,
пять итоговых состояний и план по реальным шагам. 2.1 проверяет эти обещания
на **фактическом исполнении**, а не только на уровне каталога и текста ответа.
Найдено и исправлено пять остаточных дефектов:

| # | Дефект | Где проявлялся | Что сделано |
|---|---|---|---|
| 1 | Лимит MCP-поиска (3 за запуск) можно было обойти через `mcp_call`, `execute_unified` и registry-путь | агент делал 4-й поиск; регрессионный тест падал | единый `RunBudget` в `core/limits.py`; лимит проверяют все три входа; `mcp_registry` остаётся единственным сетевым выходом |
| 2 | `database._agent_owner_allowed()` открывал строки `owner=''` любому вошедшему | чужой/старый запуск можно было прочитать | legacy-строки видит только админ или maintenance-код; `list_agent_runs()` фильтрует так же; новый запуск всегда получает владельца |
| 3 | `task_requires_user_action` не отображался, а падал в «Задача выполнена» | пользователь видел зелёный итог при отказе инструмента | добавлен terminal event и пятое состояние в UI; `partially_completed` больше не выдаётся за полный успех |
| 4 | `plan_updated`/`plan_revised` двигали «следующий pending-шаг» вслепую | галочка появлялась у шага, который не выполнялся | статус меняет только наблюдение того инструмента, который относится к шагу; лишние шаги закрываются как `skipped`, а не `completed` |
| 5 | Офлайн-терминал, форсированный поиск, чтение страниц и медиа звали `ctx.search`/`ctx.runner`/`ctx.media_generate` напрямую | обход registry, guardrails, capability-токена и отмены | все четыре пути идут через `RuntimeToolRegistry.execute()` (прямой fallback помечается `via="direct"` для юнит-тестов) |

## 1. Один путь исполнения

`RuntimeToolRegistry` теперь является шлюзом не только для выбора инструмента,
но и для вызова:

* `execute()` проверяет отмену, разрешает адаптер, валидирует структурированные
  аргументы, применяет guardrails и capability-токен; `already_guarded` — только
  историческая подсказка, шлюз всё равно перепроверяет вызов;
* вызов ограничен таймаутом спецификации; ошибки нормализованы;
* `execute_legacy()` — совместимый алиас, а не второй путь;
* `mcp:<server>:<tool>` и `skill:<id>` идут через `execute_unified()`;
* Agent Core, native tool calls, `PlannerAgent`-специалисты, форсированный
  поиск, чтение страниц, офлайн-терминал и медиа используют этот шлюз.

Если `Agent` создан без внешнего registry (standalone-тест или старый
AgenticSeek-путь), он строит `RuntimeToolRegistry` из собственных адаптеров, а
не вызывает адаптер напрямую. Прямой вызов остался только как аварийный
fallback движка, когда адаптер вообще не собран; такой результат помечается
`via="direct"` и не считается полноценным registry-путём.

Артефакты (`last_results`, `last_text`, `last_result`, `last_model`,
`last_errors`) возвращаются отдельно от model-visible `result`; truncation
`_limit_result()` их не теряет. Иначе `web_open` на большой странице обрезал бы
именно тот текст, который нужен движку.

## 2. Старые и новые записи

Инвариант: **владелец строки — единственный, кто её читает; `owner=''` — это
legacy, а не «общая» запись.**

* `agent_runs.owner/verification` добавляются лениво через
  `_ensure_agent_columns()`; старые строки получают `''`, данные не
  перезаписываются.
* `get_agent_run()` и `list_agent_runs()`: обычный пользователь видит только
  `owner == current`; админ — свои и legacy; код без запроса (maintenance)
  видит всё.
* `create_agent_run(..., owner=...)` принимает владельца явно; движок передаёт
  `ctx.owner`, поэтому новый запуск не остаётся бесхозным даже в рабочем потоке.
* `get_chat_history()`, `add_message()`, `delete_chat()`, `save_trace()`,
  `get_traces()`, `clear_traces()` дополнительно проверяют `chat_allowed()`.
  Раньше защита была только в маршрутах; прямой вызов БД мог обойти её.
* MCP (`_visible`) и Skills (`_skills_dir`) уже применяли правило legacy →
  только админ; теперь `database` с ним согласован.

Миграция не удаляет и не переименовывает ничего. Проверка: тест создаёт базу
со старой схемой без `owner/verification`, запускает `init_agent_runs()` и
читает старую строку от админа (и не читает от пользователя).

## 3. Пять итоговых состояний

Закрытый словарь: `completed`, `partially_completed`, `failed`, `cancelled`,
`requires_user_action` (`core.verification.OUTCOMES`).

* `verify()` смотрит на наблюдения инструментов, а не на текст модели.
  Отказ «нет прав/не подключено/нужно подтверждение» даёт
  `requires_user_action`; ответ при частично неудачных шагах —
  `partially_completed`; ответ без инструментального подтверждения операции —
  `failed`, а не «выполнено».
* Повторный успешный вызов снимает старую ошибку того же инструмента: решает
  последнее наблюдение, а не вся история.
* `TERMINAL_EVENTS` отдаёт `task_completed` (в т.ч. partial), `task_failed`,
  `task_cancelled`, `task_requires_user_action`; в событии есть `status`,
  `outcome`, `verified`, `summary`, `verification`, `plan`.
* UI (`static/app.js`, `static/linux.js`) рисует все пять: чип
  `.turn-outcome`, `data-task-status`, итог в meta; `partially_completed` не
  получает иконку полного успеха.
* `result` содержит `task_status` и `verification`; при обрыве потока
  `recoverInterruptedRun()` берёт статус из сохранённого запуска.
* Итог сохраняется в локальной истории (`meta.task_status`, `meta.plan`) и
  восстанавливается после перезагрузки.

## 4. План соответствует действиям

`core/planning.py` — не декоративный чек-лист:

* у шага есть `status` (`pending`, `in_progress`, `completed`, `failed`,
  `skipped`), `candidate_tools`, `success_criteria`;
* `mark_tool()` меняет **один** подходящий шаг по реальному событию
  `tool_selected` / `tool_call_completed` / `tool_call_failed`;
* `tool_call_failed` ставит `failed` и `revision_reason`, успешный повтор может
  честно вернуть `completed`;
* `finalize_plan()` закрывает `verify` результатом проверки, а
  неиспользованные шаги помечает `skipped`, не `completed`;
* `PlannerAgent` отдаёт статусы в `plan`/`plan_progress`, `done` считает только
  успешные шаги, `failed` — отдельно;
* UI считает `done` по статусам (а не по номеру шага), показывает `✕`/`–` и
  «ошибок: N»; в истории сохраняется снимок плана.

## 5. Fail-closed security boundary (2.1.1)

Главный дефект PR #23: `tool_registry.call_tool()` и `Agent._guarded()` ловили
исключение из `guardrails`/`capabilities` и **продолжали выполнение**. Теперь
единственная точка решения — `tool_registry.security_gate()`:

* missing/broken `guardrails` → `guardrails_unavailable`;
* исключение внутри guardrail → `guardrail_error`;
* missing/broken `capabilities` → `capabilities_unavailable`;
* исключение или некорректный ответ `capabilities.require()` →
  `capability_error`;
* `verdict is None` или `allowed != True` → отказ.

Во всех этих случаях адаптер не вызывается, сеть MCP не трогается, а ошибка
возвращается агенту как обычный `retryable=False` отказ. Проверка применяется
в `call_tool()`, `RuntimeToolRegistry.execute()` (включая
`already_guarded=True` и `execute_legacy()`), `execute_unified()` для
`mcp:<server>:<tool>` и в `mcp_registry.call_tool()`.

Прямой `mcp_registry.call_tool()` по умолчанию требует capability `mcp_call`.
Встроенный `web_search` может выполнить свой MCP-подвызов под родительским
`web_search`-правом только внутри `mcp_registry.internal_search_call()` —
это contextvar, который выставляет доверенный код поиска, а не аргумент
вызывающего. Поэтому гостевой токен с `web_search` не может вызвать
произвольный MCP-инструмент напрямую.

Проверка теперь смотрит не только на «был успешный инструмент», но и на
критерии задачи:

| Запрос | Обязательное доказательство |
|---|---|
| создать/изменить файл | `file_write`/`file_edit` или terminal-команда записи |
| найти/исследовать | `web_search`/`web_open`/`knowledge_search`/`mcp_call` или источник |
| запустить тесты | `terminal`/`python` плюс вывод тестовой команды |
| открыть страницу | `web_open` |
| прочитать файл | `file_read`/`file_find` или `web_open` |
| медиа | реально созданный `media`-результат |
| исправление кода | изменение файла плюс запуск проверки |

Успешный вызов постороннего инструмента больше не даёт `completed`:
например, `web_search` не подтверждает «создай файл», а `file_write` — «запусти
тесты».

## Проверка

```bash
.venv/bin/python -m pytest tests -q
# 989 passed

node tests/frontend/smoke.mjs http://127.0.0.1:5000
# все проверки, включая блок 22: пять итогов + статусы плана
```

Новые регрессионные тесты: `tests/test_novamind21_hardening.py`
(registry-шлюз, shared MCP budget, офлайн-терминал, миграция legacy-запусков,
изоляция чатов, пять состояний, plan↔tool). Фронтенд-смоук дополнен
синтетическими потоками для всех пяти состояний и плана с `failed`/`skipped`.

## Что осталось за рамками и почему

* **Таймаут адаптера** возвращает ошибку, но не убивает уже запущенный поток
  Python. Это ограничение in-process registry; команды песочницы имеют свой
  таймаут и SIGKILL, а произвольный Python-адаптер — нет.
* **Capability-секрет** по умолчанию живёт в процессе; после перезапуска
  выданные токены недействительны (это осознанно).
* **Прямой CLI-вызов** `novamind_cli mcp call` не относится к agent-run и не
  расходует его search budget; лимит применяется к ходам агента и registry.
* **Capability без активного токена** (`capabilities.require()` вне хода
  агента) по-прежнему разрешает вызов: так работают CLI, тесты и старые
  маршруты, где авторизация проверяется на уровне Flask. В агенте токен
  активируется до запуска инструментов; отсутствие/поломка модуля при этом
  блокируется fail-closed проверкой выше.
* **Схема аргументов MCP** проверяется в `execute_unified()`; прямой
  `mcp_registry.call_tool()` не делает повторный `tools/list` только ради
  валидации (это был бы лишний сетевой round-trip).
* **История плана** сохраняется в локальном хранилище браузера и в
  `agent_runs.plan`; серверная панель «Что делал агент» по-прежнему показывает
  шаги/трассы, а не отдельный план-виджет.
