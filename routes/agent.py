"""
routes/agent.py — HTTP-фасад над агентным движком AgenticSeek.

Здесь больше нет агентного цикла. Модель, планировщик, роли, инструменты и
протокол блоков живут в пакете ``agenticseek`` (порт AgenticSeek, GPL-3.0 —
см. ``NOTICE.md``). Этот модуль отвечает только за то, что относится к Flask:

* авторизация, лимиты запросов и заголовки ответа;
* история чата: чтение перед ходом и сохранение после;
* NDJSON-поток для интерфейса (``stage``/``plan``/``step``/``tool``/…);
* реестр активных запусков и кнопки «Стоп»;
* подтверждение опасных действий и правила «разрешить всегда»;
* учёт токенов и трасса шагов;
* мост к NovaMind-возможностям (поиск, MCP, медиа, память, задачи,
  расписание, навыки) — движок получает их как функции, а не импортом Flask.

Главное правило прежнее: пользователь ВСЕГДА получает ответ. Движок
гарантирует это сам (см. ``agenticseek.engine``), а здесь остаётся
сохранение хода и корректное закрытие запуска.
"""
from flask import Blueprint, Response, request, jsonify, session, stream_with_context
import json
import logging
import os
import re
import secrets
import threading
import time

from ai_providers import chat_completion, chat_stream, resolve_target  # noqa: F401
import database
import usage_stats
from agenticseek import policy
from agenticseek.policy import clip  # noqa: F401  (обрезка текста в трассе)
from agenticseek.core.prompt_paths import prompt_path  # noqa: F401 (документация/тесты)
from agenticseek.engine import AgentEngine, EngineContext
from agenticseek.router import looks_complex  # noqa: F401 (реэкспорт для тестов)
from agenticseek.state import RunState  # noqa: F401
from agenticseek.tools import sandbox
import routes.terminal as terminal_routes
from routes.terminal import (run_agent as run_terminal, ensure_workspace,  # noqa: F401
                             nova_search, nova_read, can_use_workspace, signed_in)


def workspace_dir() -> str:
    """Текущая папка песочницы — читается динамически.

    `routes.terminal` вычисляет её один раз, но тесты и переопределение
    TERMINAL_DIR подменяют модульную переменную; копия значения на импорте
    рассинхронизировала бы агент с файловым менеджером.
    """
    return terminal_routes.WORKSPACE
from agenticseek.core.workspace import set_work_dir as _set_agent_workspace

agent_bp = Blueprint("agent", __name__)
log = logging.getLogger("novamind.agent")

#: Служебный чат-ключ → последние сообщения. Страховка, если база недоступна.
_AGENT_HISTORY_CACHE = {}

MAX_STEPS = max(1, min(int(os.getenv("AGENT_MAX_STEPS", "20")), 60))
AGENT_TIMEOUT = max(10.0, float(os.getenv("AGENT_TIMEOUT", "180")))
STEP_TIMEOUT = float(os.getenv("AGENT_STEP_TIMEOUT", "12"))
MAX_OBSERVATION = 6000
MAX_SOURCES = 12

#: Имена, которые остались ради совместимости с прежним кодом.
action_risk = policy.risk_of
confirmation_needed = policy.needs_confirmation
always_allowed = policy.always_allowed
forget_always_allowed = policy.forget_always_allowed
always_allowed_rules = policy.always_rules
register_confirmation = policy.register_confirmation
resolve_confirmation = policy.resolve_confirmation
await_confirmation = policy.await_confirmation
describe_action = policy.describe_action
CONFIRM_TIMEOUT = policy.CONFIRM_TIMEOUT

#: Активные запуски: run_id → {"cancel": Event, "started": ts, "user": str}.
_ACTIVE_RUNS = {}
_RUNS_LOCK = threading.Lock()


def _untrusted(kind, text) -> str:
    """Помечает внешние данные как данные (см. agenticseek.policy)."""
    return policy.wrap_untrusted(kind, text)


def is_complex_request(text: str) -> bool:
    """Нужен ли планировщик (эвристика движка; имя сохранено для тестов)."""
    return looks_complex(text)


_MEDIA_VERB = re.compile(
    r"(сгенери|сгенерь|созда|сдела|нарису|нарисую|покажи|озвуч|озвучь|прочитай\s+вслух|"
    r"скажи\s+голосом|запиши\s+(?:голос|аудио)|спой|синтезир|"
    r"generate|create|draw|make|render|speak|say|voice)",
    re.I,
)

_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)

_THINK_TAIL = re.compile(r"^.*?</think(?:ing)?>", re.S | re.I)

_THINK_OPEN = re.compile(r"</?think(?:ing)?>", re.I)

_LEAK_START = re.compile(
    r"^[\s>*#-]*(?:"
    r"(?:we|i)\s+(?:need|should|must|have|will|can|'ll|'d)\b|"
    r"(?:let'?s|let me|need to|i need|i should|i'll|i will|okay|hmm|first|"
    r"then|next|finally|to answer|the answer|we'll|we are|we can|need answer|let us|"
    r"so we|looking at|given (?:the|that)|probably|maybe|now|thus|therefore|"
    r"alright|actually)(?:\b|,)|"
    r"(?:the user|user)\s+(?:asks|asked|wants|says|provided|supplied)\b|"
    r"(?:thinking|analysis|reasoning|chain[- ]of[- ]thought)\s*[:：]"
    r")", re.I)

_DRAFT_HINT = re.compile(
    r"(?i)\b(?:then explanation|explanation \d+\s+sentences?|we need answer|"
    r"need answer|let'?s formulate|they (?:supplied|provided)|"
    r"the user (?:asks|wants|supplied|provided)|draft answer|"
    r"черновик|пользователь (?:спрашивает|просит|прислал)|"
    r"сформулируем ответ|нужно ответить)\b")

_LEAK_START_RU = re.compile(
    r"^[\s>*#-]*(?:"
    r"(?:нужно|надо)\s+(?:ответить|понять|разобраться|сформулировать|посмотреть|проверить|учитывать)|"
    r"пользователь\s+(?:спрашивает|просит|хочет|задал|сказал|прислал)|"
    r"(?:давай|давайте)\s+(?:сформулируем|разберём|посмотрим|ответим|проверим|подумаем)|"
    r"(?:план|черновик)\s+(?:ответа|рассуждени)|"
    r"рассуждени[ея]\s*[:：]|"
    r"хорошо,?\s+(?:давай|начн[её]м|проверим)"
    r")", re.I)

_FINAL_MARK = re.compile(
    r"(?im)^\s*(?:final answer|answer|итоговый ответ|ответ|итог)\s*[:：]\s*")

_TRANSLATE_GOAL = re.compile(
    r"(?i)(?:перевед|перевод|на\s+английск|по-английск|\btranslate\b|\bin\s+english\b)")

_CYR = re.compile(r"[А-Яа-яЁё]")

_LAT = re.compile(r"[A-Za-z]")

_EN_STOP = {
    "the", "a", "an", "to", "of", "and", "or", "is", "are", "was", "were", "be",
    "been", "it", "this", "that", "these", "those", "we", "i", "you", "he", "she",
    "they", "need", "should", "must", "can", "will", "would", "for", "in", "on",
    "with", "as", "at", "by", "from", "not", "but", "if", "then", "so", "about",
    "what", "how", "which", "who", "when", "where", "why",
}

_CODE_START = ("```", "~~~", "def ", "class ", "import ", "from ", "{", "[", "<", "#")

_BAD_ENDINGS = set(",;:—–-\\([{")

_TRUNCATED_WORDS = {
    "и", "в", "во", "на", "с", "со", "по", "для", "что", "как", "это", "не",
    "а", "но", "или", "же", "ли", "при", "из", "от", "до", "к", "о", "об",
    "the", "and", "of", "to", "in", "on", "with", "for", "is", "are", "was",
    "were", "a", "an", "that", "which", "as", "by", "at", "from", "or", "but",
}

_QUOTED_ANSWER = re.compile(r'["“«]([^"”»]{40,700})["”»]', re.S)

def _has_cyrillic(text):
    return bool(_CYR.search(text or ""))

def _split_paragraphs(text):
    return [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]

def _looks_complete_answer(text):
    """Похож ли текст на законченный ответ, а не на обрезанный черновик.

    Точка в конце не обязательна (модели часто её опускают), а вот запятая,
    тире, открытая кавычка или служебное слово на конце — признак обрыва.
    """
    text = (text or "").strip()
    if not text:
        return False
    if text.count('"') % 2:             # открытая кавычка — ответ оборван
        return False
    if text.count("«") != text.count("»"):
        return False
    if text[-1] in _BAD_ENDINGS:
        return False
    if len(text) <= 40:                 # «42», «Готово», короткая реплика
        return True
    last = text.splitlines()[-1].lstrip()
    if last.startswith(("-", "*", "•", "|")) or re.match(r"^\d+[.)]\s", last):
        return True
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", text)
    if words and words[-1].lower() in _TRUNCATED_WORDS:
        return False
    return True


def is_enabled():
    """Агент включён по умолчанию.

    Раньше режим требовал явного `AGENT_ENABLED=1`: без него главный чат был
    обычным текстовым, и «агент в чате» существовал только в документации.
    Теперь агент — основной путь чата, а отключить его можно осознанно:
    `AGENT_ENABLED=0`.
    """
    value = os.getenv("AGENT_ENABLED")
    if value is None or not str(value).strip():
        return True
    return str(value).strip() == "1"


def strict_mcp_request(text: str) -> bool:
    """Определяет запросы, где пользователь явно запретил обычный web-search."""
    value = str(text or "").lower()
    explicit = re.search(r"не\s+используй\s+(?:web[_ -]?search|поиск)|только\s+(?:mcp|github|composio|switchboard|youtube)", value)
    named_service = any(name in value for name in ("github mcp", "composio", "switchboard", "mcp-инструмент", "mcp инструмент"))
    return bool(explicit or named_service)


def requested_media_kind(text: str):
    """Возвращает media action только для явного запроса генерации.

    Список глаголов расширен: «озвучь приветствие», «покажи закат», «сделай
    ролик» — это такие же явные запросы, как «нарисуй». Иначе модель могла
    ответить текстом вместо реальной генерации.
    """
    value = str(text or "").lower()
    if not _MEDIA_VERB.search(value):
        return None
    if re.search(r"(изображени|картинк|фото|иллюстрац|постер|image|picture|photo|draw)", value):
        return "image"
    if re.search(r"(видео|ролик|клип|video|clip|анимаци)", value):
        return "video"
    if re.search(r"(аудио|озвуч|голос|музык|audio|voice|music|вслух|speech)", value):
        return "audio"
    # Глагол без существительного — тоже явный запрос: «нарисуй кота»,
    # «озвучь приветствие». Без этого такие просьбы уходили в текстовый
    # ответ, и картинка не появлялась.
    if re.search(r"\b(нарису|нарисую|draw|render|sketch)", value):
        return "image"
    if re.search(r"\b(озвуч|прочитай\s+вслух|скажи\s+голосом|спой|speak|voice)", value):
        return "audio"
    return None


def tools_enabled():
    """Инструменты терминала и файлов — любому вошедшему при включённом окружении.

    Флаг читаем в момент вызова, а не на импорте: .env может подгрузиться позже.
    """
    return os.getenv("TERMINAL_ENABLED", "0") == "1" and can_use_workspace()


def strip_think(text):
    """Убирает «рассуждения» из ответа (nemotron, deepseek и др. кладут их прямо в текст)."""
    text = _THINK_BLOCK.sub("", text or "")
    text = _THINK_TAIL.sub("", text)          # закрывающий тег без открывающего
    return _THINK_OPEN.sub("", text).strip()


def looks_like_leaked_reasoning(text):
    """True, если текст начинается как рассуждение модели, а не как ответ."""
    text = strip_think(text)
    if not text:
        return False
    if _LEAK_START.match(text) or _LEAK_START_RU.match(text):
        return True
    if _DRAFT_HINT.search(text[:800]):
        return True
    # Без явных маркеров смотрим на первый абзац: естественный английский
    # текст с местоимениями/служебными словами — почти наверняка черновик.
    # Код и разметку не трогаем: ответ может начинаться с блока кода.
    paragraphs = _split_paragraphs(text)
    first = paragraphs[0] if paragraphs else text
    if first.lstrip().startswith(_CODE_START):
        return False
    cyr, lat = len(_CYR.findall(first)), len(_LAT.findall(first))
    words = re.findall(r"[A-Za-z']+", first)
    stops = sum(1 for word in words if word.lower().strip("'") in _EN_STOP)
    return lat >= 60 and cyr < 12 and stops >= 8 and " " in first


def strip_leaked_reasoning(text, goal=""):
    """Убирает из ответа утёкшее рассуждение; если ответа под ним нет — пустая строка.

    Работает только для русскоязычных запросов: английский ответ на английский
    вопрос («Let me explain…») — это не утечка. Если модель начала с мыслей, но
    ниже оставила готовый русский ответ, сохраняем только его.
    """
    text = strip_think(text)
    if not text or not _has_cyrillic(goal) or not looks_like_leaked_reasoning(text):
        return text
    if _TRANSLATE_GOAL.search(goal or ""):
        return text            # просили перевести — английский текст здесь ответ
    # В конце готового ответа модель иногда оставила служебную пометку
    # («Then explanation 2 sentences: …»). Тогда всё, что до неё, — ответ.
    hint = _DRAFT_HINT.search(text)
    if hint and hint.start() > 0:
        prefix = text[:hint.start()].strip()
        if (len(prefix) >= 40 and _has_cyrillic(prefix)
                and not looks_like_leaked_reasoning(prefix)
                and _looks_complete_answer(prefix)):
            return prefix
    # Иногда модель сама помечает финал: «Ответ: …». Берём последнюю такую метку.
    markers = list(_FINAL_MARK.finditer(text))
    if markers:
        candidate = text[markers[-1].end():].strip()
        if (candidate and _has_cyrillic(candidate)
                and not looks_like_leaked_reasoning(candidate)
                and _looks_complete_answer(candidate)):
            return candidate
    # Иначе оставляем непрерывный хвост русских абзацев, похожих на ответ.
    tail = []
    for paragraph in reversed(_split_paragraphs(text)):
        cyr, lat = len(_CYR.findall(paragraph)), len(_LAT.findall(paragraph))
        draft = paragraph[:1] in "\"'«“„" or bool(_LEAK_START.match(paragraph)) \
            or bool(_LEAK_START_RU.match(paragraph)) or bool(_DRAFT_HINT.search(paragraph))
        if draft or cyr < 15 or cyr < lat * 1.5:
            break
        tail.append(paragraph)
    answer = "\n\n".join(reversed(tail)).strip()
    if (len(answer) >= 40 and not looks_like_leaked_reasoning(answer)
            and _looks_complete_answer(answer)):
        return answer
    return ""


def salvage_quoted_answer(text, goal=""):
    """Достаёт готовый русский ответ из кавычек внутри утёкшего рассуждения.

    Бесплатные модели часто пишут черновик, а сам ответ оформляют в кавычках.
    Это последний шанс не отдать пользователю пустоту. Цитату вопроса
    пользователя и слишком короткие обрывки не берём.
    """
    text = strip_think(text)
    if not text or not _has_cyrillic(goal):
        return ""
    goal_text = " ".join(str(goal or "").lower().split())
    best = ""
    for match in _QUOTED_ANSWER.finditer(text):
        candidate = " ".join(match.group(1).split())
        low = candidate.lower()
        if not _has_cyrillic(candidate) or len(candidate) < 40:
            continue
        if goal_text and (low in goal_text or goal_text in low):
            continue                       # это цитата вопроса, а не ответ
        if looks_like_leaked_reasoning(candidate):
            continue
        if candidate.count("?") > candidate.count("."):
            continue
        if len(candidate) > len(best):
            best = candidate
    return best


_LEGACY_ACTION = re.compile(r'^\s*\{[^{}]*"action"\s*:\s*"[^"]+"', re.S)


def final_answer_text(raw, goal=""):
    """Приводит ответ модели к финальному тексту.

    Убирает рассуждения и служебные блоки. JSON-действия старого протокола
    (``{"action": "..."}``) больше не поддерживаются: если модель всё-таки
    вернула такое вместо ответа, текст отбрасывается, и движок честно
    сообщает, что ответа нет, — вместо того чтобы показать пользователю JSON.
    """
    text = strip_think(raw)
    if not text:
        return ""
    if _LEGACY_ACTION.match(text):
        return ""
    answer = strip_leaked_reasoning(text, goal)
    if answer and not _looks_complete_answer(answer):
        return ""
    return answer


def _turn_history(history_key, limit=10):
    """(chat_id, prior) для текущего хода.

    Источник истины — SQLite: обычный чат и агент теперь пишут в одну историю,
    поэтому после перезагрузки страницы агент помнит разговор. Кеш в сессии
    остаётся запасным путём, если база недоступна.
    """
    chat_id = None
    prior = []
    try:
        import database
        chat_id = database.get_or_create_session_chat()
        prior = database.get_chat_history(chat_id, limit=limit)
    except Exception:
        chat_id = None
    if not prior:
        cached = _AGENT_HISTORY_CACHE.get(history_key, session.get("agent_history", []))
        prior = cached if isinstance(cached, list) else []
    clean = []
    for item in prior:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
            content = str(item.get("content") or "").strip()
            if content:
                clean.append({"role": item["role"], "content": content[:4000]})
    return chat_id, clean[-limit:]


def _save_turn(chat_id, history_key, goal, answer, extra_messages=None):
    """Сохраняет ход агента в общую историю чата и в кеш сессии."""
    answer = str(answer or "").strip()
    if chat_id and answer:
        try:
            import database
            database.add_message(chat_id, "user", goal)
            database.add_message(chat_id, "assistant", answer)
            # Медиа — отдельными сообщениями, как в /send: интерфейс ищет
            # маркер в начале content.
            for content in extra_messages or []:
                if content:
                    database.add_message(chat_id, "assistant", content)
            database.trim_messages(chat_id, max_messages=200)
        except Exception:
            log.warning("Не удалось сохранить ход агента в историю", exc_info=True)
    updated = (_AGENT_HISTORY_CACHE.get(history_key) or [])
    updated = [m for m in updated if isinstance(m, dict)]
    updated += [{"role": "user", "content": goal},
                {"role": "assistant", "content": answer}]
    _AGENT_HISTORY_CACHE[history_key] = updated[-10:]
    # Кеш — только вспомогательный: не даём ему расти без предела.
    if len(_AGENT_HISTORY_CACHE) > 500:
        for old_key in list(_AGENT_HISTORY_CACHE)[:100]:
            _AGENT_HISTORY_CACHE.pop(old_key, None)
    try:
        session["agent_history"] = updated[-10:]
    except Exception:
        pass


def _ndjson(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _history_text(history):
    """Текст промпта для оценки токенов, если провайдер не прислал usage."""
    parts = []
    for item in history or []:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
    return "\n".join(parts)


def _trace_entries(chunk):
    """Превращает кусок NDJSON в записи трассы (шаги и команды)."""
    try:
        event = json.loads(chunk)
    except (TypeError, ValueError):
        return []
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind == "step":
        return [{"kind": "step", "icon": event.get("icon") or "•",
                 "text": clip(str(event.get("text") or ""), 300)}]
    if kind == "tool":
        entry = {"kind": "tool", "name": event.get("name") or "tool",
                 "command": clip(str(event.get("command") or ""), 300),
                 "code": event.get("code")}
        if event.get("path"):
            entry["path"] = str(event.get("path"))[:200]
        return [entry]
    if kind == "plan":
        pieces = [str(step.get("text") or "") for step in event.get("steps") or []]
        return [{"kind": "plan", "text": clip(" → ".join(pieces), 500)}] if pieces else []
    return []


def _chunks(text, size=48):
    text = text or ""
    for index in range(0, len(text), size):
        yield text[index:index + size]

# ─────────────────────────── окружение ───────────────────────────

class NovaToolsContext:
    """Мост из движка в возможности NovaMind.

    Движок не импортирует Flask и модули проекта: он вызывает эти методы.
    Так проще тестировать и видно, какие именно возможности реально доступны
    в текущем запуске (остальные инструменты модель даже не увидит).
    """

    def __init__(self, goal: str, chat_id: str, emit, signed: bool, owner: str,
                 memory_key: str = ""):
        self.goal = goal
        self.chat_id = chat_id
        self.emit = emit
        self.signed = signed
        self.owner = owner
        # Ключ памяти считается в потоке запроса: инструменты выполняются
        # в рабочем потоке движка, где flask.session уже недоступен.
        self._memory_user = memory_key
        self.task_id = ""
        self.mcp_tools = self._mcp_tools()
        self.memory_available = self._memory_key() != ""
        self.tasks_available = True
        self.schedule_available = bool(owner)
        self.skills_available = True
        for kind in ("image", "audio", "video"):
            setattr(self, f"media_{kind}_available", self._media_ready(kind))

    # ── что доступно ─────────────────────────────────────────────────────
    @staticmethod
    def _mcp_tools():
        try:
            import mcp_registry
            return mcp_registry.connected_tools()
        except Exception:  # noqa: BLE001 — MCP необязателен
            log.debug("MCP недоступен", exc_info=True)
            return []

    @staticmethod
    def _media_ready(kind: str) -> bool:
        try:
            import media_agent
            return bool(media_agent.quick_available(kind))
        except Exception:  # noqa: BLE001
            return False

    def _memory_key(self) -> str:
        if self._memory_user:
            return self._memory_user
        import memory as memory_module
        try:
            from flask import session as flask_session
            return memory_module.user_key(flask_session, signed_in())
        except Exception:  # noqa: BLE001
            return ""

    # ── действия ─────────────────────────────────────────────────────────
    def call_mcp(self, server_id: str, tool: str, arguments: dict):
        import mcp_registry
        return mcp_registry.call_tool(server_id, tool, arguments)

    def generate_media(self, kind: str, prompt: str, params: dict):
        """Генерация медиа: (result, model, errors), как у media_agent."""
        import media_agent
        options = media_agent.build_options(kind, params or {})
        wanted = str((params or {}).get("model") or "")

        def on_status(text):
            self.emit({"type": "step", "icon": "🎨", "text": str(text)[:300]})

        try:
            result, model, errors = media_agent.generate(kind, prompt, options, wanted,
                                                         on_status=on_status)
        except Exception as error:  # noqa: BLE001 — честно возвращаем ошибку
            return None, None, [f"{type(error).__name__}: {error}"]
        return result, model, errors or []

    def remember(self, text: str, kind: str = "fact", tags: str = ""):
        import memory as memory_module
        return memory_module.remember(self._memory_key(), text, kind=kind, tags=tags)

    def recall(self, query: str, limit: int = 5):
        import memory as memory_module
        return memory_module.recall(self._memory_key(), query, limit=limit)

    def forget(self, memory_id: str):
        import memory as memory_module
        return memory_module.forget(self._memory_key(), memory_id)

    def create_task(self, title: str, detail: str = ""):
        task = database.create_task(title=title, detail=detail, source="agent")
        self.task_id = task["id"]
        database.append_task_step(task["id"], "Задача заведена", status="doing", log=detail[:1000])
        self.emit({"type": "task", "task": task})
        return task

    def close_task(self, task_id: str, note: str = ""):
        task = database.get_task(task_id)
        if not task:
            return None
        database.update_task(task_id, status="done")
        database.append_task_step(task_id, note[:200] or "Готово", status="done", log=note[:1000])
        task = database.get_task(task_id)
        self.emit({"type": "task", "task": task})
        return task

    def create_schedule(self, prompt: str, kind: str, expr: str):
        import scheduler
        job = scheduler.create_job(self.owner, prompt, kind, expr, chat_id=self.chat_id)
        self.emit({"type": "schedule", "job": job})
        when = time.strftime("%d.%m %H:%M", time.localtime(job["next_run_at"]))
        return job, scheduler.describe(job), when

    def save_skill(self, name: str, content: str):
        import skills_registry
        item = skills_registry.save_text(name, content)
        self.emit({"type": "skill", "skill": item})
        return item

    def add_source(self, url: str, title: str = "") -> None:
        pass


# ─────────────────────────── запуски ───────────────────────────

def register_run(run_id=None):
    """Регистрирует запуск и возвращает (run_id, cancel_event)."""
    run_id = run_id or secrets.token_hex(8)
    cancel = threading.Event()
    with _RUNS_LOCK:
        _ACTIVE_RUNS[run_id] = {"cancel": cancel, "started": time.time()}
        # Не даём реестру расти: снимаем записи старше двух часов.
        for key, item in list(_ACTIVE_RUNS.items()):
            if time.time() - item["started"] > 7200:
                _ACTIVE_RUNS.pop(key, None)
    return run_id, cancel


def cancel_agent_run(run_id) -> bool:
    """Сигнал «Стоп»: взводит событие отмены. False — запуск не найден."""
    with _RUNS_LOCK:
        item = _ACTIVE_RUNS.get(str(run_id or ""))
    if not item:
        return False
    item["cancel"].set()
    return True


def finish_run(run_id) -> None:
    with _RUNS_LOCK:
        _ACTIVE_RUNS.pop(str(run_id or ""), None)


def active_run(run_id):
    with _RUNS_LOCK:
        return _ACTIVE_RUNS.get(str(run_id or ""))


# ─────────────────────────── маршруты ───────────────────────────

@agent_bp.route("/api/agent/status")
def agent_status():
    enabled = is_enabled()
    try:
        import mcp_registry
        mcp_tool_count = len(mcp_registry.connected_tools())
    except Exception:  # noqa: BLE001
        mcp_tool_count = 0
    workspace = tools_enabled()
    return jsonify({
        "enabled": enabled,
        "available": bool(enabled),
        "engine": "agenticseek",
        "tools": workspace,
        "search": bool(enabled),
        "mcp": bool(mcp_tool_count),
        "mcp_tools": mcp_tool_count,
        "guest": not signed_in(),
        "max_steps": MAX_STEPS,
        "timeout": AGENT_TIMEOUT,
        "confirm_mode": policy.confirm_mode(),
        "user": signed_in(),
        "admin": bool(session.get("admin_logged_in")),
        "hint": ("" if enabled
                 else "Включите AGENT_ENABLED=1 в .env и перезапустите сервер"),
        "workspace_hint": ("" if workspace else
                           "Файлы и терминал доступны после входа при TERMINAL_ENABLED=1. "
                           "Поиск, страницы, медиа, память и задачи работают сразу."),
        "media": {kind: _media_ready(kind) for kind in ("image", "audio", "video")},
    })


@agent_bp.route("/api/agent/stop", methods=["POST"])
def agent_stop():
    """Кнопка «Стоп»: {run_id}. Агент останавливается на ближайшей проверке."""
    data = request.get_json(silent=True) or {}
    run_id = str(data.get("run_id") or "").strip()
    if not run_id:
        return jsonify({"ok": False, "error": "Нужен run_id"}), 400
    if not cancel_agent_run(run_id):
        return jsonify({"ok": False, "run_id": run_id,
                        "error": "Запуск не найден или уже завершён"}), 404
    return jsonify({"ok": True, "run_id": run_id, "stopped": True})


@agent_bp.route("/api/agent/confirm", methods=["POST"])
def agent_confirm():
    """Ответ на запрос подтверждения: {"id", "approve", "always"}."""
    data = request.get_json(silent=True) or {}
    confirm_id = str(data.get("id") or "").strip()
    approve = data.get("approve")
    always = data.get("always", False)
    if not confirm_id:
        return jsonify({"ok": False, "error": "Нужен id подтверждения"}), 400
    if not isinstance(approve, bool):
        return jsonify({"ok": False, "error": "Поле approve должно быть true/false"}), 400
    if not isinstance(always, bool):
        return jsonify({"ok": False, "error": "Поле always должно быть true/false"}), 400
    ok, reason = policy.resolve_confirmation(confirm_id, approve, always=always)
    if not ok:
        message = ("Подтверждение уже обработано" if reason == "already_resolved"
                   else "Подтверждение не найдено или истекло")
        return jsonify({"ok": False, "id": confirm_id, "error": message}), 409
    return jsonify({"ok": True, "id": confirm_id, "approved": approve,
                    "always": bool(approve and always)})


@agent_bp.route("/api/agent/permissions", methods=["GET"])
def agent_permissions():
    """Действующие правила «разрешить всегда»."""
    return jsonify({"ok": True, "mode": policy.confirm_mode(), "rules": policy.always_rules()})


@agent_bp.route("/api/agent/permissions", methods=["DELETE"])
def agent_permissions_reset():
    """Сбрасывает все правила «разрешить всегда»."""
    policy.forget_always_allowed()
    return jsonify({"ok": True, "rules": []})


@agent_bp.route("/api/agent/runs")
def agent_runs():
    """История запусков движка (панель «Что делал агент»)."""
    chat_id = request.args.get("chat_id") or session.get("chat_id") or ""
    try:
        limit = max(1, min(int(request.args.get("limit") or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    runs = database.list_agent_runs(chat_id or None, limit=limit)
    return jsonify({"ok": True, "runs": runs})


@agent_bp.route("/api/agent/runs/<run_id>")
def agent_run_detail(run_id):
    record = database.get_agent_run(run_id)
    if not record:
        return jsonify({"ok": False, "error": "Запуск не найден"}), 404
    return jsonify({"ok": True, "run": record})


def _media_ready(kind: str) -> bool:
    try:
        import media_agent
        return bool(media_agent.quick_available(kind))
    except Exception:  # noqa: BLE001
        return False


def _sandbox_runner(command, timeout=None):
    """Песочница NovaMind для инструментов движка (bash/python)."""
    return run_terminal(command, timeout=timeout or STEP_TIMEOUT)


def _owner_name() -> str:
    return (str(session.get("admin_username") or session.get("nova_user_nick")
                or session.get("nova_user_email") or "").lower()[:180]
            if signed_in() else "")


def _media_markers(media) -> list:
    """Маркеры MEDIA_RESULT для истории: интерфейс отрисует их после перезагрузки.

    Формат маркера общий с обычным чатом (``routes.chat.media_result_marker``),
    иначе после перезагрузки страницы медиа не отрисуется.
    """
    from routes.chat import media_result_marker
    markers = []
    for item in media or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result") or item
        model = item.get("model") or {}
        if not (result.get("url") or result.get("filename") or result.get("job_id")):
            continue
        try:
            markers.append(media_result_marker(result, model if isinstance(model, dict) else {}))
        except Exception:  # noqa: BLE001 — маркер не должен ломать ход
            log.debug("media marker failed", exc_info=True)
    return markers


@agent_bp.route("/api/agent/stream", methods=["POST"])
def agent_stream():
    """Ход агента: план → инструменты → ответ. NDJSON."""
    data = request.get_json(silent=True) or {}
    goal = (data.get("message") or "").strip()
    if not goal:
        return jsonify({"error": "Пустое сообщение"}), 400
    if not is_enabled():
        return jsonify({"error": "Агентный режим выключен (AGENT_ENABLED=1)"}), 403

    import security
    signed = signed_in()
    rate_key = str(session.get("agent_memory_key") or session.get("nova_user_email")
                   or session.get("user_email") or session.get("username") or "")
    limit, remaining = 0, 0
    if security.is_internal_run(request):
        allowed = True   # планировщик и Telegram работают внутри процесса
    else:
        allowed, retry_after, limit, remaining, scope = security.agent_rate_check(
            request, signed, rate_key)
    if not allowed:
        response = jsonify({
            "error": "Слишком много запросов к агенту. Попробуйте позже.",
            "retry_after": retry_after, "limit": limit, "scope": scope,
        })
        response.headers["Retry-After"] = str(retry_after)
        return response, 429

    provider, model, _cfg = resolve_target()
    use_tools = tools_enabled()
    strict_mcp = strict_mcp_request(goal)
    history_key = session.get("agent_memory_key")
    if not history_key:
        history_key = secrets.token_hex(12)
        session["agent_memory_key"] = history_key
    chat_id, prior = _turn_history(history_key)
    if use_tools:
        ensure_workspace()
    # Агент и песочница работают в одной папке: иначе файлы агента не видны
    # в файловом менеджере, а команды выполняются в другом дереве.
    _set_agent_workspace(workspace_dir())
    # Инструменты движка исполняют команды только через эту песочницу.
    sandbox.set_runner(_sandbox_runner)

    run_id, cancel = register_run()
    media_kind = requested_media_kind(goal)
    owner = _owner_name()
    memory_key = ""
    try:
        import memory as memory_module
        memory_key = memory_module.user_key(session, signed_in())
    except Exception:  # noqa: BLE001
        memory_key = ""

    @stream_with_context
    def generate():
        started = time.time()
        tools_ctx = NovaToolsContext(goal, chat_id or "", emit=lambda event: None,
                                     signed=signed, owner=owner, memory_key=memory_key)
        memory_block = ""
        if memory_key:
            try:
                import memory as memory_module
                memory_block = memory_module.prompt_block(memory_key, goal) or ""
            except Exception:  # noqa: BLE001 — память не должна ломать ход
                log.debug("memory block failed", exc_info=True)
        skill_block = ""
        try:
            import skills_registry
            skill_block = skills_registry.prompt_context() or ""
        except Exception:  # noqa: BLE001
            log.debug("skills block failed", exc_info=True)

        engine = AgentEngine(EngineContext(
            goal=goal, complete=chat_completion, emit=lambda event: None,
            provider=provider, model=model, cancel=cancel,
            history=prior, workspace=use_tools,
            search=nova_search, read_page=_read_page, runner=_sandbox_runner,
            tools_ctx=tools_ctx, memory_prompt=memory_block, skills_prompt=skill_block,
            strict_mcp=strict_mcp,
            finalize=lambda text: final_answer_text(text, goal),
            salvage_answer=lambda text: salvage_quoted_answer(text, goal),
            chat_id=chat_id or "",
            run_id=run_id, force_role=("files" if media_kind else ""),
            media_kind=media_kind or "",
            media_generate=(tools_ctx.generate_media if media_kind else None),
            local_model=_local_model,
            offline=(provider == "local_demo"),
            force_search=bool(data.get("search")) and not strict_mcp,
            save_research=(terminal_routes._save_research if use_tools else None),
            max_steps=MAX_STEPS,
        ))
        tools_ctx.emit = engine._emit
        try:
            for event in engine.stream():
                yield _ndjson(event)
        finally:
            finish_run(run_id)

        answer = engine.answer
        state = engine.state
        markers = _media_markers(state.media)
        _save_turn(chat_id, history_key, goal, answer, extra_messages=markers)
        usage = usage_stats.record_turn(
            chat_id, provider, model, prompt_text=_history_text(prior),
            answer_text=answer, usage=engine.provider.usage or None)
        yield _ndjson({"type": "usage", "usage": usage})
        yield _ndjson({
            "type": "result", "reply": answer, "steps": state.steps,
            "usage": usage, "task_id": tools_ctx.task_id or None,
            "model": model, "provider": provider,
            "sources": state.sources, "searched": bool(state.sources),
            "media": state.media_results, "linux": bool(state.tools and use_tools),
            "tools": state.tools, "offline": provider == "local_demo",
            "chat_id": chat_id, "run_id": run_id, "engine": "agenticseek",
            "stopped": state.status == "stopped",
            "status": "stopped" if state.status == "stopped" else
                      ("error" if state.status == "error" else "ok"),
            "error": state.error, "seconds": round(time.time() - started, 1),
        })
        yield _ndjson({"type": "done"})

    def traced(stream):
        """Копия потока, которая пишет трассу шагов в базу."""
        collected = []
        try:
            for chunk in stream:
                collected.extend(_trace_entries(chunk))
                yield chunk
        finally:
            try:
                database.save_trace(chat_id, run_id, collected)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось сохранить трассу шагов", exc_info=True)

    return Response(traced(generate()), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "X-Agent-Run-Id": run_id,
                             "X-Agent-Engine": "agenticseek",
                             "X-RateLimit-Limit": str(limit),
                             "X-RateLimit-Remaining": str(remaining)})


def _local_model(messages):
    """Офлайн-ответчик NovaMind (local_llm) — только при provider=local_demo."""
    import local_llm
    return local_llm.complete(messages)


def _read_page(url, max_chars=6000):
    """Чтение страницы для веб-агента (тот же зачитыватель, что у поиска)."""
    from routes.search import fetch_page_text
    fetched = fetch_page_text(url, max_chars=max_chars)
    if isinstance(fetched, tuple):
        return fetched[0], fetched[1]
    return fetched, ""


@agent_bp.route("/api/usage", methods=["GET"])
def agent_usage():
    """Итоги токенов за текущий чат и лимиты провайдеров."""
    chat_id = session.get("chat_id")
    totals = usage_stats.chat_summary(chat_id) if chat_id else {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0}
    # `chat` — то, что читает интерфейс (static/app.js); `totals` оставлен для
    # старых клиентов и скриптов, которые уже обращаются к этому полю.
    return jsonify({"ok": True, "chat_id": chat_id, "chat": totals, "totals": totals,
                    "groq": usage_stats.groq_limits()})
