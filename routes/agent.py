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
from types import SimpleNamespace

from ai_providers import chat_completion, chat_stream, resolve_target  # noqa: F401
import capabilities
import database
import guardrails
import identity as _identity
import usage_stats
from agenticseek import policy
from agenticseek.policy import clip  # noqa: F401  (обрезка текста в трассе)
from agenticseek.core.prompt_paths import prompt_path  # noqa: F401 (документация/тесты)
import prompt_registry
import telemetry
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
    return terminal_routes._ws()
from agenticseek.core.workspace import set_context_work_dir as _set_agent_workspace

agent_bp = Blueprint("agent", __name__)


@agent_bp.before_request
def _runs_ownership_guard():
    """История запусков агента - только своя (без chat_id раньше отдавалась чужая)."""
    path = request.path
    if path == "/api/agent/runs":
        chat_id = request.args.get("chat_id") or session.get("chat_id") or ""
        if not chat_id or not database.chat_allowed(chat_id):
            return jsonify({"ok": True, "runs": []})
    elif path.startswith("/api/agent/runs/"):
        record = database.get_agent_run(path.rsplit("/", 1)[-1])
        if not record or not database.chat_allowed(record.get("chat_id") or ""):
            return jsonify({"ok": False, "error": "Запуск не найден"}), 404
    return None
log = logging.getLogger("novamind.agent")

#: Служебный чат-ключ → последние сообщения. Страховка, если база недоступна.
_AGENT_HISTORY_CACHE = {}

MAX_STEPS = max(1, min(int(os.getenv("AGENT_MAX_STEPS", "20")), 60))
AGENT_TIMEOUT = max(10.0, float(os.getenv("AGENT_TIMEOUT", "180")))
STEP_TIMEOUT = float(os.getenv("AGENT_STEP_TIMEOUT", "12"))
MAX_OBSERVATION = 6000
MAX_SOURCES = 12
AGENT_MAX_TOKENS = 4096  # или другое подходящее значение
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


#: Глаголы, после которых «сгенерировать» — это ещё не медиа.
#: Раньше здесь был широкий набор, и «покажи, что выводит код» или
#: «создай приложение с анимацией» уходили в генерацию картинки/видео вместо
#: работы с кодом. Теперь медиа требует ЯВНОГО объекта (картинка/видео/аудио).
_CREATE_VERB = re.compile(r"(сгенер|созда|сдела|generate|create|make)", re.I)
_SHOW_VERB = re.compile(r"(покажи|show|display)", re.I)
#: «Нарисуй», «озвучь», «спой» — медиа и без существительного. Функции вида
#: `draw_square(` и `drawSq`, а также части URL/имён (`user-sketch/repo`,
#: `sketch.py`, `на render`) не считаем: это код, ссылки или сервисы.
_DRAW_VERB = re.compile(
    r"(?<![\wа-яё./-])(?:нарису\w*|draw|sketch|(?<!на\s)(?<!on\s)render)(?![\wа-яё(./-])",
    re.I,
)
_VOICE_VERB = re.compile(r"(?<![\wа-яё./-])(?:озвуч\w*|прочитай\s+вслух|скажи\s+голосом|спой|"
                         r"speak|synthesize|voice\s+over)(?![\wа-яё(./-])", re.I)
#: Запуск кода — это исполнение, а не генерация.
_RUN_CODE = re.compile(r"(запусти|запустить|выполни|выполнить|run|execute|debug|исправь|"
                       r"что\s+выводит|проверь\s+код)", re.I)
_CODE_HINT = re.compile(r"(^|\s)(python|javascript|typescript|java|c\+\+|bash|shell|html|css|"
                        r"```|import\s|def\s|class\s|print\(|console\.log|\bкод\b|скрипт)",
                       re.I)
#: Запросы про репозитории, структуру проекта, файлы, терминал, MCP или поиск
#: всегда обрабатывает агент с инструментами, а не прямой вызов генератора медиа.
_NON_MEDIA_TASK_RE = re.compile(
    r"(?<![\wа-яё])(?:структур\w*|в\s+виде\s+дерев\w*|"
    r"дерев\w*\s+(?:файл|папок|каталог|директор|проект|репозитор)|"
    r"репозитори\w*|github|гитхаб\w*|mcp|терминал\w*|команд\w*|"
    r"файл\w*|папк\w*|каталог\w*|директори\w*|ветк\w*|коммит\w*|"
    r"проанализир\w*|разбер\w*|найд(?:и|ите)|поищ(?:и|ите)|"
    r"скача\w*|склонир\w*|депло\w*|deploy)(?![\wа-яё])",
    re.I,
)
_URL_IN_TEXT = re.compile(r"(?:https?://|www\.)\S+", re.I)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_PATH_OR_SLUG = re.compile(r"(?:^|(?<=\s))[\w.-]+/[\w./-]+")
_IMAGE_NOUN = re.compile(r"(изображени|картинк|фото|иллюстрац|постер|арт\b|image|picture|photo|"
                         r"illustration|poster)", re.I)
_VIDEO_NOUN = re.compile(r"(видео|ролик|клип|video|clip|анимаци|animation|gif)", re.I)
_AUDIO_NOUN = re.compile(r"(аудио|озвучк|голос|музык|audio|voice|music|speech|mp3)", re.I)
#: Признаки разработки: приложение/сайт/программа важнее слова «анимация».
_APP_NOUN = re.compile(r"(приложени|сайт|программ|скрипт|бот[аеу]?\b|игр[уыае]?\b|сервис|"
                       r"интерфейс|дашборд|app\b|application|website|program|script|game\b|"
                       r"python|flask|django|tkinter|pygame|html|css|react)", re.I)
_FENCED = re.compile(r"```.*?```", re.S)

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

#: Признаки того, что английский текст — не ответ, а рабочие заметки модели:
#: она говорит о пользователе, о формате ответа или о своих шагах. Без такого
#: признака английский технический ответ на русский вопрос НЕ считается
#: утёкшим рассуждением (см. ``looks_like_leaked_reasoning``).
_PROCESS_NOTE = re.compile(
    r"(?i)\b(?:the user|user (?:asks|wants|provided|supplied)|we need|i need to|"
    r"i should|let me|let's|respond (?:in|with)|answer in|in russian|russian language|"
    r"sentence|sentences|bullet points|format(?:ted)? as|draft|first,? i|"
    r"step \d|analysis|okay,? (?:so|let)|alright)\b")

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

#: Длинный ответ, оборванный на лимите токенов, НЕ выбрасываем целиком.
#: Короткие обрывки — это утечка рассуждений, их по-прежнему отбрасываем.
MIN_TRUNCATED_KEEP_CHARS = 240

#: Граница законченного предложения: до неё обрезаем «хвост» оборванного ответа.
_SENTENCE_END = re.compile(r"[.!?:;…]+(?=\s|$)")
_FENCED_CODE = re.compile(r"```.*?```", re.S)

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

def _prose_without_code(text):
    """Текст без блоков кода: кавычки и слова внутри кода не признак обрыва."""
    return _FENCED_CODE.sub(" ", text or "")


def _looks_complete_answer(text):
    """Похож ли текст на законченный ответ, а не на обрезанный черновик.
    Точка в конце не обязательна (модели часто её опускают). Обрыв — это
    запятая/тире/двоеточие/открытая скобка или кавычка на конце, а также
    «висящее» служебное слово БЕЗ единого знака конца предложения в последнем
    абзаце.
    Раньше правило было строже: любой текст, оканчивающийся на предлог или
    союз, считался обрывом и выбрасывался целиком. Из-за этого нормальные
    ответы вида «… чаще берут Python или JavaScript» исчезали из чата (пустой
    пузырь и «Не удалось получить ответ»). Теперь решение принимается по
    признакам обрыва, а не по одному слову.
    """
    text = (text or "").strip()
    if not text:
        return False
    if text.count("```") % 2:           # незакрытый блок кода — явный обрыв
        return False
    prose = _prose_without_code(text)
    if prose.count('"') % 2:            # открытая кавычка — ответ оборван
        return False
    if prose.count("«") != prose.count("»"):
        return False
    if text[-1] in _BAD_ENDINGS:
        return False
    if len(text) <= 40:                 # «42», «Готово», короткая реплика
        return True
    last = text.splitlines()[-1].lstrip()
    if last.startswith(("-", "*", "•", "|")) or re.match(r"^\d+[.)]\s", last):
        return True
    tail = text[-200:]
    if re.search(r"[.!?…)\]]\s*$", tail):
        return True                     # ответ закончился знаком препинания
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", text)
    if words and words[-1].lower() in _TRUNCATED_WORDS:
        # Служебное слово на конце подозрительно только тогда, когда в самом
        # хвосте нет ни одного законченного предложения.
        if not re.search(r"[.!?…]", text[-80:]):
            return False
    return True


def repair_truncated_answer(text, goal=""):
    """Спасает длинный ответ, оборванный на лимите токенов.

    Возвращает ``""`` для коротких обрывков и утечек рассуждений (их лечит
    :func:`_recover_answer`), а для длинного текста — его завершённую часть:
    всё до последнего полного предложения. Движок использует её как основу
    и дописывает ответ до конца отдельным запросом к модели.
    """
    text = strip_think(text)
    if not text or len(text) < MIN_TRUNCATED_KEEP_CHARS:
        return ""
    if looks_like_leaked_reasoning(text):
        return ""
    if _looks_complete_answer(text):
        return ""
    body = text
    if body.count("```") % 2:           # закрываем блок кода, чтобы ответ не рвался
        body += "\n```"
    ends = list(_SENTENCE_END.finditer(body))
    if ends:
        head = body[:ends[-1].end()].strip()
    else:
        head = re.sub(r"\s+\S*$", "", body).strip()
    head = head.rstrip("".join(sorted(_BAD_ENDINGS)))
    if len(head) < MIN_TRUNCATED_KEEP_CHARS:
        return ""
    if not _looks_complete_answer(head):
        head = head.rstrip() + "."
    return head


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


_MULTIAGENT_HINT = re.compile(
    r"(?i)(?:^\s*/multi\b|мультиагент\w*|нескольк\w*\s+(?:ии[- ]?)?агент\w*|"
    r"спроси\s+нескольк\w*\s+агент\w*|команд\w*\s+агент\w*|multi[- ]?agent)"
)


def requested_multiagent_pattern(text: str) -> str:
    """Explicit user request for several independent agents + synthesis."""
    return "parallel" if _MULTIAGENT_HINT.search(str(text or "")) else ""


def requested_media_kind(text: str):
    """Вид медиа, если пользователь ЯВНО просит сгенерировать картинку/видео/аудио.

    История двух ошибок (обе видны в журнале с телефона):

    * «Запусти этот код … и покажи, что он выводит» → сгенерировалась картинка:
      слово «покажи» считалось глаголом генерации, а внутри кода было
      `draw_square`, что давало «картинку». Теперь код сначала вырезается, а без
      явного объекта («картинка», «видео», «аудио») генерации нет;
    * «создай приложение с красивой анимацией» → уходило в генерацию видео
      из-за слова «анимация». Теперь при упоминании приложения/сайта/игры это
      разработка, а не видео.

    Возвращает ``"image"``, ``"video"``, ``"audio"`` или ``None`` (обычный ход).
    """
    raw = str(text or "")
    # Код, ссылки и пути (`user-sketch/repo`, `https://…`, `` `render()` ``) —
    # это данные задачи, а не просьба нарисовать картинку.
    prose = _FENCED.sub(" ", raw)
    prose = _INLINE_CODE.sub(" ", prose)
    if _URL_IN_TEXT.search(prose):
        return None
    prose = _URL_IN_TEXT.sub(" ", prose)
    prose = _PATH_OR_SLUG.sub(" ", prose)
    if _CODE_HINT.search(prose):
        prose = " ".join(line for line in prose.splitlines()
                         if not re.search(r"[=;{}()\[\]]|^\s{2,}\S", line))
    if not prose.strip():
        return None

    if _NON_MEDIA_TASK_RE.search(prose):
        return None

    if _RUN_CODE.search(prose) and not _IMAGE_NOUN.search(prose) \
            and not _VIDEO_NOUN.search(prose) and not _AUDIO_NOUN.search(prose):
        return None            # «запусти и покажи вывод» — это про код

    if _VOICE_VERB.search(prose):
        return "audio"
    if _DRAW_VERB.search(prose):
        return "image"

    build_verb = bool(_CREATE_VERB.search(prose) or _SHOW_VERB.search(prose))
    if not build_verb:
        return None
    if _AUDIO_NOUN.search(prose) and not _APP_NOUN.search(prose):
        return "audio"
    if _VIDEO_NOUN.search(prose) and not _APP_NOUN.search(prose):
        return "video"
    if _IMAGE_NOUN.search(prose) and not _APP_NOUN.search(prose):
        return "image"
    # «Создай приложение с анимацией», «сделай сайт с красивыми картинками» —
    # это разработка: медиа-инструмент тут только помешает.
    return None


def tools_enabled():
    """Инструменты терминала и файлов — любому вошедшему при включённом окружении.

    Флаг читаем в момент вызова, а не на импорте: .env может подгрузиться позже.
    """
    return os.getenv("TERMINAL_ENABLED", "0") == "1" and can_use_workspace()


def _bool_flag(value, default=True) -> bool:
    """Мягкий разбор булева флага из JSON/форм (true/1/«on» → True)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "да", "вкл"):
        return True
    if text in ("0", "false", "no", "off", "нет", "выкл", ""):
        return False
    return default


def request_tools_allowed(data) -> bool:
    """Кнопка «Автопоиск» — главный выключатель ВСЕХ инструментов агента.

    Фронтенд шлёт ``tools`` (и дублирует старым ``search``). Если клиент явно
    выключил авто-поиск, движок не регистрирует ни поиск, ни терминал, ни MCP,
    ни медиа: роутер просто отправляет запрос в ИИ-модель. Клиенты без флага
    (Telegram, планировщик, старый API) работают как раньше — с инструментами.
    """
    for key in ("tools", "auto_search", "search"):
        if key in (data or {}):
            return _bool_flag(data[key], default=True)
    return True


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
    # текст с местоимениями/служебными словами похож на черновик. Но одного
    # этого мало: свободные модели регулярно отвечают по-английски на русский
    # технический вопрос, и раньше такой ответ удалялся целиком — в чате
    # оставался пустой пузырь. Черновик обязан ещё и говорить о процессе
    # («the user asks», «let me», «in russian»).
    # Код и разметку не трогаем: ответ может начинаться с блока кода.
    paragraphs = _split_paragraphs(text)
    first = paragraphs[0] if paragraphs else text
    if first.lstrip().startswith(_CODE_START):
        return False
    cyr, lat = len(_CYR.findall(first)), len(_LAT.findall(first))
    words = re.findall(r"[A-Za-z']+", first)
    stops = sum(1 for word in words if word.lower().strip("'") in _EN_STOP)
    if not (lat >= 60 and cyr < 12 and stops >= 8 and " " in first):
        return False
    return bool(_PROCESS_NOTE.search(text[:1200]))


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
    # Иначе берём всё, что идёт после последнего абзаца-черновика. Раньше
    # собирался только непрерывный хвост русских абзацев: длинный ответ,
    # заканчивавшийся кодом или английским примером, обрезался до пары строк
    # (или вообще терялся). Теперь код, списки и латиница внутри готового
    # ответа остаются на месте — убираются только сами рассуждения.
    paragraphs = _split_paragraphs(text)
    last_draft = -1
    for index, paragraph in enumerate(paragraphs):
        if _is_draft_paragraph(paragraph):
            last_draft = index
    answer = "\n\n".join(paragraphs[last_draft + 1:]).strip()
    if (len(answer) >= 40 and not looks_like_leaked_reasoning(answer)
            and _looks_complete_answer(answer)):
        return answer
    return ""


def _is_draft_paragraph(paragraph: str) -> bool:
    """Похож ли абзац на рассуждение модели, а не на готовый ответ."""
    if paragraph.lstrip().startswith(_CODE_START):
        return False                     # код/разметка — часть ответа
    if paragraph[:1] in "\"'«“„":
        return True
    if _LEAK_START.match(paragraph) or _LEAK_START_RU.match(paragraph):
        return True
    if _DRAFT_HINT.search(paragraph):
        return True
    if looks_like_leaked_reasoning(paragraph):
        return True
    cyr, lat = len(_CYR.findall(paragraph)), len(_LAT.findall(paragraph))
    return cyr < 15 and lat > cyr       # почти сплошной английский без русского


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

    Два правила, выведенных из жалоб «в чате почти ничего нет»:

    * английский технический ответ на русский вопрос — это ответ, а не
      утёкшее рассуждение (решение принимает ``looks_like_leaked_reasoning``);
    * ответ без точки в конце не считается обрывом, если он не заканчивается
      служебным словом без единого знака конца предложения.

    Если после очистки текста не осталось, возвращается пустая строка: движок
    сделает один строгий повтор и, если он тоже пуст, честно скажет об этом.
    Пустой пузырь при этом не появляется.
    """
    text = strip_think(raw)
    if not text:
        return ""
    if _LEGACY_ACTION.match(text):
        return ""
    if re.match(r"(?is)^\s*(?:<tool_code>|<tool_call>|<web_search>|<web_open>)", text):
        return ""
    answer = strip_leaked_reasoning(text, goal)
    if answer and not _looks_complete_answer(answer):
        # Длинный ответ, оборванный на лимите токенов, не выбрасываем:
        # сохраняем его завершённую часть, а движок допишет хвост.
        return repair_truncated_answer(answer, goal)
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
    # «Вспоминаю инструменты / MCP-серверы» и размышление вокруг каждого шага —
    # в трассе сохраняем только завершённые строки, без промежуточной анимации.
    if kind == "recall" and event.get("done") and event.get("phase") != "ready":
        return [{"kind": "step", "icon": event.get("icon") or "🧰",
                 "text": clip(str(event.get("text") or ""), 300)}]
    if kind == "think" and event.get("done"):
        return [{"kind": "thought", "label": clip(str(event.get("label") or "Думал"), 120),
                 "text": clip(str(event.get("text") or ""), 400)}]
    if kind == "step":
        return [{"kind": "step", "icon": event.get("icon") or "•",
                 "text": clip(str(event.get("text") or ""), 300)}]
    if kind == "tool":
        entry = {"kind": "tool", "name": event.get("name") or "tool",
                 "command": clip(str(event.get("command") or ""), 300),
                 "code": event.get("code")}
        if event.get("label"):
            entry["label"] = clip(str(event.get("label")), 120)
        if event.get("detail"):
            entry["detail"] = clip(str(event.get("detail")), 200)
        if event.get("icon"):
            entry["icon"] = str(event.get("icon"))[:16]
        if event.get("status") == "error" or (event.get("code") not in (None, 0)):
            err_msg = str(event.get("error") or event.get("output") or "").strip()
            err_msg = re.sub(r"^\[failure\]\s*", "", err_msg, flags=re.I).strip()
            if err_msg:
                entry["error"] = clip(err_msg, 500)
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
        # База знаний принадлежит владельцу (включая гостя): RAG-поиск не должен
        # показывать чужие документы, но и гость имеет право на свои.
        self.knowledge_owner = _identity.chat_owner() or owner
        self.knowledge_available = self._knowledge_ready(self.knowledge_owner)
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
    def _knowledge_ready(owner: str) -> bool:
        """Есть ли в базе знаний хотя бы один документ этого владельца."""
        try:
            import knowledge
            return bool(knowledge.default_base().stats(owner=owner).get("ready"))
        except Exception:  # noqa: BLE001 — RAG необязателен
            log.debug("knowledge недоступна", exc_info=True)
            return False

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
        "admin": bool(terminal_routes.is_admin()),
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
    return _identity.chat_owner() if signed_in() else ""


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


# ───────────────────── ограждения, права, трассировка ─────────────────────

#: Сколько задач сочинённых моделью символов сохранять в трассе хода.
_TRACE_TEXT = 300


def _refusal_response(verdict, goal=""):
    """Ответ на заблокированный вход: честный отказ + событие ограждения.

    Возвращаем тот же NDJSON, что и обычный ход, чтобы интерфейс не показывал
    «ошибку сети»: пользователь видит пузырь с объяснением и причиной.
    """
    reasons = ", ".join(verdict.reasons) or "запрос похож на попытку обойти инструкции"
    reply = ("Не могу выполнить этот запрос: он выглядит как попытка изменить мои "
             "правила или получить служебные данные.\n\n"
             f"Причина: {reasons}.\n\n"
             "Спросите по существу задачи — помогу с кодом, поиском, файлами и документами.")

    def generate():
        yield _ndjson(verdict.event("input"))
        yield _ndjson({"type": "step", "icon": "🛡", "text": "Ограждение остановило запрос"})
        yield _ndjson({"type": "token", "token": reply})
        yield _ndjson({"type": "result", "reply": reply, "steps": 0, "sources": [],
                       "searched": False, "media": [], "tools": {}, "linux": False,
                       "status": "blocked", "engine": "guardrails",
                       "guard": {"reasons": list(verdict.reasons)}})
        yield _ndjson({"type": "done"})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "X-Agent-Engine": "guardrails"})


def _run_tool_scope(*, tools_allowed: bool, use_tools: bool, signed: bool,
                    admin: bool, media_ready: dict) -> list[str]:
    """Набор инструментов, который реально подключён к этому ходу.

    Именно он попадает в capability-токен: лишнего в правах нет, поэтому
    «вспомнить» недоступный инструмент из промпта или навыка нельзя.
    """
    scope: list[str] = []
    if tools_allowed:
        scope.extend(["web_search", "web_open", "knowledge_search"])
    if use_tools:
        scope.extend(["terminal", "python", "file_read", "file_write",
                      "file_edit", "file_find"])
    if signed:
        scope.extend(["recall", "remember", "forget", "task", "task_done",
                      "skill_create"])
    if admin:
        scope.extend(["mcp_call", "schedule"])
    scope.extend(kind for kind, ready in (media_ready or {}).items() if ready)
    return sorted(set(scope))


def _guarded_finalize(text: str, goal: str, notes: list) -> str:
    """Финальный текст: очистка рассуждений + выходные ограждения.

    ``notes`` пополняется вердиктами: маршрут отдаёт их в поток как события
    ``guard``, а трасса сохраняет типы сработавших правил.
    """
    answer = final_answer_text(text, goal)
    if not answer:
        return ""
    try:
        import config
        verdict = guardrails.check_output(answer, system_prompt=getattr(config, "system_prompt", ""))
    except Exception:                                   # noqa: BLE001 — фильтр не должен есть ответ
        log.warning("output guard failed", exc_info=True)
        return answer
    if verdict.severity != guardrails.OK:
        notes.append(verdict)
    return verdict.text or answer


class _TraceRecorder:
    """Переводит события NDJSON в спаны трассы (шаг/инструмент/подтверждение)."""

    SPAN_KINDS = {"tool": "tool", "step": "step", "plan": "plan", "stage": "stage",
                  "recall": "recall", "think": "think", "thought": "thought",
                  "confirm": "confirm", "guard": "guard", "capability": "capability",
                  "sources": "sources", "media": "media", "error": "error"}

    def __init__(self, trace, run_id: str = "", chat_id: str = ""):
        self.trace = trace
        self.run_id = run_id
        self.chat_id = chat_id
        self.last = time.time()
        self.tokens = 0
        self.guards = 0
        self.tools: list[str] = []
        self.errors: list[str] = []

    def feed(self, event: dict) -> None:
        if self.trace is None or not isinstance(event, dict):
            return
        kind = str(event.get("type") or "")
        now = time.time()
        gap = max(0.0, now - self.last)
        self.last = now
        if kind == "token":
            self.tokens += len(str(event.get("token") or ""))
            return
        if kind == "guard":
            self.guards += 1
        if kind == "error":
            self.errors.append(clip(str(event.get("text") or ""), 200))
        span_kind = self.SPAN_KINDS.get(kind)
        if not span_kind:
            return
        name = f"{span_kind}:{event.get('name')}" if kind == "tool" else span_kind
        attributes = {
            "text": clip(str(event.get("text") or event.get("title") or ""), _TRACE_TEXT),
            "icon": str(event.get("icon") or "")[:8],
        }
        for key in ("name", "status", "scene", "phase", "tool", "severity", "allowed",
                    "command", "path", "code", "reasons", "done"):
            if event.get(key) is not None:
                attributes[key] = event.get(key)
        span = self.trace.span(name, kind=span_kind, attributes=attributes)
        span.started = now - gap
        span.finish("error" if (kind == "error" or event.get("status") == "error") else "ok")
        if kind == "tool" and event.get("name"):
            self.tools.append(str(event.get("name")))

    def finish(self, result_event: dict | None, answer: str, status: str) -> None:
        if self.trace is None:
            return
        result_event = result_event or {}
        self.trace.finish(status, {
            "answer_chars": len(answer or ""),
            "steps": result_event.get("steps"),
            "tools_used": self.tools[:20],
            "sources": len(result_event.get("sources") or []),
            "media": len(result_event.get("media") or []),
            "tokens_streamed": self.tokens,
            "guard_events": self.guards,
            "errors": self.errors[:5],
            "provider": result_event.get("provider"),
            "model": result_event.get("model"),
            "offline": result_event.get("offline"),
        })
        telemetry.save_trace(self.trace)


@agent_bp.route("/api/agent/stream", methods=["POST"])
def agent_stream():
    """Ход агента: ограждения → права → план → инструменты → ответ. NDJSON."""
    data = request.get_json(silent=True) or {}
    goal = (data.get("message") or "").strip()
    if not goal:
        return jsonify({"error": "Пустое сообщение"}), 400
    if not is_enabled():
        return jsonify({"error": "Агентный режим выключен (AGENT_ENABLED=1)"}), 403

    # 1. Ограждение входа: нормализация текста и отказ от инъекций.
    guard_in = guardrails.check_input(goal)
    if not guard_in.allowed:
        log.info("вход отклонён ограждением: %s", ",".join(guard_in.reasons))
        return _refusal_response(guard_in, goal)
    goal = guard_in.text
    requested_pattern = requested_multiagent_pattern(goal)
    if requested_pattern and re.match(r"(?i)^\s*/multi\b", goal):
        goal = re.sub(r"(?i)^\s*/multi\b\s*", "", goal).strip()
        if not goal:
            return jsonify({"error": "После /multi укажите вопрос"}), 400

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
    # Один ответ агента должен влезать в лимит провайдера, но не быть 1200
    # токенов: на таком потолке модель обрывалась на полуслове, а фильтр
    # полноты ответа отбрасывал весь текст (пользователь видел одну строку).
    answer_tokens = max(1024, min(AGENT_MAX_TOKENS, int(_cfg.get("max_tokens") or AGENT_MAX_TOKENS)))
    tools_allowed = request_tools_allowed(data)
    use_tools = tools_enabled() and tools_allowed
    strict_mcp = strict_mcp_request(goal) if tools_allowed else False
    history_key = session.get("agent_memory_key")
    if not history_key:
        history_key = secrets.token_hex(12)
        session["agent_memory_key"] = history_key
    chat_id, prior = _turn_history(history_key)
    if use_tools:
        ensure_workspace()
    # Агент и песочница работают в одной папке: иначе файлы агента не видны
    # в файловом менеджере, а команды выполняются в другом дереве.
    cap_owner = _identity.chat_owner()
    _identity.bind_owner(cap_owner)                # поток агента узнает своего владельца
    _set_agent_workspace(workspace_dir())
    # Инструменты движка исполняют команды только через эту песочницу.
    # При выключенном автопоиске песочница вообще не подключается к запуску.
    if tools_allowed:
        sandbox.set_runner(_sandbox_runner)

    run_id, cancel = register_run()
    media_kind = requested_media_kind(goal) if tools_allowed else ""
    owner = _owner_name()
    from routes.terminal import is_admin as _is_admin
    admin_flag = bool(_is_admin())
    memory_key = ""
    try:
        import memory as memory_module
        memory_key = memory_module.user_key(session, signed_in())
    except Exception:  # noqa: BLE001
        memory_key = ""

    # 2. Криптографические права хода: подписанный токен с least-privilege.
    role = "admin" if admin_flag else ("user" if signed else "guest")
    media_ready = {kind: _media_ready(kind) for kind in ("image", "audio", "video")}
    tool_scope = _run_tool_scope(tools_allowed=tools_allowed, use_tools=use_tools,
                                 signed=signed, admin=admin_flag, media_ready=media_ready)
    _cap_token, capability = capabilities.mint(owner=cap_owner, run_id=run_id,
                                               role=role, tools=tool_scope)

    # 3. Трасса: один ход — одна трасса, со всеми шагами и решениями.
    pattern = requested_pattern or (os.getenv("AGENT_PATTERN") or "off").strip().lower()
    trace = telemetry.start_trace(
        "agent.turn", run_id=run_id, chat_id=chat_id or "", provider=provider,
        model=model, engine="agenticseek", role=role, tools=use_tools,
        pattern=pattern, goal=clip(goal, 200), goal_chars=len(goal),
        prompt_versions=prompt_registry.record_use_all(),
        guardrails={"severity": guard_in.severity, "reasons": list(guard_in.reasons)},
        capability={"role": role, "tools": sorted(capability.tools),
                    "dropped": list(capability.dropped)})
    recorder = _TraceRecorder(trace, run_id=run_id, chat_id=chat_id or "")
    output_guards: list = []

    @stream_with_context
    def generate():
        started = time.time()
        cap_context = capabilities.activate(capability)
        cap_event = guard_in.event("input")
        if cap_event["severity"] != guardrails.OK:
            yield _ndjson(cap_event)

        if tools_allowed:
            tools_ctx = NovaToolsContext(goal, chat_id or "", emit=lambda event: None,
                                         signed=signed, owner=owner, memory_key=memory_key)
            tools_ctx.is_admin = admin_flag
        else:
            # Ни перечисления MCP, ни проверки медиа-бэкендов: в прямом режиме
            # capability-контекст не нужен и не должен ничего трогать.
            tools_ctx = SimpleNamespace(task_id="", emit=lambda event: None,
                                        generate_media=None, is_admin=False,
                                        knowledge_available=False,
                                        knowledge_owner=cap_owner)
        memory_block = ""
        if memory_key:
            try:
                import memory as memory_module
                memory_block = memory_module.prompt_block(memory_key, goal) or ""
            except Exception:  # noqa: BLE001 — память не должна ломать ход
                log.debug("memory block failed", exc_info=True)
        # 4. RAG: фрагменты внутренних документов с цитатами [K1].
        knowledge_block, knowledge_items = "", []
        if str(os.getenv("KNOWLEDGE_IN_PROMPT", "1")).strip() != "0":
            try:
                import knowledge as knowledge_module
                knowledge_block, knowledge_items = knowledge_module.default_base().context_block(
                    goal, limit=4, owner=cap_owner, max_chars=3000)
            except Exception:  # noqa: BLE001 — база знаний необязательна
                log.debug("knowledge block failed", exc_info=True)
        # События о найденных фрагментах отправляет движок сразу после первого
        # шага: ход должен начинаться со ``stage`` (см. Engine._knowledge_stage).
        skill_block = ""
        if tools_allowed:
            try:
                import skills_registry
                skill_block = skills_registry.prompt_context() or ""
            except Exception:  # noqa: BLE001
                log.debug("skills block failed", exc_info=True)

        engine = AgentEngine(EngineContext(
            goal=goal, complete=chat_completion, emit=lambda event: None,
            provider=provider, model=model, cancel=cancel,
            history=prior, workspace=use_tools,
            search=(nova_search if tools_allowed else None),
            read_page=(_read_page if tools_allowed else None),
            runner=(_sandbox_runner if tools_allowed else None),
            tools_ctx=(tools_ctx if tools_allowed else None),
            memory_prompt=memory_block,
            knowledge_prompt=knowledge_block,
            knowledge_items=list(knowledge_items),
            skills_prompt=(skill_block if tools_allowed else ""),
            tools_allowed=tools_allowed,
            strict_mcp=strict_mcp,
            pattern=pattern,
            finalize=lambda text: _guarded_finalize(text, goal, output_guards),
            salvage_answer=lambda text: salvage_quoted_answer(text, goal),
            repair_answer=lambda text: repair_truncated_answer(text, goal),
            max_tokens=answer_tokens,
            chat_id=chat_id or "",
            run_id=run_id, force_role=("files" if media_kind else ""),
            media_kind=media_kind or "",
            media_generate=(tools_ctx.generate_media if media_kind else None),
            local_model=_local_model,
            offline=(provider == "local_demo"),
            force_search=(bool(data.get("search")) and tools_allowed and not strict_mcp),
            save_research=(terminal_routes._save_research if use_tools else None),
            max_steps=MAX_STEPS,
        ))
        tools_ctx.emit = getattr(engine, "emit", getattr(engine, "_emit", lambda *args, **kwargs: None))
        result_event = None
        try:
            for event in engine.stream():
                if isinstance(event, dict) and event.get("type") == "result":
                    result_event = event
                yield _ndjson(event)
        finally:
            capabilities.deactivate(cap_context)
            finish_run(run_id)

        answer = engine.answer
        state = engine.state
        # 5. Выходные ограждения: секреты и эхо системного промпта.
        for verdict in output_guards:
            yield _ndjson(verdict.event("output"))
        markers = _media_markers(state.media)
        _save_turn(chat_id, history_key, goal, answer, extra_messages=markers)
        usage = usage_stats.record_turn(
            chat_id, provider, model, prompt_text=_history_text(prior),
            answer_text=answer, usage=engine.provider.usage or None)
        yield _ndjson({"type": "usage", "usage": usage})
        result = {
            "type": "result", "reply": answer, "steps": state.steps,
            "usage": usage, "task_id": tools_ctx.task_id or None,
            "model": model, "provider": provider,
            "sources": state.sources, "searched": bool(state.sources),
            "media": state.media_results, "linux": bool(state.tools and use_tools),
            "tools": state.tools, "offline": provider == "local_demo",
            "chat_id": chat_id, "run_id": run_id, "engine": "agenticseek",
            "trace_id": trace.trace_id, "role": role,
            "knowledge": knowledge_items,
            "guard": {"severity": guard_in.severity,
                      "reasons": list(guard_in.reasons),
                      "output": [violation.event("output")["reasons"]
                                 for violation in output_guards]},
            "stopped": state.status == "stopped",
            "status": "stopped" if state.status == "stopped" else
                      ("error" if state.status == "error" else "ok"),
            "error": state.error, "seconds": round(time.time() - started, 1),
        }
        # Права хода объявляем перед итогом: интерфейс показывает их в карточке
        # хода, а поток начинается с шагов агента, как и раньше.
        yield _ndjson({"type": "capability", **capability.event(), "scope": tool_scope})
        yield _ndjson(result)
        yield _ndjson({"type": "done"})
        guard_error = (guard_in.severity == guardrails.BLOCK
                       or any(violation.severity == guardrails.BLOCK
                              for violation in output_guards))
        recorder.finish(result, answer,
                        "error" if state.status == "error" else
                        ("guard" if guard_error else "ok"))

    def traced(stream):
        """Копия потока: пишет трассу шагов в базу и спаны в телеметрию."""
        collected = []
        try:
            for chunk in stream:
                collected.extend(_trace_entries(chunk))
                try:
                    recorder.feed(json.loads(chunk))
                except (TypeError, ValueError):
                    pass
                yield chunk
        finally:
            try:
                database.save_trace(chat_id, run_id, collected)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось сохранить трассу шагов", exc_info=True)
            if trace.status == "running":
                # Клиент оборвал поток: трасса всё равно должна закрыться.
                recorder.finish(None, "", "cancelled")

    return Response(traced(generate()), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "X-Agent-Run-Id": run_id,
                             "X-Agent-Engine": "agenticseek",
                             "X-Trace-Id": trace.trace_id,
                             "X-Agent-Role": role,
                             "X-RateLimit-Limit": str(limit),
                             "X-RateLimit-Remaining": str(remaining)})


def _local_model(messages):
    """Офлайн-ответчик NovaMind (local_llm) — только при provider=local_demo."""
    import local_llm
    return local_llm.complete(messages)


_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/([^/\s?#]+)/([^/\s?#]+?)(?:\.git)?"
    r"(?:/(tree|blob)/([^/\s?#]+)(?:/([^\s?#]*))?)?/?(?:\?.*)?(?:#.*)?$",
    re.I,
)


def _format_github_tree(entries: list[dict], subpath: str = "", max_entries: int = 180) -> str:
    """Формирует компактное ASCII-дерево файлов и папок из ответа GitHub git/trees API."""
    prefix = (subpath.strip("/") + "/") if subpath.strip("/") else ""
    items: list[tuple[str, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        raw_path = str(entry.get("path") or "").strip("/")
        kind = str(entry.get("type") or "blob")
        if not raw_path:
            continue
        if prefix:
            if not raw_path.startswith(prefix):
                continue
            raw_path = raw_path[len(prefix):]
            if not raw_path:
                continue
        items.append((raw_path, kind))
    if not items:
        return ""

    # Если файлов очень много — сначала приоритет верхним уровням (глубина <= 2).
    omitted = 0
    if len(items) > max_entries:
        shallow = [it for it in items if it[0].count("/") <= 1]
        deep = [it for it in items if it[0].count("/") > 1]
        selected = shallow[:max_entries]
        if len(selected) < max_entries:
            selected.extend(deep[: max_entries - len(selected)])
        omitted = len(items) - len(selected)
        items = selected

    tree: dict[str, dict] = {}
    dirs: set[str] = set()
    for rel_path, kind in items:
        parts = [p for p in rel_path.split("/") if p]
        node = tree
        cur = ""
        for idx, part in enumerate(parts):
            cur = f"{cur}/{part}" if cur else part
            is_last = idx == len(parts) - 1
            if not is_last or kind == "tree":
                dirs.add(cur)
            node = node.setdefault(part, {})

    lines: list[str] = []

    def _walk(node: dict[str, dict], parent_path: str, indent: str) -> None:
        def _sort_key(name: str):
            full = f"{parent_path}/{name}" if parent_path else name
            return (0 if full in dirs else 1, name.lower())

        keys = sorted(node.keys(), key=_sort_key)
        for idx, name in enumerate(keys):
            is_last = idx == len(keys) - 1
            full = f"{parent_path}/{name}" if parent_path else name
            is_dir = full in dirs or bool(node[name])
            connector = "└── " if is_last else "├── "
            lines.append(f"{indent}{connector}{name}{'/' if is_dir else ''}")
            if node[name]:
                next_indent = indent + ("    " if is_last else "│   ")
                _walk(node[name], full, next_indent)

    _walk(tree, "", "")
    if omitted > 0:
        lines.append(f"└── … (ещё {omitted} элементов опущено для компактности)")
    return "\n".join(lines)


def _read_page(url, max_chars=8000):
    """Чтение страницы для веб-агента (с поддержкой дерева файлов и файлов GitHub)."""
    gh = _GITHUB_URL_RE.match(str(url or "").strip())
    if gh:
        try:
            import httpx
            owner, repo, view_kind, ref, subpath = (
                gh.group(1),
                gh.group(2),
                (gh.group(3) or "").lower(),
                gh.group(4) or "",
                (gh.group(5) or "").strip("/"),
            )
            head = {"User-Agent": "NovaMind", "Accept": "application/vnd.github+json"}
            gh_token = (os.getenv("GITHUB_TOKEN") or "").strip()
            if gh_token:
                head["Authorization"] = f"Bearer {gh_token}"

            if view_kind == "blob" and ref and subpath:
                raw_resp = httpx.get(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{subpath}",
                    params={"ref": ref},
                    headers={**head, "Accept": "application/vnd.github.raw+json"},
                    timeout=15,
                    follow_redirects=True,
                )
                if raw_resp.status_code == 200:
                    header = f"GitHub файл {owner}/{repo} ({ref}) · {subpath}\n\n"
                    return (header + raw_resp.text)[:max_chars], ""

            meta_resp = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers=head,
                timeout=15,
            )
            meta = meta_resp.json() if meta_resp.status_code == 200 else {}
            branch = ref or str(meta.get("default_branch") or "main")
            title = (
                f"GitHub {owner}/{repo} (ветка: {branch}) · "
                f"звёзд: {meta.get('stargazers_count', '?')} · "
                f"язык: {meta.get('language') or '—'} · "
                f"{meta.get('description') or ''}"
            ).strip()

            sections = [title]
            tree_resp = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}",
                params={"recursive": "1"},
                headers=head,
                timeout=15,
            )
            if tree_resp.status_code == 200:
                tree_json = tree_resp.json()
                rendered_tree = _format_github_tree(tree_json.get("tree") or [], subpath=subpath)
                if rendered_tree:
                    root_label = f"{repo}/{subpath}" if subpath else f"{repo}/"
                    sections.append(f"Структура репозитория (дерево файлов):\n{root_label}\n{rendered_tree}")

            readme_url = (
                f"https://api.github.com/repos/{owner}/{repo}/readme/{subpath}"
                if subpath else f"https://api.github.com/repos/{owner}/{repo}/readme"
            )
            readme_params = {"ref": branch} if ref else None
            readme = httpx.get(
                readme_url,
                params=readme_params,
                headers={**head, "Accept": "application/vnd.github.raw+json"},
                timeout=15,
                follow_redirects=True,
            )
            if readme.status_code == 200 and readme.text.strip():
                sections.append("README:\n" + readme.text.strip()[:3000])

            if len(sections) > 1 or meta:
                return "\n\n".join(sections)[:max_chars], ""
        except Exception:
            pass
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
