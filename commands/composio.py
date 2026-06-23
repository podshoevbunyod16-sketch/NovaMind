import requests
import os
import json

COMPOSIO_API_KEY = lambda: os.getenv('COMPOSIO_API_KEY', '')
BASE = 'https://backend.composio.dev/api'
GROQ_API_KEY = lambda: os.getenv('GROQ_API_KEY', '')


def headers():
    return {
        'x-api-key': COMPOSIO_API_KEY(),
        'Content-Type': 'application/json'
    }


def get_active_accounts():
    try:
        resp = requests.get(
            f'{BASE}/v3.1/connected_accounts',
            headers=headers(),
            timeout=10
        )
        resp.raise_for_status()
        items = resp.json().get('items', [])
        return [a for a in items if a.get('status') == 'ACTIVE']
    except:
        return []


def get_account_for_toolkit(toolkit):
    accounts = get_active_accounts()
    for acc in accounts:
        t = acc.get('toolkit', {})
        slug = t.get('slug', '') if isinstance(t, dict) else str(t)
        if slug.lower() == toolkit.lower():
            return acc['id']
    return accounts[0]['id'] if accounts else ''


def get_tools_for_toolkit(toolkit):
    try:
        resp = requests.get(
            f'{BASE}/v3.1/tools?toolkit_slug={toolkit}&toolkit_versions=latest&limit=30',
            headers=headers(),
            timeout=10
        )
        if resp.ok:
            items = resp.json().get('items', [])
            if items:
                return items
    except:
        pass
    return []


def extract_word_after(text, keywords):
    text_lower = text.lower()
    for kw in keywords:
        idx = text_lower.find(kw)
        if idx != -1:
            after = text[idx + len(kw):].strip()
            words = after.split()
            return words[0] if words else ''
    return ''


def extract_after_keyword(text, keywords):
    text_lower = text.lower()
    for kw in keywords:
        idx = text_lower.find(kw)
        if idx != -1:
            return text[idx + len(kw):].strip()
    return text


def find_tool_by_keyword(tool_name_hint, tools_list):
    hint_upper = tool_name_hint.upper()
    for t in tools_list:
        slug = (t.get('slug') or '').upper()
        if slug == hint_upper:
            return slug
    for t in tools_list:
        slug = (t.get('slug') or '').upper()
        if hint_upper in slug or slug in hint_upper:
            return t.get('slug')
    return None


def keyword_parse(task, tools_list):
    task_lower = task.lower()

    keywords = {
        'создай репозитори': ('GITHUB_CREATE_REPO', {
            'name': extract_word_after(task, ['репозитори', 'repo', 'repository']),
            'description': '', 'private': False
        }),
        'создай repo': ('GITHUB_CREATE_REPO', {
            'name': extract_word_after(task, ['repo', 'репо'])
        }),
        'список репозитори': ('GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER', {'per_page': 10, 'page': 1}),
        'покажи репозитори': ('GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER', {'per_page': 10, 'page': 1}),
        'мои репозитори': ('GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER', {'per_page': 10, 'page': 1}),
        'данные из github': ('GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER', {'per_page': 10, 'page': 1}),
        'мои проекты github': ('GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER', {'per_page': 10, 'page': 1}),
        'создай issue': ('GITHUB_CREATE_ISSUE', {
            'title': extract_after_keyword(task, ['issue', 'issue с заголовком'])
        }),
        'создай файл': ('GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS', {}),
        'добавь файл': ('GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS', {}),
        'список issue': ('GITHUB_LIST_ISSUES', {}),
        'покажи issue': ('GITHUB_LIST_ISSUES', {}),
        'создай ветку': ('GITHUB_CREATE_BRANCH', {}),
        'список веток': ('GITHUB_LIST_BRANCHES', {}),
        'форк': ('GITHUB_CREATE_FORK', {}),
        'мой профиль github': ('GITHUB_GET_THE_AUTHENTICATED_USER', {}),
        'отправь письмо': ('GMAIL_SEND_EMAIL', {}),
        'отправь email': ('GMAIL_SEND_EMAIL', {}),
        'входящие письма': ('GMAIL_FETCH_EMAILS', {}),
        'покажи письма': ('GMAIL_FETCH_EMAILS', {}),
        'данные из gmail': ('GMAIL_FETCH_EMAILS', {}),
        'найди письма': ('GMAIL_FETCH_EMAILS', {}),
        'прочитай письма': ('GMAIL_FETCH_EMAILS', {}),
        'создай черновик': ('GMAIL_CREATE_EMAIL_DRAFT', {}),
        'создай страницу': ('NOTION_CREATE_PAGE', {}),
        'добавь в notion': ('NOTION_CREATE_PAGE', {}),
        'найди в notion': ('NOTION_SEARCH', {'query': task}),
        'данные из notion': ('NOTION_SEARCH', {'query': ''}),
        'отправь сообщение в slack': ('SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL', {}),
        'напиши в slack': ('SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL', {}),
        'список каналов': ('SLACK_LIST_CHANNELS', {}),
        'создай карточку': ('TRELLO_CREATE_CARD', {}),
        'список досок': ('TRELLO_LIST_BOARDS', {}),
        'создай событие': ('GOOGLECALENDAR_CREATE_EVENT', {}),
        'мои события': ('GOOGLECALENDAR_LIST_EVENTS', {}),
    }

    for kw, (tool_name, default_params) in keywords.items():
        if kw in task_lower:
            exact = find_tool_by_keyword(tool_name, tools_list)
            final_tool = exact or tool_name
            return {
                'tool': final_tool,
                'params': default_params,
                'explanation': f'Использую {final_tool} для: {task[:50]}'
            }

    words = task_lower.split()
    for word in words:
        if len(word) > 3:
            for t in tools_list:
                desc = (t.get('description') or '').lower()
                slug = (t.get('slug') or '').lower()
                if word in desc or word in slug:
                    return {
                        'tool': t.get('slug') or t.get('name'),
                        'params': {},
                        'explanation': f'Нашёл по слову: {word}'
                    }

    return {'tool': None, 'params': {}, 'explanation': 'Не нашёл инструмент'}


def ai_extract_params(task, tool_name, tool_schema):
    try:
        input_schema = tool_schema.get('input_schema', {})
        props = input_schema.get('properties', {}) if input_schema else {}
        params_desc = '\n'.join([
            f"- {k}: {v.get('description','')[:60]} (тип: {v.get('type','string')})"
            for k, v in props.items()
        ]) if props else 'параметры неизвестны'

        prompt = f"""Задача: {task}
Инструмент: {tool_name}
Параметры:
{params_desc}

Извлеки значения из задачи. Ответь ТОЛЬКО JSON без markdown:
{{"params": {{"param1": "value1"}}}}"""

        resp = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {GROQ_API_KEY()}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'openai/gpt-oss-120b',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.1,
                'max_tokens': 300
            },
            timeout=20
        )
        if resp.ok:
            content = resp.json()['choices'][0]['message']['content'].strip()
            if '```' in content:
                content = content.split('```')[1]
                if content.startswith('json'):
                    content = content[4:]
            parsed = json.loads(content.strip())
            return {
                'tool': tool_name,
                'params': parsed.get('params', {}),
                'explanation': f'Использую {tool_name} для: {task[:60]}'
            }
    except:
        pass
    return {'tool': tool_name, 'params': {}, 'explanation': f'Использую {tool_name}'}


def ai_groq_parse(task, tools_list):
    key = GROQ_API_KEY()
    if not key:
        return keyword_parse(task, tools_list)

    try:
        tools_str = '\n'.join([
            f"- {t.get('slug') or t.get('name','?')}: {(t.get('description') or '')[:70]}"
            for t in tools_list[:25]
        ])

        prompt = f"""Выбери ТОЧНЫЙ slug инструмента для задачи.

Инструменты:
{tools_str}

Задача: {task}

Ответь ТОЛЬКО JSON без markdown:
{{"tool": "ТОЧНЫЙ_SLUG", "params": {{"key": "value"}}, "explanation": "что сделаю"}}

Если не найден: {{"tool": null, "params": {{}}, "explanation": "не найден"}}"""

        resp = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json'
            },
            json={
                'model': 'openai/gpt-oss-120b',
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.1,
                'max_tokens': 400
            },
            timeout=25
        )
        if resp.ok:
            content = resp.json()['choices'][0]['message']['content'].strip()
            if '```' in content:
                parts = content.split('```')
                content = parts[1] if len(parts) > 1 else parts[0]
                if content.startswith('json'):
                    content = content[4:]
            return json.loads(content.strip())
        else:
            return keyword_parse(task, tools_list)
    except:
        return keyword_parse(task, tools_list)


def ai_parse_task(task, tools_list):
    try:
        resp = requests.post(
            f'{BASE}/v3.1/tools/generate',
            headers=headers(),
            json={"query": task, "limit": 5},
            timeout=15
        )
        if resp.ok:
            data = resp.json()
            tools = data.get('tools', data.get('items', []))
            if tools:
                tool = tools[0]
                tool_name = (
                    tool.get('slug') or
                    tool.get('name') or
                    tool.get('tool_slug', '')
                )
                if tool_name:
                    return ai_extract_params(task, tool_name, tool)
    except:
        pass
    return ai_groq_parse(task, tools_list)


def execute_tool(tool_name, params, connected_account_id):
    """
    ГЛАВНОЕ ИСПРАВЛЕНИЕ:
    v2 убран! Только v3.1/tools/{slug}/execute
    """
    try:
        payload = {
            'arguments': params if params else {},
            'user_id': 'novauser'
        }
        if connected_account_id:
            payload['connected_account_id'] = connected_account_id

        resp = requests.post(
            f'{BASE}/v3.1/tools/execute/{tool_name}',
            headers=headers(),
            json=payload,
            timeout=30
        )

        # Логируем для отладки
        print(f'[Composio] execute {tool_name} → {resp.status_code}')

        if resp.ok:
            data = resp.json()
            if isinstance(data, dict):
                return data

        # Показываем реальную ошибку
        try:
            err_data = resp.json()
            return {'error': err_data, 'successful': False}
        except:
            return {'error': resp.text[:400], 'successful': False}

    except Exception as e:
        return {'error': str(e), 'successful': False}


def format_result(result, tool_name):
    if not result:
        return '❌ Пустой ответ от Composio'

    # Ошибка
    if isinstance(result, dict) and not result.get('successful', True):
        err = result.get('error', 'неизвестная ошибка')
        msg = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)
        return f'❌ Ошибка:\n```\n{msg[:500]}\n```'

    output = (
        result.get('response_data') or
        result.get('result') or
        result.get('data') or
        result
    )

    # GitHub — файл
    if isinstance(output, dict) and 'content' in output and isinstance(output.get('content'), dict):
        url = output['content'].get('html_url', '')
        name = output['content'].get('name', '')
        return f'✅ Файл создан!\n📄 `{name}`\n🔗 {url}'

    # GitHub — репозиторий (один)
    if isinstance(output, dict) and 'full_name' in output:
        return (
            f'✅ Репозиторий: **{output["full_name"]}**\n'
            f'🔗 {output.get("html_url", "")}\n'
            f'📝 {output.get("description") or "—"}\n'
            f'⭐ {output.get("stargazers_count", 0)} звёзд'
        )

    # GitHub — issue/PR
    if isinstance(output, dict) and 'html_url' in output and 'number' in output:
        return (
            f'✅ #{output["number"]}: **{output.get("title", "")}**\n'
            f'🔗 {output["html_url"]}'
        )

    # Список
    if isinstance(output, list):
        if not output:
            return '✅ Выполнено — список пуст'
        lines = []
        for item in output[:10]:
            if isinstance(item, dict):
                name = (
                    item.get('full_name') or
                    item.get('name') or
                    item.get('title') or
                    str(item)[:60]
                )
                url = item.get('html_url', '')
                lines.append(f'• **{name}**' + (f'\n  🔗 {url}' if url else ''))
            else:
                lines.append(f'• {str(item)[:80]}')
        return f'✅ **{tool_name}** — {len(output)} результатов:\n\n' + '\n'.join(lines)

    # Общий dict
    if isinstance(output, dict):
        formatted = json.dumps(output, ensure_ascii=False, indent=2)
        if len(formatted) > 1200:
            formatted = formatted[:1200] + '\n...(обрезано)'
        return f'✅ **{tool_name}**:\n```json\n{formatted}\n```'

    return f'✅ **{tool_name}**:\n{str(output)[:800]}'


def run(args):
    if not args:
        return """🔗 **Composio команды:**

`/composio menu` — карточки интеграций в чате
`/composio do <задача>` — AI выберет и выполнит
`/composio list` — все интеграции
`/composio search <название>` — поиск
`/composio tools <toolkit>` — инструменты тулкита
`/composio auth <toolkit>` — ссылка авторизации
`/composio accounts` — мои аккаунты
`/composio execute <slug> [json]` — выполнить напрямую

**Примеры:**
`/composio do покажи мои репозитории github`
`/composio do создай репозиторий test-repo на GitHub`
`/composio do отправь письмо на test@mail.com тема Привет текст Как дела`"""

    cmd = args[0].lower()

    if cmd == 'do':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ Подключи API ключ на странице /composio'
        if len(args) < 2:
            return '❌ Опиши задачу!'

        task = ' '.join(args[1:])
        accounts = get_active_accounts()

        if not accounts:
            return '❌ Нет аккаунтов!\n\nПодключи: `/composio auth github`'

        seen = set()
        active_toolkits = []
        for acc in accounts:
            t = acc.get('toolkit', {})
            slug = t.get('slug', '') if isinstance(t, dict) else str(t)
            if slug and slug not in seen:
                seen.add(slug)
                active_toolkits.append(slug)

        toolkit_str = ', '.join(active_toolkits)
        log = f'🤖 **AI агент**\n\n'
        log += f'📋 Задача: _{task}_\n'
        log += f'🔗 Интеграции: `{toolkit_str}`\n\n'

        all_tools = []
        for toolkit in active_toolkits[:3]:
            tools = get_tools_for_toolkit(toolkit)
            all_tools.extend(tools)
            log += f'🛠️ {toolkit}: {len(tools)} инструментов\n'

        log += '\n'

        parsed = ai_parse_task(task, all_tools)
        tool_name = parsed.get('tool')
        params = parsed.get('params', {})
        explanation = parsed.get('explanation', '')

        if not tool_name:
            return (
                log +
                f'🤔 Не нашёл инструмент\n_{explanation}_\n\n'
                f'Попробуй: `/composio tools github`'
            )

        log += f'✨ Инструмент: **{tool_name}**\n'
        log += f'📝 План: _{explanation}_\n'
        log += f'⚙️ Параметры: `{json.dumps(params, ensure_ascii=False)}`\n\n'

        target_toolkit = active_toolkits[0]
        for toolkit in active_toolkits:
            if toolkit.upper() in tool_name.upper():
                target_toolkit = toolkit
                break

        account_id = get_account_for_toolkit(target_toolkit)
        log += f'🚀 Выполняю...\n\n'

        exec_result = execute_tool(tool_name, params, account_id)
        return log + format_result(exec_result, tool_name)

    elif cmd == 'menu':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ Подключи API ключ на странице /composio'
        try:
            resp = requests.get(
                f'{BASE}/v3.1/toolkits?limit=30',
                headers=headers(),
                timeout=10
            )
            resp.raise_for_status()
            items = resp.json().get('items', [])
            if not items:
                return '📭 Интеграции не найдены'

            connected_slugs = set()
            try:
                acc_resp = requests.get(
                    f'{BASE}/v3.1/connected_accounts',
                    headers=headers(),
                    timeout=10
                )
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
                cards.append({
                    'slug': slug,
                    'name': name,
                    'desc': desc,
                    'connected': slug.lower() in connected_slugs
                })

            return f'COMPOSIO_CARDS:{json.dumps(cards, ensure_ascii=False)}'
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    elif cmd == 'list':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        try:
            resp = requests.get(
                f'{BASE}/v3.1/toolkits?limit=30',
                headers=headers(),
                timeout=10
            )
            resp.raise_for_status()
            items = resp.json().get('items', [])
            if not items:
                return '📭 Интеграции не найдены'

            result = f'🧩 **Доступные интеграции** ({len(items)}):\n\n'
            for item in items[:25]:
                slug = item.get('slug', '?')
                name = item.get('name', slug)
                desc = (item.get('description') or '')[:55]
                result += f'• **{name}** `{slug}`\n'
                if desc:
                    result += f'  _{desc}_\n'
                result += f'  ▶️ `/composio auth {slug}`\n\n'
            return result
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    elif cmd == 'search':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        if len(args) < 2:
            return '❌ `/composio search github`'
        query = args[1].lower()
        try:
            resp = requests.get(
                f'{BASE}/v3.1/toolkits?limit=50&search={query}',
                headers=headers(),
                timeout=10
            )
            resp.raise_for_status()
            items = resp.json().get('items', [])
            filtered = [
                i for i in items
                if query in i.get('slug', '').lower()
                or query in i.get('name', '').lower()
                or query in (i.get('description') or '').lower()
            ]
            if not filtered:
                return f'🔍 **"{query}"** — ничего не найдено'

            result = f'🔍 **Результаты "{query}"** ({len(filtered)}):\n\n'
            for item in filtered[:8]:
                slug = item.get('slug', '?')
                name = item.get('name', slug)
                desc = (item.get('description') or '')[:70]
                result += f'**{name}** (`{slug}`)\n'
                if desc:
                    result += f'_{desc}_\n'
                result += f'▶️ `/composio auth {slug}` | `/composio tools {slug}`\n\n'
            return result
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    elif cmd == 'tools':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        if len(args) < 2:
            return '❌ `/composio tools github`'
        toolkit = args[1].lower()
        items = get_tools_for_toolkit(toolkit)
        if not items:
            return f'🛠️ Инструменты для **{toolkit}** не найдены'

        result = f'🛠️ **{toolkit.upper()} инструменты** ({len(items)}):\n\n'
        for action in items[:15]:
            slug = action.get('slug') or action.get('name') or '?'
            desc = (action.get('description') or '')[:65]
            result += f'• **{slug}**\n'
            if desc:
                result += f'  _{desc}_\n'
            result += f'  ▶️ `/composio execute {slug}`\n\n'
        return result

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

    elif cmd == 'accounts':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        try:
            resp = requests.get(
                f'{BASE}/v3.1/connected_accounts',
                headers=headers(),
                timeout=10
            )
            resp.raise_for_status()
            items = resp.json().get('items', [])
            if not items:
                return '📭 Нет аккаунтов\n\nПодключи: `/composio auth github`'

            result = f'👤 **Мои аккаунты** ({len(items)}):\n\n'
            for acc in items:
                t = acc.get('toolkit', {})
                toolkit = t.get('slug', '?') if isinstance(t, dict) else str(t)
                status = acc.get('status', '?')
                emoji = '✅' if status == 'ACTIVE' else '⚠️'
                result += f'{emoji} **{toolkit.upper()}** — `{status}`\n'
                result += f'   ID: `{acc.get("id", "?")}`\n'
                result += f'   🛠️ `/composio tools {toolkit}`\n'
                result += f'   🤖 `/composio do покажи данные из {toolkit}`\n\n'
            return result
        except Exception as e:
            return f'❌ Ошибка: {str(e)}'

    elif cmd == 'execute':
        key = COMPOSIO_API_KEY()
        if not key:
            return '❌ API ключ не найден'
        if len(args) < 2:
            return '❌ `/composio execute GITHUB_GET_REPOS`'

        action_name = args[1].upper()
        params = {}
        if len(args) > 2:
            try:
                params = json.loads(' '.join(args[2:]))
            except:
                params = {'query': ' '.join(args[2:])}

        accounts = get_active_accounts()
        account_id = accounts[0]['id'] if accounts else ''
        result = execute_tool(action_name, params, account_id)
        return format_result(result, action_name)

    else:
        return f'❌ Неизвестная команда: `{cmd}`\n\nВведи `/composio` для помощи'
