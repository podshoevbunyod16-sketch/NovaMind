"""
API Catalog Module — Получение и форматирование каталога бесплатных API
"""
import requests
import json
import logging

logger = logging.getLogger(__name__)


def get_api_catalog():
    """
    Загружает каталог бесплатных API с JSON источника
    Возвращает HTML-список с кликабельными ссылками и кнопками подключения
    """
    try:
        # Получаем каталог
        url = "https://public-api-lists.github.io/public-api-lists/api.json"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        apis = response.json()
        
        if not isinstance(apis, list):
            return format_error("Неверный формат каталога")
        
        # Ограничиваем первыми 20 API для производительности
        apis = apis[:20]
        
        # Формируем HTML
        html = '<div class="api-catalog">'
        html += '<h3 style="margin-bottom: 20px;">📚 Доступные API</h3>'
        html += '<ul style="list-style: none; padding: 0; margin: 0;">'
        
        for api in apis:
            api_name = api.get('name', 'Unknown API')
            api_desc = api.get('description', 'Нет описания')
            api_url = api.get('apiDocumentation', '#')
            
            # Очищаем название для использования в функции
            tool_name = sanitize_tool_name(api_name)
            
            html += f'''
            <li style="
                margin-bottom: 12px;
                padding: 12px;
                border: 1px solid rgba(124,58,237,0.3);
                border-radius: 8px;
                background: rgba(124,58,237,0.05);
            ">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;">
                    <div style="flex: 1;">
                        <a href="{api_url}" target="_blank" style="
                            color: #c4b5fd;
                            text-decoration: none;
                            font-weight: 600;
                            font-size: 15px;
                        ">{api_name}</a>
                        <p style="margin: 6px 0 0 0; color: #a78bfa; font-size: 13px;">{api_desc[:100]}...</p>
                    </div>
                    <button onclick="initComposio('{tool_name}')" style="
                        background: linear-gradient(135deg, #7c3aed, #a855f7);
                        color: white;
                        border: none;
                        padding: 8px 16px;
                        border-radius: 6px;
                        cursor: pointer;
                        font-size: 13px;
                        font-weight: 600;
                        white-space: nowrap;
                        transition: all 0.3s ease;
                    " onmouseover="this.style.background='linear-gradient(135deg, #a855f7, #d946ef)'" 
                       onmouseout="this.style.background='linear-gradient(135deg, #7c3aed, #a855f7)'">
                        🔗 Подключить
                    </button>
                </div>
            </li>
            '''
        
        html += '</ul></div>'
        return html
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API Catalog Error: {e}")
        return format_error(f"Ошибка загрузки каталога: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error in API catalog: {e}")
        return format_error(f"Неожиданная ошибка: {str(e)}")


def format_error(message):
    """Форматирует сообщение об ошибке в HTML"""
    return f'''
    <div style="
        padding: 16px;
        background: rgba(239,68,68,0.1);
        border: 1px solid rgba(239,68,68,0.5);
        border-radius: 8px;
        color: #fca5a5;
        text-align: center;
    ">
        ⚠️ {message}
    </div>
    '''


def sanitize_tool_name(name):
    """
    Преобразует имя API в формат инструмента
    Пример: "OpenWeather API" → "openweather"
    """
    import re
    # Убираем "API", спецсимволы
    cleaned = re.sub(r'\s+API\s*$', '', name, flags=re.IGNORECASE)
    cleaned = re.sub(r'[^a-zA-Z0-9]', '', cleaned)
    return cleaned.lower()


def run(args):
    """
    Точка входа для плагина команды
    Возвращает HTML каталог API
    """
    return get_api_catalog()
