import requests
import os
import json

COMPOSIO_API_KEY = lambda: os.getenv('COMPOSIO_API_KEY', '')
BASE = 'https://backend.composio.dev/api'
GROQ_API_KEY = lambda: os.getenv('GROQ_API_KEY', '')

# ✅ Проверенные slugи — точные названия из Composio API
KNOWN_SLUGS = {
    # GitHub
    'список репозиториев': 'GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER',
    'покажи репозитории': 'GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER',
    'мои репозитории': 'GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER',
    'создай репозиторий': 'GITHUB_CREATE_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER',
    'новый репозиторий': 'GITHUB_CREATE_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER',
    'создать репо': 'GITHUB_CREATE_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER',
    'создай issue': 'GITHUB_CREATE_AN_ISSUE',
    'новый issue': 'GITHUB_CREATE_AN_ISSUE',
    'список issue': 'GITHUB_LIST_ISSUES',
    'покажи issue': 'GITHUB_LIST_ISSUES',
    'список веток': 'GITHUB_LIST_BRANCHES',
    'покажи ветки': 'GITHUB_LIST_BRANCHES',
    'создай ветку': 'GITHUB_CREATE_A_BRANCH',
    'мой профиль': 'GITHUB_GET_THE_AUTHENTICATED_USER',
    'профиль github': 'GITHUB_GET_THE_AUTHENTICATED_USER',
    'форк': 'GITHUB_CREATE_A_FORK',
    'создай форк': 'GITHUB_CREATE_A_FORK',
    'коммит': 'GITHUB_CREATE_A_COMMIT',
    'список коммитов': 'GITHUB_LIST_COMMITS',
    'прочитай файл': 'GITHUB_GET_REPOSITORY_CONTENT',
    'содержимое файла': 'GITHUB_GET_REPOSITORY_CONTENT',
    'создай файл': 'GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS',
    'звёзды': 'GITHUB_IS_REPOSITORY_STARRED_BY_THE_USER',
    'поиск репозиториев': 'GITHUB_FIND_REPOSITORIES',
    'найди репозиторий': 'GITHUB_FIND_REPOSITORIES',
    # Gmail
    'отправь письмо': 'GMAIL_SEND_EMAIL',
    'отправь email': 'GMAIL_SEND_EMAIL',
    'письма': 'GMAIL_FETCH_EMAILS',
    'входящие': 'GMAIL_FETCH_EMAILS',
    'прочитай письма': 'GMAIL_FETCH_EMAILS',
    'покажи письма': 'GMAIL_FETCH_EMAILS',
    'черновик': 'GMAIL_CREATE_EMAIL_DRAFT',
    # Notion
    'создай страницу': 'NOTION_CREATE_PAGE',
    'новая страница': 'NOTION_CREATE_PAGE',
    'поиск notion': 'NOTION_SEARCH',
    'найди в notion': 'NOTION_SEARCH',
    # Slack
    'сообщение slack': 'SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL',
    'напиши в slack': 'SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL',
    'каналы slack': 'SLACK_LIST_CHANNELS',
    # Google Calendar
    'создай событие': 'GOOGLECALENDAR_CREATE_EVENT',
    'мои события': 'GOOGLECALENDAR_LIST_EVENTS',
    'календарь': 'GOOGLECALENDAR_LIST_EVENTS',
}


def headers():
    return {'x-api-key': COMPOSIO_API_KEY(), 'Content-Type': 'application/json'}


def groq_call(messages, max_tokens=800, temperature=0.1):
    key = GROQ_API_KEY()
    if not key:
        return None
    try:
        resp = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json={
                'model': 'llama-3.3-70b-versatile',
                'messages': messages,
                'temperature': temperature,
                'max_tokens': max_tokens
            },
            timeout=30
        )
        if resp.ok:
            return resp.json()['choices'][0]['message']['content'].strip()
    except:
        pass
    return None


def parse_json_safe(text):
    if not text:
        return None
    if '```' in text:
        parts = text.split('```')
        for part in parts:
            part = part.strip()
            if part.startswith('json'):
                part = part[4:]
            try:
                return json.loads(part.strip())
            except:
                continue
    try:
        start = text.index('{')
        end = text.rindex('}') + 1
        return json.loads(text[start:end])
    except:
        pass
    return None


def get_active_accounts():
    try:
        resp = requests.get(f'{BASE}/v3.1/connected_accounts', headers=headers(), timeout=10)
        resp.raise_for_status()
        return [a for a in resp.json().get('items', []) if a.get('status') == 'ACTIVE']
    except:
        return []


def get_toolkit_slug(account):
    t = account.get('toolkit', {})
    return t.get('slug', '') if isinstance(t, dict) else str(t)


def check_slug_exists(slug):
    """Проверяем slug — 404 значит не существует"""
    try:
        resp = requests.post(
            f'{BASE}/v3.1/tools/execute/{slug}',
            headers=headers(),
            json={'arguments': {}, 'user_id': 'novauser'},
            timeout=10
        )
        return resp.status_code != 404
    except:
        return False


def find_tool_slug(task, toolkit=''):
    """
    3 уровня поиска:
    1. Словарь известных slugов
    2. Groq угадывает + проверка
    3. Возврат Groq slug без проверки
    """
    task_lower = task.lower()

    # Уровень 1: словарь известных slugов
    for key, slug in KNOWN_SLUGS.items():
        if key in task_lower:
            print(f"[Composio] Словарь: {slug}")
            return slug

    # Уровень 2: Groq угадывает slug
    accounts = get_active_accounts()
    toolkits_str = ', '.join(set([get_toolkit_slug(a) for a in accounts]))

    prompt = f"""Ты эксперт по Composio API. Знаешь все slugи наизусть.

Подключённые тулкиты: {toolkits_str}
Задача: {task}

Назови ТОЧНЫЙ slug Composio инструмента.

Важные примеры точных slugов:
- GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER
- GITHUB_CREATE_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER
- GITHUB_CREATE_AN_ISSUE
- GITHUB_LIST_ISSUES
- GITHUB_LIST_BRANCHES
- GITHUB_CREATE_A_BRANCH
- GITHUB_GET_THE_AUTHENTICATED_USER
- GITHUB_CREATE_A_FORK
- GITHUB_FIND_REPOSITORIES
- GITHUB_GET_REPOSITORY_CONTENT
- GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS
- GMAIL_SEND_EMAIL
- GMAIL_FETCH_EMAILS
- GMAIL_CREATE_EMAIL_DRAFT
- SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL
- SLACK_LIST_CHANNELS
- NOTION_CREATE_PAGE
- NOTION_SEARCH
- GOOGLECALENDAR_CREATE_EVENT
- GOOGLECALENDAR_LIST_EVENTS

Ответь ТОЛЬКО slug, без объяснений."""

    result = groq_call([{"role": "user", "content": prompt}], max_tokens=60)
    if result:
        slug = result.strip().upper().split()[0]
        print(f"[Composio] Groq предлагает: {slug}")

        # Проверяем существует ли
        if check_slug_exists(slug):
            print(f"[Composio] ✅ Slug подтверждён")
            return slug
        else:
            print(f"[Composio] ⚠️ Slug не найден, используем как есть")
            return slug  # всё равно пробуем — может параметры не те

    return None


def execute_tool(tool_name, params, user_id='novauser'):
    """
    ✅ ГЛАВНОЕ ИСПРАВЛЕНИЕ: НЕ передаём connected_account_id!
    Composio сам выбирает правильный аккаунт по user_id
    """
    try:
        payload = {
            'arguments': params if params else {},
            'user_id': user_id
        }

        resp = requests.post(
            f'{BASE}/v3.1/tools/execute/{tool_name}',
            headers=headers(),
            json=payload,
            timeout=30
        )
        print(f'[Composio] execute {tool_name} → {resp.status_code}')

        if resp.ok:
            return resp.json()
        try:
            return {'error': resp.json(), 'successful': False}
        except:
            return {'error': resp.text[:300], 'successful': False}
    except Exception as e:
        return {'error': str(e), 'successful': False}


def ai_extract_params(task, tool_name, prev_result=''):
    """Groq извлекает параметры"""
    prev_str = f"\nРезультат предыдущего шага:\n{prev_result[:500]}" if prev_result else ""

    prompt = f"""Задача: {task}
Инструмент Composio: {tool_name}
{prev_str}

Извлеки параметры из задачи для этого инструмента.

Примеры параметров:
- GITHUB_CREATE_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER: {{"name": "repo-name", "description": "...", "private": false}}
- GITHUB_CREATE_AN_ISSUE: {{"owner": "username", "repo": "repo-name", "title": "...", "body": "..."}}
- GITHUB_LIST_BRANCHES: {{"owner": "username", "repo": "repo-name"}}
- GITHUB_FIND_REPOSITORIES: {{"q": "search query"}}
- GMAIL_SEND_EMAIL: {{"recipient_email": "test@mail.com", "subject": "...", "body": "..."}}
- GMAIL_FETCH_EMAILS: {{"max_results": 5}}
- SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL: {{"channel": "general", "text": "..."}}
- NOTION_CREATE_PAGE: {{"title": "...", "content": "..."}}
- GOOGLECALENDAR_CREATE_EVENT: {{"summary": "...", "start_datetime": "2026-06-24T10:00:00", "end_datetime": "2026-06-24T11:00:00"}}

Ответь ТОЛЬКО JSON: {{"params": {{"key": "value"}}}}
Если параметр неизвестен — не включай его."""

    result = groq_call([{"role": "user", "content": prompt}], max_tokens=300)
    parsed = parse_json_safe(result)
    return parsed.get('params', {}) if parsed else {}


def ai_format_result(task, tool_name, result):
    """Groq красиво форматирует результат"""
    result_str = json.dumps(result, ensure_ascii=False)[:2000] if isinstance(result, (dict, list)) else str(result)[:2000]

    prompt = f"""Задача была: {task}
Инструмент: {tool_name}
Результат от API:
{result_str}

Напиши красивый понятный ответ на русском языке.
Покажи реальные данные — имена, ссылки, даты.
Используй эмодзи и Markdown."""

    formatted = groq_call([{"role": "user", "content": prompt}], max_tokens=600, temperature=0.3)
    return formatted or format_raw(result, tool_name)


def format_raw(result, tool_name):
    if not result:
        return '❌ Пустой ответ'
    if isinstance(result, dict) and not result.get('successful', True):
        err = result.get('error', 'неизвестная ошибка')
        msg = json.dumps(err, ensure_ascii=False) if isinstance(err, dict) else str(err)
        return f'❌ Ошибка: {msg[:400]}'
    output = result.get('response_data') or result.get('data') or result.get('result') or result
    if isinstance(output, list):
        lines = []
        for item in output[:10]:
            if isinstance(item, dict):
                name = item.get('full_name') or item.get('name') or item.get('title') or str(item)[:60]
                url = item.get('html_url', '')
                lines.append(f'• **{name}**' + (f' 🔗 {url}' if url else ''))
            else:
                lines.append(f'• {str(item)[:80]}')
        return f'✅ {len(output)} результатов:\n' + '\n'.join(lines)
    if isinstance(output, dict):
        return f'✅ **{tool_name}**:\n```json\n{json.dumps(output, ensure_ascii=False, indent=2)[:800]}\n```'
    return f'✅ {str(output)[:600]}'


def run_agent(task):
    """Главный агентный цикл"""
    key = COMPOSIO_API_KEY()
    if not key:
        return '❌ Добавь COMPOSIO_API_KEY в .env!'

    accounts = get_active_accounts()
    if not accounts:
        return '❌ Нет подключённых аккаунтов!\nПодключи: `/composio auth github`'

    log = f'🤖 **NovaMind Agent**\n\n'
    log += f'📋 Задача: _{task}_\n'
    log += f'🔗 Аккаунтов: {len(accounts)}\n\n'
    log += '─' * 30 + '\n\n'

    # Шаг 1: Находим инструмент
    log += '**🧠 Выбираю инструмент...**\n'
    tool_name = find_tool_slug(task)

    if not tool_name:
        return log + '❌ Не смог определить инструмент\n\nПопробуй точнее описать задачу'

    log += f'🛠️ Инструмент: **{tool_name}**\n\n'

    # Шаг 2: Извлекаем параметры
    params = ai_extract_params(task, tool_name)
    if params:
        log += f'⚙️ Параметры: `{json.dumps(params, ensure_ascii=False)}`\n\n'
    else:
        log += f'⚙️ Параметры: не нужны\n\n'

    # Шаг 3: Выполняем БЕЗ connected_account_id
    log += f'**🚀 Выполняю...**\n\n'
    result = execute_tool(tool_name, params)

    # Проверяем ошибку
    if isinstance(result, dict) and not result.get('successful', True):
        error = result.get('error', '')
        err_str = json.dumps(error, ensure_ascii=False) if isinstance(error, dict) else str(error)

        # Если ошибка параметров — показываем подсказку
        if 'missing' in err_str.lower():
            missing = err_str
            log += f'⚠️ Не хватает параметров: `{missing[:200]}`\n\n'
            log += f'💡 Попробуй уточнить запрос, например:\n'
            log += f'`/composio do создай репозиторий МОЙ-ПРОЕКТ на GitHub`'
            return log

        return log + f'❌ Ошибка: `{err_str[:300]}`'

    # Шаг 4: Форматируем результат
    formatted = ai_format_result(task, tool_name, result)
    log += f'**Результат:**\n\n{formatted}'
    return log


def run(args):
    if not args:
        return """🤖 **NovaMind Composio Agent**

Поддерживает **1000+ тулкитов** и **тысячи инструментов**!

**Команды:**
`/composio do <задача>` — выполнить (AI сам найдёт инструмент)
`/composio accounts` — мои аккаунты
`/composio tools <toolkit>` — инструменты тулкита
`/composio auth <toolkit>` — подключить интеграцию
`/composio execute <SLUG>` — выполнить напрямую
`/composio list` — все интеграции
`/composio search <название>` — поиск интеграции

**Примеры:**
`/composio do покажи мои репозитории на GitHub`
`/composio do создай репозиторий my-project на GitHub`
`/composio do прочитай последние письма Gmail`
`/composio do отправь письмо на test@mail.com тема Привет`
`/composio do создай issue в репозитории NovaMind`"""

    cmd = args[0].lower()

    if cmd == 'do':
        if len(args) < 2:
            return '❌ Опиши задачу!\nПример: `/composio do покажи мои репозитории`'
        task = ' '.join(args[1:])
        return run_agent(task)

    elif cmd == 'accounts':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ Добавь COMPOSIO_API_KEY в .env'
        accounts = get_active_accounts()
        if not accounts:
            return '📭 Нет аккаунтов\n\nПодключи: `/composio auth github`'
        result = f'👤 **Подключённые аккаунты** ({len(accounts)}):\n\n'
        for acc in accounts:
            slug = get_toolkit_slug(acc)
            uid = acc.get('user_id', '?')
            result += f'✅ **{slug.upper()}** (user: `{uid}`)\n'
            result += f'   🛠️ `/composio tools {slug}`\n'
            result += f'   🤖 `/composio do задача через {slug}`\n\n'
        return result

    elif cmd == 'tools':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        if len(args) < 2:
            return '❌ `/composio tools github`'
        toolkit = args[1].lower()
        try:
            all_items = []
            for page in range(1, 4):
                resp = requests.get(
                    f'{BASE}/v3.1/tools?toolkit_slug={toolkit}&limit=50&page={page}',
                    headers=headers(), timeout=10
                )
                if not resp.ok:
                    break
                items = resp.json().get('items', [])
                if not items:
                    break
                all_items.extend(items)
                if len(items) < 50:
                    break
            if not all_items:
                return f'🛠️ Инструменты для **{toolkit}** не найдены'
            result = f'🛠️ **{toolkit.upper()}** — {len(all_items)} инструментов:\n\n'
            for action in all_items[:25]:
                slug = action.get('slug') or '?'
                desc = (action.get('description') or '')[:65]
                result += f'• `{slug}`\n  _{desc}_\n\n'
            if len(all_items) > 25:
                result += f'_...и ещё {len(all_items)-25} инструментов_\n'
            result += f'\n🤖 Используй: `/composio do <задача>`'
            return result
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    elif cmd == 'auth':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        if len(args) < 2:
            return '❌ `/composio auth github`'
        toolkit = args[1].lower()
        try:
            sess_resp = requests.post(
                f'{BASE}/v3.1/tool_router/session',
                headers=headers(),
                json={"user_id": "novauser"},
                timeout=10
            )
            sess_resp.raise_for_status()
            session_id = sess_resp.json().get('session_id', '')
            if not session_id:
                return '❌ Не удалось создать сессию'
            link_resp = requests.post(
                f'{BASE}/v3.1/tool_router/session/{session_id}/link',
                headers=headers(),
                json={"toolkit": toolkit},
                timeout=10
            )
            link_resp.raise_for_status()
            redirect_url = link_resp.json().get('redirect_url', '')
            if not redirect_url:
                return '❌ Ссылка не получена'
            return f'COMPOSIO_AUTH:{toolkit}:{redirect_url}'
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    elif cmd == 'list':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        try:
            resp = requests.get(f'{BASE}/v3.1/toolkits?limit=30', headers=headers(), timeout=10)
            resp.raise_for_status()
            items = resp.json().get('items', [])
            if not items:
                return '📭 Интеграции не найдены'
            result = f'🧩 **Доступные интеграции** (из 1000+):\n\n'
            for item in items[:25]:
                slug = item.get('slug', '?')
                name = item.get('name', slug)
                result += f'• **{name}** `{slug}` → `/composio auth {slug}`\n'
            result += '\n🔍 Найди нужную: `/composio search <название>`'
            return result
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    elif cmd == 'search':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        if len(args) < 2:
            return '❌ `/composio search notion`'
        query = ' '.join(args[1:])
        try:
            resp = requests.get(
                f'{BASE}/v3.1/toolkits?search={requests.utils.quote(query)}&limit=10',
                headers=headers(), timeout=10
            )
            resp.raise_for_status()
            items = resp.json().get('items', [])
            if not items:
                return f'🔍 **"{query}"** — ничего не найдено'
            result = f'🔍 **Результаты "{query}"**:\n\n'
            for item in items[:8]:
                slug = item.get('slug', '?')
                name = item.get('name', slug)
                desc = (item.get('description') or '')[:60]
                result += f'**{name}** (`{slug}`)\n_{desc}_\n'
                result += f'▶️ `/composio auth {slug}`\n\n'
            return result
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    elif cmd == 'execute':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        if len(args) < 2:
            return '❌ `/composio execute GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER`'
        action_name = args[1].upper()
        params = {}
        if len(args) > 2:
            try:
                params = json.loads(' '.join(args[2:]))
            except:
                params = {'query': ' '.join(args[2:])}
        result = execute_tool(action_name, params)
        return format_raw(result, action_name)

    elif cmd == 'menu':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        try:
            resp = requests.get(f'{BASE}/v3.1/toolkits?limit=30', headers=headers(), timeout=10)
            resp.raise_for_status()
            items = resp.json().get('items', [])
            connected_slugs = set()
            try:
                acc_resp = requests.get(f'{BASE}/v3.1/connected_accounts', headers=headers(), timeout=10)
                if acc_resp.ok:
                    for acc in acc_resp.json().get('items', []):
                        t = acc.get('toolkit', {})
                        slug = t.get('slug', '') if isinstance(t, dict) else str(t)
                        if acc.get('status') == 'ACTIVE':
                            connected_slugs.add(slug.lower())
            except:
                pass
            cards = []
            for item in items[:20]:
                slug = item.get('slug', '?')
                name = item.get('name', slug)
                desc = (item.get('description') or '')[:60]
                cards.append({'slug': slug, 'name': name, 'desc': desc, 'connected': slug.lower() in connected_slugs})
            return f'COMPOSIO_CARDS:{json.dumps(cards, ensure_ascii=False)}'
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    else:
        return f'❌ Неизвестная команда: `{cmd}`\n\nВведи `/composio` для помощи'
