"""
routes/agent.py — главный чат: ИИ-агент, который сам выбирает, что делать.

Отдельных режимов и кнопок нет: каждое сообщение в главном чате идёт сюда,
и модель сама решает, как отвечать.

    вопрос → [ответ сразу]                                          простые вопросы
    вопрос → план → [команда | файл | поиск | страница |
                     картинка | озвучка | видео] × N → ответ          задачи

Инструменты:
  * веб: search / open — всегда;
  * медиа: image / audio / video / media_models — всегда; модель генерации
    выбирает сам агент (media_agent.py), бесплатные — первыми;
  * Linux: run / read / write / ls — при TERMINAL_ENABLED=1 и входе в аккаунт.

Как это устроено:
  * каждый шаг — потоковый запрос к модели. Если модель начинает писать
    обычный текст, он сразу печатается в чат (одним запросом, без задержек).
    Если модель отвечает JSON-объектом — это действие в Linux-окружении:
    сервер выполняет его, отдаёт результат модели, и цикл повторяется,
    пока цель не достигнута;
  * кнопка «Поиск» (автопоиск) тоже работает через окружение: сначала
    выполняется `nova search`, читаются лучшие страницы, выдержки
    сохраняются в notes/research/, а дальше модель может искать ещё,
    открывать страницы и считать в python — и отвечает со ссылками [1], [2];
  * история диалога берётся из базы, ответ сохраняется туда же.

Границы (это сервер, а не песочница ОС):
  * AGENT_ENABLED=0 выключает агента целиком (чат уходит в /send_stream);
  * TERMINAL_ENABLED=1 в .env, иначе агент работает без Linux-команд;
  * AGENT_MAX_STEPS действий и общий AGENT_TIMEOUT — цикл не крутится вечно;
  * команды проверяются белым списком routes/terminal.py.
"""
from flask import Blueprint, Response, request, jsonify, session, stream_with_context
import json
import os
import re
import time

<<<<<<< HEAD
import media_agent
from ai_providers import chat_completion, chat_stream, resolve_target
=======
from ai_providers import chat_completion, chat_stream, resolve_target  # noqa: F401 (chat_stream — для тестов)
from tool_registry import bootstrap_default_tools, call_tool, tool_schemas
>>>>>>> origin/agent-system-audit-fix-2026-10
from routes.terminal import (run_agent as run_terminal, safe_path, relative_to_workspace,
                             ensure_workspace, nova_search, nova_read, _save_research,
                             can_use_workspace, signed_in)

agent_bp = Blueprint("agent", __name__)
_AGENT_HISTORY_CACHE = {}

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))
AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "240"))
STEP_TIMEOUT = float(os.getenv("AGENT_STEP_TIMEOUT", "30"))
STREAM_TIMEOUT = float(os.getenv("AGENT_STREAM_TIMEOUT", "120"))
SEARCH_PAGES = int(os.getenv("AGENT_SEARCH_PAGES", os.getenv("SEARCH_MAX_PAGES", "3")))
MAX_OBSERVATION = 3500
HISTORY_LIMIT = 16

LINUX_ACTIONS = {"run", "read", "write", "ls"}
WEB_ACTIONS = {"search", "open"}
MEDIA_ACTIONS = {"image", "audio", "video", "media_models"}
TOOL_ACTIONS = {"plan", "task", "task_done"} | LINUX_ACTIONS | WEB_ACTIONS | MEDIA_ACTIONS
ALL_ACTIONS = TOOL_ACTIONS | {"answer"}

AGENT_SYSTEM_PROMPT = """{persona}
Сегодня {date}.

Ты — ИИ-агент. Ты сам решаешь, что сделать для пользователя: ответить, поискать в интернете,
{linux_line}нарисовать картинку, озвучить текст или сделать видео. Пользователь пишет с телефона —
отвечай по делу, без воды.

КАК ОТВЕЧАТЬ
• Если можно ответить сразу (приветствие, объяснение, перевод, знания, совет) —
  просто ответь обычным текстом в markdown. Без JSON.
• Если для цели нужно действовать — работай ПО ШАГАМ, пока цель не достигнута.
  Каждый шаг — ТОЛЬКО один JSON-объект, без слов вокруг:

  {{"action":"plan","steps":["шаг 1","шаг 2"]}}              план для сложной задачи (первым шагом)
  {{"action":"search","query":"что найти"}}                    поиск в интернете
  {{"action":"open","url":"https://…"}}                        прочитать страницу
  {{"action":"image","prompt":"подробное описание на английском","aspect":"1:1"}}  нарисовать изображение
  {{"action":"audio","text":"что озвучить","voice":""}}        озвучить текст (речь, аудио)
  {{"action":"video","prompt":"описание сцены на английском","duration":5,"aspect":"16:9"}}  сделать видео
  {{"action":"media_models","type":"image"}}                   какие модели генерации доступны
{linux_actions}  {{"action":"task","title":"…","detail":"…"}}                 завести задачу (для длинной работы)
  {{"action":"task_done","note":"что сделано"}}                закрыть задачу

МЕДИА
• Просят картинку, рисунок, логотип, фото, арт — image. Озвучку, голос, аудио, прочитать
  вслух — audio. Ролик, анимацию, видео — video. Не спрашивай, какой моделью: модель
  выбирается сама (лучшая бесплатная), а если провайдер откажет — берётся следующая.
• Промпт для image/video пиши подробно и по-английски: объект, стиль, свет, ракурс, фон.
  aspect: 1:1, 16:9, 9:16, 4:3, 3:4. Можно указать "model", если пользователь назвал модель
  или нужен особый стиль — список даст media_models.
• Готовый файл сразу показывается пользователю в чате. В финальном ответе НЕ вставляй
  ссылку и markdown-картинку — коротко скажи, что получилось, и предложи варианты.
• Не получилось — честно скажи почему и что подключить (это будет в результате действия).

ПРАВИЛА
• После каждого действия придёт его результат. Смотри на него и решай следующий шаг.
  Ошибка — исправь и попробуй снова.
• Никогда не выдумывай результат команды, страницы или генерации — сначала выполни действие.
• Когда цель достигнута — напиши финальный ответ обычным текстом (markdown):
  что сделано и какой результат.{linux_rules}"""

LINUX_LINE = "поработать в своём Linux-окружении (код, файлы, терминал), "
LINUX_ACTION_LINES = """  {"action":"write","path":"calc.py","content":"print(2**10)"}  записать файл
  {"action":"run","command":"python3 calc.py","thought":"проверю расчёт"}  выполнить команду
  {"action":"read","path":"data.csv"}                        прочитать файл
  {"action":"ls"}                                            список файлов
"""
LINUX_RULES = """

LINUX-ОКРУЖЕНИЕ
Рабочая папка с терминалом, файлами, python3, node, git и интернетом. Посчитать, запустить
или проверить код, создать или прочитать файлы — делай это в окружении, а не в уме.
Код, который пользователю пригодится, покажи в ответе.
Одна команда за раз, без |, >, &&, $(), sudo. Сложную логику пиши в файл (write)
и запускай (run: python3 файл.py). Разрешены: ls, cat, grep, find, head, tail, wc,
mkdir, cp, mv, rm, python3, pip, node, npm, git, curl, sqlite3, date и похожие.
Команда `nova search <запрос>` и `nova read <url>` тоже работают в терминале."""
NO_LINUX_RULES = """

Linux-окружение сейчас выключено: команды run/read/write/ls недоступны. Считай и пиши код
в ответе сам и предупреди, что код не запускался."""


def build_system_prompt(use_tools, search_mode=False, date=None):
    """Системный промпт агента: веб и медиа — всегда, Linux — если включён."""
    prompt = AGENT_SYSTEM_PROMPT.format(
        persona=_persona(), date=date or time.strftime("%Y-%m-%d"),
        linux_line=LINUX_LINE if use_tools else "",
        linux_actions=LINUX_ACTION_LINES if use_tools else "",
        linux_rules=LINUX_RULES if use_tools else NO_LINUX_RULES,
    )
    if search_mode:
        prompt += SEARCH_ADDON
    return prompt


SEARCH_ADDON = """

ПОИСК ВКЛЮЧЁН
Пользователь нажал «Поиск»: источники уже найдены и лучшие страницы прочитаны —
результат лежит в истории выше. Если данных мало или они противоречат друг другу — сделай
ещё search или open. Цифры можно пересчитать в python. В финальном ответе опирайся на
найденное, ссылайся на источники как [1], [2] (номера из выдачи) и прямо говори, чего
в источниках нет."""

def is_enabled():
    """Агент включён по умолчанию; AGENT_ENABLED=0 возвращает старый чат."""
    return os.getenv("AGENT_ENABLED", "1") != "0"


def tools_enabled():
    """Инструменты терминала и файлов — любому вошедшему при включённом окружении.

    Флаг читаем в момент вызова, а не на импорте: .env может подгрузиться позже.
    """
    return os.getenv("TERMINAL_ENABLED", "0") == "1" and can_use_workspace()


_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_THINK_TAIL = re.compile(r"^.*?</think(?:ing)?>", re.S | re.I)
_THINK_TAG = re.compile(r"</?think(?:ing)?>", re.I)
_THINK_OPEN = re.compile(r"<think(?:ing)?>", re.I)
_THINK_CLOSE = re.compile(r"</think(?:ing)?>", re.I)


def strip_think(text):
    """Убирает «рассуждения» <think>…</think>: nemotron, deepseek и др. кладут их прямо в ответ."""
    text = _THINK_BLOCK.sub("", text or "")
    text = _THINK_TAIL.sub("", text)          # закрывающий тег без открывающего
    return _THINK_TAG.sub("", text).strip()


class ThinkFilter:
    """Потоковый разбор: текст внутри <think>…</think> уходит в рассуждения, а не в ответ.

    feed(chunk) → (видимый текст, рассуждение). Хвост, похожий на начало тега,
    придерживается до следующего кусочка, чтобы тег не разрезало пополам.
    """
    HOLD = len("</thinking>")

    def __init__(self):
        self.pending = ""
        self.inside = False

    def feed(self, chunk):
        self.pending += chunk or ""
        visible, thought = "", ""
        while self.pending:
            if self.inside:
                match = _THINK_CLOSE.search(self.pending)
                if match:
                    thought += self.pending[:match.start()]
                    self.pending = self.pending[match.end():]
                    self.inside = False
                    continue
                cut = max(0, len(self.pending) - self.HOLD)
                thought += self.pending[:cut]
                self.pending = self.pending[cut:]
                break
            match = _THINK_OPEN.search(self.pending)
            if match:
                visible += self.pending[:match.start()]
                self.pending = self.pending[match.end():]
                self.inside = True
                continue
            tail = self.pending.rfind("<")
            if tail >= 0 and len(self.pending) - tail < self.HOLD and \
                    "<thinking>".startswith(self.pending[tail:].lower()):
                visible += self.pending[:tail]
                self.pending = self.pending[tail:]
            else:
                visible += self.pending
                self.pending = ""
            break
        return visible, thought

    def flush(self):
        rest, self.pending = self.pending, ""
        return ("", rest) if self.inside else (rest, "")


def _extract_json_object(text):
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if quoted:
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == '"': quoted = False
            continue
        if ch == '"': quoted = True
        elif ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: return text[start:i + 1]
    return None

def parse_action(raw):
    text = strip_think(raw)
<<<<<<< HEAD
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    block = fence.group(1) if fence else None
    if not block:
        start, end = text.find("{"), text.rfind("}")
        block = text[start:end + 1] if 0 <= start < end else None
    if not block:
        return None
    try:
        data = json.loads(block)
    except (TypeError, ValueError):
        # модели любят сырые переводы строк внутри "content" — пробуем мягкий разбор
        try:
            data = json.loads(block, strict=False)
        except (TypeError, ValueError):
            return None
    return data if isinstance(data, dict) and data.get("action") else None
=======
    if not text: return None
    candidates = []
    fence = re.search(r"\`\`\`(?:json)?\s*(.*?)\s*\`\`\`", text, re.S | re.I)
    if fence: candidates.append(fence.group(1))
    obj = _extract_json_object(text)
    if obj: candidates.append(obj)
    candidates.append(text)
    for block in candidates:
        try:
            data = json.loads(block.strip(), strict=False)
            if isinstance(data, dict) and data.get("action"): return data
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?im)^ACTION\s*:\s*(SEARCH|OPEN|SHELL|READ|WRITE|ANSWER)\s*(.*)$", text)
    if match:
        action, rest = match.group(1).lower(), match.group(2).strip()
        if action == "search": return {"action":"search","query":rest}
        if action == "open": return {"action":"open","url":rest}
        if action == "shell": return {"action":"run","command":rest}
        if action == "read": return {"action":"read","path":rest}
        if action == "write":
            try: return {"action":"write", **json.loads(rest)}
            except ValueError: return None
        return {"action":"answer","text":rest}
    return None
>>>>>>> origin/agent-system-audit-fix-2026-10


def looks_like_action(text):
    """Похоже на JSON действия, но не разобралось (обрезан, кривые кавычки)."""
    text = strip_think(text).lstrip()
    return text.startswith(("{", "```")) and '"action"' in text


def _action_key(action, kind):
    """Ключ действия, чтобы не выполнять одно и то же дважды за ответ."""
    value = (action.get("command") or action.get("query") or action.get("url")
             or action.get("path") or action.get("prompt") or action.get("text") or "")
    if kind == "write":
        value = f"{value}|{str(action.get('content') or '')[:200]}"
    return kind, " ".join(str(value).lower().split())


def _action_share(text):
    """Какую долю ответа занимает JSON-объект (чтобы не принять пример из ответа за действие)."""
    start, end = text.find("{"), text.rfind("}")
    if not (0 <= start < end):
        return 0.0
    return (end - start + 1) / max(1, len(text.strip()))


def clip(text, limit=MAX_OBSERVATION):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n… обрезано"


<<<<<<< HEAD
def _decide(buffer):
    """По началу ответа понять: это действие (JSON) или обычный текст.

    True — действие, False — текст, None — пока непонятно, ждём ещё токенов.
    """
    text = buffer.lstrip()
    if not text:
        return None
    if text[0] == "{":
        return True
    if not text.startswith("`"):
        return False
    if len(text) < 3:
        return None
    if not text.startswith("```"):
        return False
    if "\n" not in text:
        return None if len(text) < 24 else False
    head, rest = text.split("\n", 1)
    head = head.strip().lower()
    if head == "```json":
        return True
    if head != "```":
        return False            # ```python и т.п. — это ответ с кодом
    rest = rest.lstrip()
    if not rest:
        return None
    return rest[0] == "{"


def _persona():
    try:
        import config
        return config.system_prompt
    except Exception:          # pragma: no cover - config всегда есть в приложении
        return "Ты — NovaMind, умный AI-ассистент."
=======
def _ask(messages, **kwargs):
    """Запрашивает модель через поток; fallback на обычный completion."""
    try:
        chunks = []
        error_text = ""
        saw = False
        for kind, chunk in chat_stream(messages, **kwargs):
            saw = True
            if kind == "token":
                chunks.append(str(chunk or ""))
            elif kind == "error":
                error_text = str(chunk or "")
        text = strip_think("".join(chunks))
        if text:
            offline_fallback = (
                "встроенная **офлайн-модель" in text.lower()
                or "ассистент novamind. спросите что-нибудь" in text.lower()
                or "проверьте модель и настройки" in text.lower()
            )
            if not offline_fallback:
                return text, ""
    except Exception:
        pass
    text, meta = chat_completion(messages, **kwargs)
    text = strip_think(text)
    if text:
        return text, ""
    return "", str(meta) if isinstance(meta, str) and meta else "модель не ответила"

# ───────────────────────── источники и заметки ─────────────────────────

_RESULT_LINE = re.compile(r"^\s*\d+\.\s+(.+)\n\s+(https?://\S+)", re.M)


def _add_sources(state, items):
    """Копит уникальные источники (title, url, host) для сворачиваемого блока."""
    seen = {item["url"] for item in state["sources"]}
    for item in items or []:
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        state["sources"].append({
            "title": str(item.get("title") or "")[:220],
            "url": url,
            "host": item.get("host") or urlparse(url).netloc,
        })
    del state["sources"][MAX_SOURCES:]


def _sources_from_text(text):
    """Источники из текстовой выдачи `nova search` (когда модель вызвала её командой run)."""
    return [{"title": title.strip(), "url": url} for title, url in _RESULT_LINE.findall(text or "")]


def _note(state, label, text):
    """Запоминает находку — из этих заметок собирается финальный ответ."""
    text = (text or "").strip()
    if text:
        state["notes"].append((label, clip(text, MAX_NOTE)))


def _is_research_command(command):
    parts = str(command or "").split()
    return len(parts) >= 2 and parts[0] == "nova" and parts[1] in ("search", "read")


# ───────────────────────── итоговый ответ ─────────────────────────

def _synthesize(goal, state, provider, model):
    """Финальный запрос: модель отвечает обычным текстом по собранным данным."""
    notes = "\n\n".join(f"[{label}]\n{text}" for label, text in state["notes"][-6:])
    prompt = (
        f"Вопрос пользователя: {goal}\n\n"
        f"Найденные данные:\n{notes or 'ничего не найдено'}\n\n"
        "Ответь пользователю по-русски прямо и по существу, опираясь на эти данные: сначала суть "
        "(названия, цифры, годы), затем 1–3 предложения пояснения. Если данные неполные или "
        "противоречивые — скажи об этом честно. Не пиши JSON, не пересказывай свои шаги, "
        "не показывай команды и не вставляй ссылки."
    )
    text, _error = _ask(
        [{"role": "system", "content": "Ты — NovaMind, ассистент. Отвечай точно и по делу."},
         {"role": "user", "content": prompt}],
        provider=provider, model=model, temperature=0.3, max_tokens=1200, timeout=60,
    )
    action = parse_action(text)
    if action:                                   # модель всё равно ответила JSON-ом
        text = str(action.get("text") or "").strip() if action.get("action") == "answer" else ""
    return text


def _fallback_answer(state):
    """Модель молчит — отдаём хотя бы найденное, а не пустоту."""
    if state["notes"]:
        label, text = state["notes"][0]
        return ("Модель не смогла сформулировать ответ, но данные собраны. "
                f"Вот что нашлось ({label}):\n\n{clip(text, 1500)}\n\n"
                "Источники — в свёрнутом блоке ниже; попробуйте ещё раз или смените модель.")
    return "Не удалось получить ответ. Проверьте модель и настройки."


@agent_bp.route("/api/agent/status")
def agent_status():
    enabled = is_enabled()
    return jsonify({
        "enabled": enabled,
        "tools": tools_enabled(),
        "available": bool(is_enabled() and tools_enabled()),
        "max_steps": MAX_STEPS,
        "user": signed_in(),
        "admin": bool(session.get("admin_logged_in")),
        "hint": ("" if enabled
                 else "Включите AGENT_ENABLED=1 и TERMINAL_ENABLED=1 в .env и перезапустите сервер"),
    })


@agent_bp.route("/api/agent/stream", methods=["POST"])
def agent_stream():
    """Агентный цикл: план → инструменты → ответ. NDJSON."""
    data = request.get_json(silent=True) or {}
    goal = (data.get("message") or "").strip()
    if not goal:
        return jsonify({"error": "Пустое сообщение"}), 400
    if not is_enabled():
        return jsonify({"error": "Агентный режим выключен (AGENT_ENABLED=1)"}), 403
    if not signed_in():
        return jsonify({"error": "Войдите в аккаунт, чтобы включить агентный режим"}), 403

    provider, model, _ = resolve_target()
    use_tools = tools_enabled()
    history_key = (
        str(session.get("user_email") or session.get("email") or session.get("username") or "")
        or request.cookies.get("session")
        or "anonymous"
    )
    prior = _AGENT_HISTORY_CACHE.get(history_key, session.get("agent_history", []))
    if not isinstance(prior, list):
        prior = []
    if use_tools:
        bootstrap_default_tools()
        ensure_workspace()

    @stream_with_context
    def generate():
        started = time.time()
        state = {"task_id": None, "steps": 0, "answer": "", "sources": [], "notes": [],
                 "research": 0, "searched": bool(data.get("search"))}

        # 1. Один первый запрос: простой вопрос получает обычный ответ без лишнего plan/stage.
        # Если модель вернула action=plan, показываем его как план и продолжаем цикл.
        history = [{"role": "system", "content": AGENT_SYSTEM_PROMPT + "\n\nAVAILABLE TOOL REGISTRY:\n" + json.dumps(tool_schemas(), ensure_ascii=False) if use_tools else AGENT_SYSTEM_PROMPT}]
        for item in prior[-8:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                history.append({"role": item["role"], "content": str(item.get("content") or "")[:4000]})
        history.append({"role": "user", "content": f"Задача: {goal}"})

        if data.get("search") and use_tools:
            yield _ndjson({"type": "stage", "scene": "search", "title": "Поиск в интернете",
                           "text": "Ищу свежие данные и проверяю страницы…"})
            pre = call_tool("web_search", query=goal, limit=3)
            pre_results = pre.get("results") or []
            pre_text = "\n".join(f"{i}. {x.get('title','')}\n   {x.get('url','')}\n   {x.get('snippet','')}"
                                  for i, x in enumerate(pre_results, 1))
            if pre_results:
                state["research"] += 1
                _add_sources(state, pre_results)
                _note(state, f"поиск «{goal}»", pre_text)
                yield _ndjson({"type": "tool", "name": "search", "command": f"nova search {goal}",
                               "code": 0, "output": pre_text, "results": pre_results})
                try:
                    body = f"# {goal}\n\n" + pre_text + "\n"
                    saved_path = _save_research(goal, body, "search")
                    yield _ndjson({"type": "step", "icon": "💾",
                                   "text": f"Сохранил результаты поиска: {saved_path}"})
                except Exception:
                    pass
                for item in pre_results:
                    opened = call_tool("web_open", url=item.get("url"), max_chars=3000)
                    page = opened.get("content") or ""
                    if page:
                        page = "Текст страницы:\n" + page if not page.startswith("Текст страницы") else page
                        _note(state, f"страница {item.get('url')}", page)
                        yield _ndjson({"type": "tool", "name": "open", "command": item.get("url"),
                                       "code": 0, "output": clip(page, 1500)})
            else:
                yield _ndjson({"type": "step", "icon": "⚠️", "text": "Поиск ничего не дал"})
            observation_parts = [pre_text] if pre_text else ["Поиск ничего не нашёл."]
            for item in pre_results:
                opened = call_tool("web_open", url=item.get("url"), max_chars=3000)
                page = opened.get("content") or ""
                if page:
                    page = "Текст страницы:\n" + page if not page.startswith("Текст страницы") else page
                    observation_parts.append(page[:1800])
            history.append({"role": "user", "content":
                            "ПОИСК ВКЛЮЧЁН. Используй найденные данные. Если их достаточно — ответь; "
                            "если нет — можешь выполнить ещё один search.\n\n" + "\n\n".join(observation_parts)})

        # 2. Цикл действий
        seen, duplicates, parse_fails = set(), 0, 0
        for step in range(1, MAX_STEPS + 1):
            if time.time() - started > AGENT_TIMEOUT:
                yield _ndjson({"type": "step", "icon": "⏱", "text": "Время вышло — собираю ответ"})
                break
            if not use_tools:
                break

            state["steps"] += 1
            ask_kwargs = {"provider": provider, "model": model,
                          "temperature": 0.1, "max_tokens": 1200, "timeout": 60}
            if data.get("search"):
                ask_kwargs["system"] = "ПОИСК ВКЛЮЧЁН. Используй найденные данные и указывай источники [1], [2], [3]."
            raw, error = _ask(history, **ask_kwargs)
            if error:
                yield _ndjson({"type": "error", "text": error})
                break                                   # ответ соберём из найденного

            action = parse_action(raw)
            extracted = _extract_json_object(strip_think(raw)) if raw else None
            if action and extracted and strip_think(raw).strip() != extracted.strip():
                yield _ndjson({"type": "retract", "count": len(strip_think(raw))})
            if not action and raw and '"action"' in raw and _extract_json_object(raw):
                action = parse_action(_extract_json_object(raw))
                if action:
                    yield _ndjson({"type": "retract", "count": len(raw)})
            if not action and re.match(r"^План\s*:", strip_think(raw), re.I):
                plan_text = strip_think(raw).split(":", 1)[1].strip()
                yield _ndjson({"type": "stage", "scene": "agent", "title": "Планирую",
                               "text": "Разбираю задачу по шагам…"})
                yield _ndjson({"type": "plan", "text": clip(plan_text, 900)})
                history.append({"role": "assistant", "content": raw[:2000]})
                history.append({"role": "user", "content": "План принят. Выполни задачу по плану и продолжай до готового ответа."})
                continue
            if not action:
                if looks_like_action(raw):
                    # JSON обрезался или сломан: один раз просим повторить, потом сдаёмся
                    parse_fails += 1
                    if parse_fails >= 2:
                        break
                    history.append({"role": "assistant", "content": raw[:500]})
                    history.append({"role": "user", "content":
                                    "Это не разобралось как JSON. Повтори одним корректным JSON-объектом."})
                    continue
                state["answer"] = raw.strip()           # обычный текст — это и есть ответ
                break

            kind = str(action.get("action"))
            if kind == "plan":
                steps = action.get("steps") or action.get("plan") or action.get("text") or []
                if isinstance(steps, list):
                    plan_text = "\n".join(f"{i}. {str(item)}" for i, item in enumerate(steps, 1))
                else:
                    plan_text = str(steps)
                yield _ndjson({"type": "stage", "scene": "agent", "title": "Планирую",
                               "text": "Разбираю задачу по шагам…"})
                yield _ndjson({"type": "plan", "text": clip(plan_text, 900)})
                history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)[:2000]})
                history.append({"role": "user", "content": "План принят. Выполни первый пункт и продолжай работу."})
                continue
            key = _action_key(action, kind)
            if key in seen and kind not in ("answer", "task", "task_done"):
                duplicates += 1
                yield _ndjson({"type": "step", "icon": "↺", "text": "Повторное действие пропущено"})
                if duplicates >= 2:
                    break
                observation = ("Ты уже выполнял именно это действие — результат выше. Не повторяй его: "
                               "используй имеющиеся данные и дай action=answer.")
            else:
                seen.add(key)
                observation = yield from _perform(action, kind, use_tools, state, goal=goal)

            if kind == "answer":
                state["answer"] = str(action.get("text") or "").strip()
                break

            history.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)[:2000]})
            observation = clip(observation)
            verification = _verify_action(kind, observation)
            yield _ndjson({"type": "verify", "ok": verification["ok"], "tool": kind,
                           "text": verification["message"]})
            if not verification["ok"]:
                history.append({"role": "user", "content":
                                "VERIFY: действие не подтверждено. Не повторяй вслепую; "
                                "исправь параметры или выбери другой инструмент."})
            nudge = _nudge(state, MAX_STEPS - step)
            history.append({"role": "user", "content":
                            f"Результат действия:\n{observation}" + (f"\n\n{nudge}" if nudge else "")})

        if state["steps"] >= MAX_STEPS and not state["answer"]:
            yield _ndjson({"type": "step", "icon": "⏹", "text": "Шаги закончились — готовлю итог"})

        # 3. Нет ответа (лимит шагов, таймаут, повторы, сбой) — собираем его по найденному
        if not state["answer"] and (use_tools or state["notes"]):
            yield _ndjson({"type": "step", "icon": "✍️", "text": "Готовлю ответ по найденному"})
            state["answer"] = _synthesize(goal, state, provider, model)
        if not state["answer"] and not use_tools:
            # инструментов нет — обычный ответ модели
            text, _err = _ask(history, provider=provider, model=model,
                              temperature=0.3, max_tokens=1200, timeout=60)
            action = parse_action(text)
            state["answer"] = (str(action.get("text") or "").strip() if action else text)
        if not state["answer"]:
            state["answer"] = _fallback_answer(state)

        # Источники — отдельным событием; интерфейс держит их свёрнутыми
        if state["sources"]:
            yield _ndjson({"type": "sources", "sources": state["sources"]})

        # 4. Финальный ответ печатаем по кускам — интерфейс любит поток
        for chunk in _chunks(state["answer"]):
            yield _ndjson({"type": "token", "token": chunk})

        if state["task_id"]:
            import database
            task = database.get_task(state["task_id"])
            if task and task["status"] != "done":
                database.update_task(state["task_id"], status="done")
                database.append_task_step(state["task_id"], "Агент закончил", status="done",
                                          log=clip(state["answer"], 1500))
                yield _ndjson({"type": "task", "task": database.get_task(state["task_id"])})

        updated_history = (prior + [{"role": "user", "content": goal},
                                       {"role": "assistant", "content": state["answer"]}])[-10:]
        _AGENT_HISTORY_CACHE[history_key] = updated_history
        session["agent_history"] = updated_history
        yield _ndjson({"type": "result", "reply": state["answer"], "steps": state["steps"],
                       "task_id": state["task_id"], "model": model,
                       "sources": state["sources"], "searched": state["searched"],
                       "offline": provider == "local_demo"})
        yield _ndjson({"type": "done"})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
>>>>>>> origin/agent-system-audit-fix-2026-10


def _ndjson(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _chunks(text, size=48):
    text = text or ""
    for index in range(0, len(text), size):
        yield text[index:index + size]


def _history_for_prompt(chat_id):
    """Последние сообщения диалога — чтобы ИИ помнил контекст, как в обычном чате."""
    if not chat_id:
        return []
    try:
        from database import get_chat_history
        rows = get_chat_history(chat_id, limit=HISTORY_LIMIT)
    except Exception:
        return []
    history = []
    for row in rows:
        content = str(row.get("content") or "")
        if not content or content.startswith(("MEDIA_RESULT:", "COMPOSIO_CARDS:", "COMPOSIO_AUTH:")):
            continue
        role = "assistant" if row.get("role") in ("assistant", "ai", "model") else "user"
        history.append({"role": role, "content": clip(content, 3000)})
    return history


<<<<<<< HEAD
# ───────────────────────── маршруты ─────────────────────────
=======
def _verify_action(kind, observation):
    text = str(observation or "").strip()
    if kind in ("search", "open", "read", "run", "write", "ls") and not text:
        return {"ok": False, "message": "Проверка не пройдена: инструмент вернул пустой результат."}
    if kind == "run" and "код -1" in text:
        return {"ok": False, "message": "Проверка не пройдена: команда не выполнилась."}
    if kind == "search" and "ничего не вернул" in text.lower():
        return {"ok": False, "message": "Поиск не подтвердил наличие результатов."}
    return {"ok": True, "message": "Результат инструмента получен и принят."}


def _nudge(state, remaining):
    """Подсказка модели: пора отвечать, а не искать дальше."""
    if remaining <= 0:
        return "Шагов не осталось. Дай финальный ответ прямо сейчас: {\"action\":\"answer\",\"text\":\"…\"}."
    if remaining == 1:
        return "Остался последний шаг. Больше не ищи — ответь action=answer по собранным данным."
    if state["research"] >= 3:
        return ("Данных из интернета уже достаточно. Если можешь ответить — сразу action=answer; "
                "новый поиск — только если чего-то принципиально не хватает.")
    return ""
>>>>>>> origin/agent-system-audit-fix-2026-10

@agent_bp.route("/api/agent/status")
def agent_status():
    enabled = is_enabled()
    user = signed_in()
    tools = tools_enabled()
    hint = ""
    if not enabled:
        hint = "ИИ-агент выключен (AGENT_ENABLED=0) — чат отвечает без инструментов"
    elif not user:
        hint = "Войдите в аккаунт, чтобы ИИ мог искать, генерировать медиа и работать в Linux"
    elif os.getenv("TERMINAL_ENABLED", "0") != "1":
        hint = ("ИИ ищет в интернете и генерирует медиа. Для Linux-окружения добавьте "
                "TERMINAL_ENABLED=1 в .env и перезапустите сервер")
    return jsonify({
        "enabled": enabled,
        "tools": tools,
        "linux": tools,
        "media": {kind: mg_connected(kind) for kind in media_agent.KINDS},
        "available": enabled and user,
        "max_steps": MAX_STEPS,
        "user": user,
        "admin": bool(session.get("admin_logged_in")),
        "hint": hint,
    })


@agent_bp.route("/api/agent/stream", methods=["POST"])
def agent_stream():
    """Главный чат: ИИ отвечает сам или работает в Linux по шагам. NDJSON."""
    data = request.get_json(silent=True) or {}
    goal = (data.get("message") or "").strip()
    if not goal:
        return jsonify({"error": "Пустое сообщение"}), 400
    if not is_enabled():
        return jsonify({"error": "ИИ-агент выключен (AGENT_ENABLED=0)"}), 403
    if not signed_in():
        return jsonify({"error": "Войдите в аккаунт, чтобы ИИ мог работать с инструментами"}), 403

    search_mode = bool(data.get("search"))
    reasoning = bool(data.get("reasoning"))
    provider, model, _ = resolve_target()
    use_tools = tools_enabled()
    if use_tools:
        ensure_workspace()

    # История и сохранение — как у обычного чата (/send_stream)
    chat_id = None
    history = []
    if data.get("save_history", True):
        try:
            from database import get_or_create_session_chat, add_message
            chat_id = get_or_create_session_chat()
            history = _history_for_prompt(chat_id)
            add_message(chat_id, "user", goal)
        except Exception as exc:        # история не должна ронять ответ
            print(f"[agent] история недоступна: {exc}")
            chat_id = None

    system = build_system_prompt(use_tools, search_mode)

    @stream_with_context
    def generate():
        started = time.time()
        state = {"task_id": None, "steps": 0, "answer": "", "sources": [],
                 "searched": False, "announced": False, "media": [], "linux": False,
                 "use_tools": use_tools}
        messages = list(history) + [{"role": "user", "content": goal}]
        done_actions = set()          # что уже выполнено — повторы не гоняем

        try:
            if search_mode:
                yield from _auto_search(goal, state, messages)

            for step_no in range(MAX_STEPS + 1):
                out_of_time = time.time() - started > AGENT_TIMEOUT
                allow = step_no < MAX_STEPS and not out_of_time
                if not allow and step_no > 0:
                    note = "Время вышло" if out_of_time else "Шаги закончились"
                    yield _ndjson({"type": "step", "icon": "⏱", "text": f"{note} — собираю ответ"})
                    messages.append({"role": "user", "content":
                                     f"{note}. Больше никаких действий: напиши финальный ответ "
                                     f"пользователю обычным текстом по тому, что удалось сделать."})

                outcome = yield from _model_step(
                    messages, system, provider, model,
                    reasoning=reasoning and step_no == 0,
                    allow_action=allow,
                )
                if outcome["kind"] == "answer":
                    state["answer"] = outcome["text"]
                    break
                if outcome["kind"] == "stuck":
                    # Шаги кончились, а модель всё просит действий — отвечаем по собранному
                    state["answer"] = yield from _final_answer(messages, provider, model, state)
                    break
                if outcome["kind"] == "error":
                    yield _ndjson({"type": "error", "text": str(outcome["error"] or "модель не ответила")})
                    break
                if outcome["kind"] == "bad_json":
                    messages.append({"role": "assistant", "content": clip(outcome["text"], 1500)})
                    messages.append({"role": "user", "content":
                                     "Не удалось разобрать JSON действия. Повтори ОДНИМ корректным "
                                     "JSON-объектом или ответь пользователю обычным текстом."})
                    continue

                action = outcome["action"]
                kind = str(action.get("action"))
                key = _action_key(action, kind)
                if kind not in ("plan", "task_done") and key in done_actions:
                    yield _ndjson({"type": "step", "icon": "↩️", "text": "Это уже сделано — не повторяю"})
                    messages.append({"role": "assistant",
                                     "content": json.dumps(action, ensure_ascii=False)[:1000]})
                    messages.append({"role": "user", "content":
                                     "Это действие уже выполнено, его результат есть выше. Не повторяй его: "
                                     "сделай другой шаг или, если данных хватает, ответь пользователю."})
                    continue
                done_actions.add(key)
                if not state["announced"] and not search_mode and kind not in MEDIA_ACTIONS:
                    state["announced"] = True
                    title = "Работаю в Linux" if use_tools and kind in LINUX_ACTIONS else "Работаю над задачей"
                    yield _ndjson({"type": "stage", "scene": "agent", "title": title,
                                   "text": "Иду к цели по шагам…"})
                state["steps"] += 1
                thought = str(action.get("thought") or "").strip()
                if thought:
                    yield _ndjson({"type": "step", "icon": "💭", "text": thought[:200]})
                observation = yield from _perform(action, kind, use_tools, state, goal=goal)
                messages.append({"role": "assistant",
                                 "content": json.dumps(action, ensure_ascii=False)[:3000]})
                messages.append({"role": "user", "content":
                                 f"Результат действия:\n{clip(observation)}\n\n"
                                 f"Дальше: следующий шаг (JSON) или, если цель достигнута, "
                                 f"финальный ответ обычным текстом."})
        except GeneratorExit:
            raise
        except Exception as exc:
            yield _ndjson({"type": "error", "text": f"{exc.__class__.__name__}: {exc}"})

        if not state["answer"]:
            state["answer"] = "Не удалось получить ответ модели. Проверьте модель в настройках и повторите."
            for chunk in _chunks(state["answer"]):
                yield _ndjson({"type": "token", "token": chunk})

        if state["task_id"]:
            import database
            task = database.get_task(state["task_id"])
            if task and task["status"] != "done":
                database.update_task(state["task_id"], status="done")
                database.append_task_step(state["task_id"], "ИИ закончил", status="done",
                                          log=clip(state["answer"], 1500))
                yield _ndjson({"type": "task", "task": database.get_task(state["task_id"])})

        if chat_id:
            try:
                from database import add_message, trim_messages
                saved = state["answer"]
                if state["media"]:
                    # ИИ должен помнить, что уже нарисовал: «сделай ярче», «ещё вариант»
                    notes = "\n".join(
                        f"[{media_agent.KIND_LABELS.get(item.get('kind'), 'медиа')}: "
                        f"«{clip(item.get('prompt', ''), 300)}» — {item.get('model_name') or item.get('model')}]"
                        for item in state["media"])
                    saved = f"{saved}\n\n{notes}"
                add_message(chat_id, "assistant", saved)
                trim_messages(chat_id, max_messages=100)
            except Exception as exc:
                print(f"[agent] не удалось сохранить ответ: {exc}")

        yield _ndjson({"type": "result", "reply": state["answer"], "steps": state["steps"],
                       "task_id": state["task_id"], "model": model, "provider": provider,
                       "sources": state["sources"], "searched": state["searched"],
                       "media": state["media"], "linux": state["linux"],
                       "offline": provider == "local_demo"})
        yield _ndjson({"type": "done"})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ───────────────────────── один шаг модели ─────────────────────────

def _model_step(messages, system, provider, model, reasoning=False, allow_action=True):
    """Потоковый запрос к модели.

    Генератор событий (reasoning/token/retract). Возвращает словарь:
      {"kind": "answer", "text": …}   — модель ответила текстом (уже напечатан);
      {"kind": "action", "action": …} — модель выбрала действие в окружении;
      {"kind": "error",  "error": …}  — модель не ответила.
    """
    buffer = ""
    mode = None                   # None — ещё не решили, "action" / "text"
    failure = None
    thinker = ThinkFilter()       # <think>…</think> прямо в тексте — это рассуждения, не ответ

    def visible_tokens(chunk):
        """Пропускает кусочек через фильтр рассуждений. Генератор событий, возвращает видимый текст."""
        shown, thought = chunk
        if thought:
            yield _ndjson({"type": "reasoning", "token": thought})
        return shown

    try:
        for kind, chunk in chat_stream(messages, provider=provider, model=model, system=system,
                                       temperature=0.3, timeout=STREAM_TIMEOUT,
                                       reasoning=reasoning):
            if kind == "reasoning":
                if chunk:
                    yield _ndjson({"type": "reasoning", "token": chunk})
                continue
            if kind == "error":
                failure = chunk
                continue
            if kind == "done":
                break
            if kind != "token" or not chunk:
                continue
            chunk = yield from visible_tokens(thinker.feed(chunk))
            if not chunk:
                continue
            buffer += chunk
            if mode is None:
                decision = _decide(buffer)
                if decision is None:
                    continue
                mode = "action" if decision else "text"
                if mode == "text":
                    yield _ndjson({"type": "token", "token": buffer})
                continue
            if mode == "text":
                yield _ndjson({"type": "token", "token": chunk})
    except GeneratorExit:
        raise
    except Exception as exc:      # сеть или провайдер упали посреди потока
        failure = f"{exc.__class__.__name__}: {exc}"

    tail = yield from visible_tokens(thinker.flush())
    if tail:
        buffer += tail
        if mode == "text":
            yield _ndjson({"type": "token", "token": tail})
        elif mode is None and not _decide(buffer):
            mode = "text"
            yield _ndjson({"type": "token", "token": buffer})

    text = buffer.strip()
    if not text:
        # Поток пустой — один повтор без стрима, затем честная ошибка.
        # chat_completion: успех — (текст, meta-словарь), ошибка — (None, строка).
        raw, meta = chat_completion(messages, provider=provider, model=model, system=system,
                                    temperature=0.3, timeout=STREAM_TIMEOUT)
        text, mode = strip_think(str(raw or "")), None
        if not text:
            error = meta if isinstance(meta, str) and meta else None
            return {"kind": "error", "error": failure or error or "модель не ответила"}

    if mode == "text":
        # Текст уже напечатан. Бывает, что модель пишет фразу, а потом JSON действия —
        # тогда забираем напечатанное назад и выполняем действие.
        if allow_action:
            action = parse_action(text)
            if (action and action.get("action") in TOOL_ACTIONS
                    and _action_share(text) >= 0.6):
                yield _ndjson({"type": "retract"})
                return {"kind": "action", "action": action}
        return {"kind": "answer", "text": text}

    action = parse_action(text)
    if action and action.get("action") == "answer":
        answer = str(action.get("text") or "").strip() or text
        for chunk in _chunks(answer):
            yield _ndjson({"type": "token", "token": chunk})
        return {"kind": "answer", "text": answer}
    if action and action.get("action") in TOOL_ACTIONS:
        if allow_action:
            return {"kind": "action", "action": action}
        return {"kind": "stuck", "action": action}      # шаги кончились, а модель всё действует
    if allow_action and looks_like_action(text):
        return {"kind": "bad_json", "text": text}       # JSON обрезан или кривой — попросим повторить

    # Не разобрали JSON (или действия сейчас нельзя) — показываем как есть
    for chunk in _chunks(text):
        yield _ndjson({"type": "token", "token": chunk})
    return {"kind": "answer", "text": text}


# ───────────────────────── источники ─────────────────────────

def _add_sources(state, results):
    """Складывает найденное в общий список источников. Возвращает [(номер, источник)]."""
    numbered = []
    known = {item["url"]: index for index, item in enumerate(state["sources"], 1)}
    for item in results or []:
        url = item.get("url")
        if not url:
            continue
        if url not in known:
            state["sources"].append({
                "title": item.get("title") or item.get("host") or url,
                "url": url,
                "snippet": item.get("snippet", ""),
                "host": item.get("host", ""),
            })
            known[url] = len(state["sources"])
        numbered.append((known[url], state["sources"][known[url] - 1]))
    return numbered


def _format_numbered(query, numbered, backend=None, elapsed=0):
    lines = [f"🔎 «{query}» — найдено {len(numbered)} ({elapsed / 1000:.1f} c"
             + (f", {backend}" if backend else "") + ")"]
    for number, item in numbered:
        lines.append(f"[{number}] {item['title']}")
        lines.append(f"    {item['url']}")
        if item.get("snippet"):
            lines.append(f"    {item['snippet'][:260]}")
    return "\n".join(lines)


def _search_query(goal):
    """Поисковый запрос из сообщения: без лишнего запроса к модели — быстрее на телефоне."""
    text = " ".join((goal or "").split())
    return text[:160] or goal


def _auto_search(goal, state, messages):
    """Кнопка «Поиск»: поиск и чтение страниц через Linux-окружение.

    Всё, что найдено, видно в блоке «Работа в Linux», сохраняется в
    notes/research/ и уходит модели как результат первого шага.
    """
    from routes.search import _read_pages

    query = _search_query(goal)
    state["searched"] = True
    yield _ndjson({"type": "stage", "scene": "search", "title": "Поищу в интернете",
                   "text": "Ищу через Linux-окружение…" if state.get("use_tools", True) else "Ищу источники…"})
    results, meta = nova_search(query, limit=8)
    numbered = _add_sources(state, results)
    listing = _format_numbered(query, numbered, meta.get("backend"), meta.get("elapsed_ms", 0))
    action = {"action": "search", "query": query}

    if not numbered:
        yield _ndjson({"type": "step", "icon": "⚠️", "text": "Поиск ничего не дал — попробую по-другому"})
        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        messages.append({"role": "user", "content":
                         "Результат действия: поиск ничего не нашёл. Попробуй другой запрос "
                         "(search) или ответь из своих знаний с пометкой «(из базы знаний)»."})
        return

    yield _ndjson({"type": "step", "icon": "🔎",
                   "text": f"Нашёл {len(numbered)} источников ({meta.get('backend') or 'поиск'})"})
    yield _ndjson({"type": "tool", "name": "search", "command": f"nova search {query}",
                   "code": 0, "output": listing, "results": results})
    yield _ndjson({"type": "sources", "sources": state["sources"]})

    pages = []
    top = [item for _, item in numbered[:max(0, SEARCH_PAGES)]]
    if top:
        yield _ndjson({"type": "step", "icon": "📄", "text": f"Читаю {len(top)} страницы…"})
        pages = _read_pages(top)
        for page in pages:
            yield _ndjson({"type": "tool", "name": "open", "command": f"nova read {page['url']}",
                           "code": 0 if page["chars"] > 200 else 1,
                           "output": clip(page["text"], 400)})

    number_of = {item["url"]: number for number, item in numbered}
    context_parts = [listing, ""]
    for page in pages:
        if page.get("text"):
            context_parts.append(f"### [{number_of.get(page['url'], '?')}] {page['title']}\n"
                                 f"URL: {page['url']}\n{page['text']}")
    context = "\n\n".join(context_parts)

    if state.get("use_tools", True):
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M")
            saved = _save_research(query, f"# {query}\n\n_поиск {stamp}_\n\n{context}\n", "search")
            yield _ndjson({"type": "step", "icon": "💾", "text": f"Сохранил выдержки: {saved}"})
        except OSError as exc:
            print(f"[agent] не удалось сохранить заметку: {exc}")

    yield _ndjson({"type": "stage", "scene": "write", "title": "Разбираю найденное",
                   "text": "Решаю, хватает ли данных"})
    messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
    messages.append({"role": "user", "content":
                     f"Результат действия (поиск и чтение страниц):\n{clip(context, 9000)}\n\n"
                     f"Дальше: ещё search/open/run, если данных мало, или финальный ответ "
                     f"обычным текстом со ссылками [n]."})


# ───────────────────────── действия ─────────────────────────

def _perform(action, kind, use_tools, state, goal=""):
    """Одно действие в окружении.

    Генератор: отдаёт события потока (шаги, вывод команд, задачи) и
    возвращает наблюдение, которое уходит обратно в модель.
    """
    import database

    if kind == "plan":
        steps = action.get("steps")
        if isinstance(steps, list):
            text = "\n".join(f"{index}. {str(step).strip()}" for index, step in enumerate(steps, 1)
                             if str(step).strip())
        else:
            text = str(steps or action.get("text") or "").strip()
        if text:
            yield _ndjson({"type": "plan", "text": clip(text, 900)})
        return "План принят. Выполняй первый шаг."

    if kind == "task":
        task = database.create_task(
            title=(action.get("title") or goal or "Задача ИИ")[:200],
            detail=(action.get("detail") or goal or "")[:2000],
            source="agent",
        )
        state["task_id"] = task["id"]
        database.append_task_step(task["id"], "Задача заведена", status="doing", log=goal[:1000])
        yield _ndjson({"type": "task", "task": task})
        yield _ndjson({"type": "step", "icon": "📋", "text": f"Задача: {task['title']}"})
        return f"Задача создана, id={task['id']}. Дальше выполняй её по шагам."

    if kind == "task_done":
        if state["task_id"]:
            database.update_task(state["task_id"], status="done")
            database.append_task_step(state["task_id"], action.get("note") or "Готово",
                                      status="done", log=str(action.get("note") or "")[:1000])
            yield _ndjson({"type": "task", "task": database.get_task(state["task_id"])})
        return "Задача закрыта. Теперь ответь пользователю."

    if kind in MEDIA_ACTIONS:
        return (yield from _perform_media(action, kind, state))

    if kind in LINUX_ACTIONS and not use_tools:
        return ("Linux-окружение выключено (нет TERMINAL_ENABLED=1): команды и файлы недоступны. "
                "Ответь без них и предупреди, что код не запускался.")
    if kind in LINUX_ACTIONS:
        state["linux"] = True

    if kind == "search":
        state["searched"] = True
        query = str(action.get("query") or "").strip()
        if not query:
            return 'Пустой поисковый запрос. Повтори с {"action":"search","query":"..."}.'
<<<<<<< HEAD
        results, meta = nova_search(query, limit=6)
        numbered = _add_sources(state, results)
        text = _format_numbered(query, numbered, meta.get("backend"), meta.get("elapsed_ms", 0))
        if not numbered:
=======
        state["research"] += 1
        tool_result = call_tool("web_search", query=query, limit=3)
        results = tool_result.get("results") or []
        trace = tool_result.get("trace") or []
        text = "\n".join(
            f"{i}. {item.get('title','')}\n   {item.get('url','')}\n   {item.get('snippet','')}"
            for i, item in enumerate(results, 1)
        ) or str(tool_result.get("error") or "Поиск ничего не вернул.")
        backend = next((x.get("backend") for x in trace if x.get("ok")), "registry")
        elapsed = next((x.get("ms") for x in trace if x.get("ok")), 0)
        if not results:
>>>>>>> origin/agent-system-audit-fix-2026-10
            yield _ndjson({"type": "step", "icon": "⚠️", "text": f"Поиск «{query}» ничего не дал"})
            text += "\nНичего не нашлось. Попробуй другой запрос."
        else:
            state["searched"] = True
            yield _ndjson({"type": "step", "icon": "🔎",
<<<<<<< HEAD
                           "text": f"Нашёл {len(numbered)} по запросу «{query}» ({meta.get('backend')})"})
=======
                           "text": f"Нашёл {len(results)} по запросу «{query}» ({backend}, {elapsed} мс)"})
>>>>>>> origin/agent-system-audit-fix-2026-10
            yield _ndjson({"type": "tool", "name": "search", "command": f"nova search {query}",
                           "code": 0, "output": text, "results": results})
            yield _ndjson({"type": "sources", "sources": state["sources"]})
        if state["task_id"]:
            database.append_task_step(state["task_id"], f"Поиск: {query}", status="done",
                                      log=text[:1200])
        return text

    if kind == "open":
        url = str(action.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return "Нужен полный адрес страницы, например https://example.com"
<<<<<<< HEAD
        text, elapsed = nova_read(url, max_chars=4000)
=======
        state["research"] += 1
        open_result = call_tool("web_open", url=url, max_chars=4000)
        text = open_result.get("content") or ""
        if text and not text.startswith("Текст страницы"):
            text = "Текст страницы:\n" + text
        elapsed = 0
        if text:
            _add_sources(state, [{"title": urlparse(url).netloc, "url": url}])
            _note(state, f"страница {url}", text)
>>>>>>> origin/agent-system-audit-fix-2026-10
        if state["task_id"]:
            database.append_task_step(state["task_id"], f"Прочитал {url}", status="done",
                                      log=(text or "")[:1200])
        yield _ndjson({"type": "step", "icon": "📰", "text": f"Прочитал {url} ({elapsed} мс)"})
        yield _ndjson({"type": "tool", "name": "open", "command": url, "code": 0 if text else 1,
                       "output": (text or "страница пустая")[:1500]})
        return text or "Страница не прочиталась."

    if kind == "ls":
        command, label = "ls -la", "Смотрю файлы"
    elif kind == "read":
        rel_path = str(action.get("path", ""))
        read_result = call_tool("file_read", path=rel_path, max_chars=MAX_OBSERVATION)
        if not read_result.get("ok"):
            return str((read_result.get("error") or {}).get("message") or "Не удалось прочитать файл")
        try:
            path = safe_path(rel_path)
        except ValueError as exc:
            return str(exc)
<<<<<<< HEAD
        if not os.path.isfile(path):
            yield _ndjson({"type": "tool", "name": "read", "command": f"cat {action.get('path', '')}",
                           "code": 1, "output": "Файла нет"})
            return f"Файла {action.get('path', '')} нет. Посмотри список: {{\"action\":\"ls\"}}"
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_OBSERVATION)
=======
        content = str(read_result.get("content") or "")
>>>>>>> origin/agent-system-audit-fix-2026-10
        yield _ndjson({"type": "step", "icon": "📖", "text": f"Читаю {relative_to_workspace(path)}"})
        yield _ndjson({"type": "tool", "name": "read", "command": f"cat {relative_to_workspace(path)}",
                       "code": 0, "output": clip(content, 600)})
        return content or "(файл пустой)"

    elif kind == "write":
        rel_path = str(action.get("path", "notes/out.md"))
        content = str(action.get("content") or "")
        write_result = call_tool("file_write", path=rel_path, content=content)
        if not write_result.get("ok"):
            return str((write_result.get("error") or {}).get("message") or "Не удалось записать файл")
        try:
            path = safe_path(rel_path)
        except ValueError as exc:
            return str(exc)
<<<<<<< HEAD
        if os.path.isdir(path):
            return "Это папка, укажи путь к файлу."
        content = str(action.get("content") or "")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        relative = relative_to_workspace(path)
=======
>>>>>>> origin/agent-system-audit-fix-2026-10
        if state["task_id"]:
            database.append_task_step(state["task_id"], f"Записал {relative}",
                                      status="doing", log=clip(content, 800))
<<<<<<< HEAD
        yield _ndjson({"type": "step", "icon": "✍️", "text": f"Записал {relative}"})
        yield _ndjson({"type": "tool", "name": "write", "command": f"write {relative}",
                       "code": 0, "output": clip(content, 600)})
        return f"Файл {relative} записан ({len(content)} символов)."
=======
        yield _ndjson({"type": "step", "icon": "✍️", "text": f"Записал {relative_to_workspace(path)}"})
        yield _ndjson({"type": "tool", "name": "write", "command": relative_to_workspace(path),
                       "code": 0, "output": f"Файл {relative_to_workspace(path)} записан."})
        return f"Файл {relative_to_workspace(path)} записан. Открой его командой cat {relative_to_workspace(path)}."
>>>>>>> origin/agent-system-audit-fix-2026-10

    elif kind == "run":
        command = str(action.get("command") or "").strip()
        label = f"$ {command}"
    else:
        return f"Неизвестное действие: {kind}. Используй одно из: {', '.join(sorted(ALL_ACTIONS))}."

<<<<<<< HEAD
    result = run_terminal(command, timeout=STEP_TIMEOUT)
    output = (result["stdout"] or "") + (("\n" + result["stderr"]) if result["stderr"] else "")
=======
    result = call_tool("shell", command=command, timeout=STEP_TIMEOUT)
    result["code"] = int(result.get("exit_code", result.get("code", -1)) or -1)
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    output = stdout + (("\n" + stderr) if stderr else "")
    if _is_research_command(command) and not result["code"]:
        # модель искала не через action=search, а командой `nova search …` — учитываем так же
        state["research"] += 1
        _add_sources(state, _sources_from_text(output))
        _note(state, command, output)
>>>>>>> origin/agent-system-audit-fix-2026-10
    if state["task_id"]:
        database.append_task_step(
            state["task_id"], label, status="error" if result["code"] else "done",
            log=clip(output, 1200) or f"код {result['code']}",
        )
    status = "❌" if result["code"] else "✅"
    yield _ndjson({"type": "step", "icon": status, "text": f"{label} — код {result['code']}"})
    yield _ndjson({"type": "tool", "name": "run", "command": command, "code": result["code"],
                   "output": clip(output, 1500)})
<<<<<<< HEAD
    if _is_search_command(command):
        # Модель искала командой `nova search` — источники всё равно попадают под ответ
        found = [{"title": title.strip(), "url": url} for title, url in _RESULT_LINE.findall(output)]
        if _add_sources(state, found):
            state["searched"] = True
            yield _ndjson({"type": "sources", "sources": state["sources"]})
    body = output.strip() or "(пустой вывод)"
    return f"$ {command}\nкод выхода: {result['code']}\n{body}"


_RESULT_LINE = re.compile(r"^\s*\d+\.\s+(.+)\n\s+(https?://\S+)", re.M)


def _is_search_command(command):
    parts = str(command or "").split()
    return len(parts) >= 2 and parts[0] == "nova" and parts[1] == "search"


# ───────────────────────── финальный ответ ─────────────────────────

FINAL_PROMPT = ("Действий больше не будет. Напиши финальный ответ пользователю обычным текстом "
                "(markdown), без JSON: сначала суть, затем коротко пояснение. Опирайся только на "
                "результаты действий выше; чего не удалось узнать — скажи честно.")


def _final_answer(messages, provider, model, state):
    """Ответ по собранному, когда модель застряла на действиях. Генератор токенов → текст."""
    raw, _meta = chat_completion(
        messages + [{"role": "user", "content": FINAL_PROMPT}],
        provider=provider, model=model, system=f"{_persona()}\nОтвечай по-русски, по делу. Никакого JSON.",
        temperature=0.3, timeout=STREAM_TIMEOUT,
    )
    text = strip_think(str(raw or ""))
    action = parse_action(text) if text.lstrip().startswith(("{", "```")) else None
    if action:
        text = str(action.get("text") or "").strip() if action.get("action") == "answer" else ""
    if not text:
        if state["sources"]:
            links = "\n".join(f"{index}. [{item['title']}]({item['url']})"
                              for index, item in enumerate(state["sources"][:6], 1))
            text = ("Модель не смогла сформулировать итог, но вот что удалось найти:\n\n"
                    f"{links}\n\nПопробуйте спросить ещё раз или сменить модель в настройках.")
        else:
            text = "Не удалось собрать ответ. Попробуйте ещё раз или смените модель в настройках."
    for chunk in _chunks(text):
        yield _ndjson({"type": "token", "token": chunk})
    return text


# ───────────────────────── медиа ─────────────────────────

MEDIA_STAGE = {
    "image": ("🎨", "Рисую изображение"),
    "audio": ("🔊", "Озвучиваю"),
    "video": ("🎬", "Делаю видео"),
}


def mg_connected(kind):
    """Есть ли хоть одна модель для этого типа медиа (для статуса)."""
    try:
        return media_agent.quick_available(kind)
    except Exception:
        return False


def _perform_media(action, kind, state):
    """Генерация медиа по решению ИИ. Модель выбирает media_agent.

    Генератор событий (stage/step/media). Возвращает наблюдение для модели.
    """
    if kind == "media_models":
        wanted = str(action.get("type") or "all").strip().lower()
        yield _ndjson({"type": "step", "icon": "🧩", "text": "Смотрю, какие модели генерации доступны"})
        return media_agent.describe(wanted if wanted in media_agent.KINDS else "all")

    prompt = str(action.get("prompt") or action.get("text") or "").strip()
    if not prompt:
        field = "text" if kind == "audio" else "prompt"
        return f'Пустое описание. Повтори с {{"action":"{kind}","{field}":"..."}}.'

    icon, title = MEDIA_STAGE[kind]
    yield _ndjson({"type": "stage", "scene": "media", "title": title,
                   "text": clip(prompt, 140)})
    events = []
    result, model, errors = media_agent.generate(
        kind, prompt, options=media_agent.build_options(kind, action),
        wanted_model=str(action.get("model") or ""), on_status=events.append,
    )
    # На телефоне достаточно «какой моделью пробовал» — служебные строки провайдера прячем
    attempts = [message for message in events if message.startswith("Пробую")] or events[-1:]
    for message in attempts[-3:]:
        yield _ndjson({"type": "step", "icon": icon, "text": clip(message, 160)})

    if not result:
        reason = "; ".join(errors) or "неизвестная ошибка"
        yield _ndjson({"type": "step", "icon": "⚠️", "text": clip(f"Не получилось: {reason}", 220)})
        return (f"Генерация ({media_agent.KIND_LABELS[kind]}) не удалась: {clip(reason, 1200)}\n"
                f"Объясни пользователю коротко и по-человечески, что случилось и что подключить.")

    media = {
        "kind": result.get("kind") or kind,
        "url": result.get("url"),
        "filename": result.get("filename"),
        "mime": result.get("mime"),
        "model": result.get("model"),
        "model_name": result.get("model_name") or model.get("name"),
        "provider": result.get("provider"),
        "provider_name": result.get("provider_name") or model.get("provider_name"),
        "pricing_status": result.get("pricing_status"),
        "elapsed_ms": result.get("elapsed_ms"),
        "prompt": prompt,
        "title": prompt[:80],
    }
    if result.get("job_id"):
        media.update({"job_id": result["job_id"], "state": result.get("state") or "queued"})
    state["media"].append(media)
    yield _ndjson({"type": "media", "media": media})

    used = f"{media['model_name']} ({media['provider_name']})"
    tried = f" До этого не вышло: {'; '.join(errors)}." if errors else ""
    if media.get("job_id"):
        return (f"Видео поставлено в очередь у провайдера моделью {used}, задача {media['job_id']}. "
                f"Пользователь видит статус в чате, файл появится сам.{tried} "
                f"Коротко скажи, что видео готовится.")
    return (f"Готово: {media_agent.KIND_LABELS[kind]} создано моделью {used} за "
            f"{(media.get('elapsed_ms') or 0) / 1000:.1f} c и уже показано пользователю.{tried} "
            f"Не вставляй ссылку — коротко опиши результат или сделай следующий шаг.")
=======
    if result["code"]:
        return (f"код выхода {result['code']}.\n" + output) if output else (
            f"Команда завершилась с кодом выхода {result['code']} и пустым выводом."
        )
    return output or "Команда завершилась успешно, но ничего не вывела."
>>>>>>> origin/agent-system-audit-fix-2026-10
