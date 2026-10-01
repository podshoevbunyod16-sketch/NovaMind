/**
 * static/i18n.js — интерфейс на трёх языках: тоҷикӣ (по умолчанию), русский, English.
 *
 * Как это работает:
 *  1. Словарь ENTRIES хранит каждую строку интерфейса на трёх языках.
 *  2. apply() обходит текстовые узлы и атрибуты (placeholder, title, aria-label)
 *     и заменяет строки по словарю; PATTERNS переводит шаблонные строки
 *     вида «Поисковый запрос: …» с сохранением динамической части.
 *  3. MutationObserver переводит всё, что интерфейс создаёт позже:
 *     шаги агента, статусы, тосты и т.д. — даже если их генерирует app.js.
 *  4. Выбор языка: кнопки [data-lang] в сайдбаре и в окне настроек.
 *     Язык сохраняется в localStorage и на сервере (runtime_settings.json).
 */
(() => {
  'use strict';

  const SUPPORTED = ['tg', 'ru', 'en'];
  const STORAGE_KEY = 'nova_ui_lang';
  const DEFAULT_LANG = 'tg'; // таджикский — язык по умолчанию

  // ─────────────────────────── словарь ───────────────────────────
  // ru — исходная строка в коде; tg/en — переводы.
  const ENTRIES = [
    // — сайдбар —
    { ru: 'Новый диалог', tg: 'Сӯҳбати нав', en: 'New chat' },
    { ru: 'Основные', tg: 'Асосӣ', en: 'Main' },
    { ru: 'Чат', tg: 'Чат', en: 'Chat' },
    { ru: 'Веб-поиск', tg: 'Ҷустуҷӯи веб', en: 'Web search' },
    { ru: 'Генерация изображений', tg: 'Сохтани тасвирҳо', en: 'Image generation' },
    { ru: 'Помощник кода', tg: 'Ёрдамчи барои код', en: 'Code assistant' },
    { ru: 'Сервисы', tg: 'Хидматҳо', en: 'Services' },
    { ru: 'Погода', tg: 'Ҳаво', en: 'Weather' },
    { ru: 'Курс валют', tg: 'Курси асъор', en: 'Exchange rates' },
    { ru: 'Википедия', tg: 'Википедия', en: 'Wikipedia' },
    { ru: 'История чатов', tg: 'Таърихи чатҳо', en: 'Chat history' },
    { ru: '+ Новый', tg: '+ Нав', en: '+ New' },
    { ru: 'Панель управления', tg: 'Панели идора', en: 'Control panel' },
    { ru: 'Настройки и ключи', tg: 'Танзимот ва калидҳо', en: 'Settings and keys' },
    { ru: 'Пользователь', tg: 'Корбар', en: 'User' },
    { ru: 'Базовый план', tg: 'Нақшаи асосӣ', en: 'Basic plan' },
    { ru: 'Выйти', tg: 'Баромадан', en: 'Sign out' },
    // — верхняя панель —
    { ru: 'AI Ассистент', tg: 'AI Ёрдамчи', en: 'AI Assistant' },
    { ru: 'Медиа', tg: 'Медиа', en: 'Media' },
    { ru: 'Открыть боковую панель', tg: 'Кушодани панели канорӣ', en: 'Open sidebar' },
    { ru: 'Настройки AI', tg: 'Танзимоти AI', en: 'AI settings' },
    { ru: 'Очистить чат', tg: 'Тоза кардани чат', en: 'Clear chat' },
    // — панель ввода —
    { ru: 'Поиск', tg: 'Ҷустуҷӯ', en: 'Search' },
    { ru: 'Рассуждение', tg: 'Фикрронӣ', en: 'Reasoning' },
    { ru: 'Прикрепить файл', tg: 'Замима кардани файл', en: 'Attach file' },
    { ru: 'Изображение', tg: 'Тасвир', en: 'Image' },
    { ru: 'Файл', tg: 'Файл', en: 'File' },
    { ru: 'Аудио', tg: 'Аудио', en: 'Audio' },
    { ru: 'Видео', tg: 'Видео', en: 'Video' },
    { ru: 'Анимации', tg: 'Аниматсияҳо', en: 'Animations' },
    { ru: 'Напишите сообщение...', tg: 'Паём нависед...', en: 'Type a message...' },
    { ru: 'Отправить (Enter)', tg: 'Фиристодан (Enter)', en: 'Send (Enter)' },
    { ru: 'Голосовой ввод', tg: 'Воридсозии овозӣ', en: 'Voice input' },
    { ru: 'Нажмите для записи', tg: 'Барои сабт пахш кунед', en: 'Click to record' },
    { ru: 'Автопоиск: ИИ ищет в интернете через Linux-окружение и отвечает с источниками',
      tg: 'Ҷустуҷӯи худкор: AI аз тариқи муҳити Linux дар интернет меҷӯяд ва бо манобиъ ҷавоб медиҳад',
      en: 'Auto search: the AI searches the web via the Linux environment and answers with sources' },
    { ru: 'Показывать ход рассуждений модели', tg: 'Нишон додани раванди фикрронии модель', en: 'Show the model\u2019s reasoning' },
    { ru: 'Модели изображений, аудио и видео', tg: 'Модельҳои тасвир, аудио ва видео', en: 'Image, audio and video models' },
    { ru: 'Отключить фоновые анимации', tg: 'Хомӯш кардани аниматсияҳои замина', en: 'Turn off background animations' },
    { ru: 'Медиа-модель не выбрана', tg: 'Модели медиа интихоб нашудааст', en: 'No media model selected' },
    { ru: 'Вернуться к обычному чату', tg: 'Бозгашт ба чати оддӣ', en: 'Back to regular chat' },
    { ru: 'Выключить режим', tg: 'Хомӯш кардани реҷа', en: 'Turn off mode' },
    // — экран приветствия —
    { ru: 'Привет! Я', tg: 'Салом! Ман', en: 'Hi! I am' },
    { ru: 'Напишите задачу — я отвечу сразу или сам поработаю в Linux: запущу код, создам файлы, найду свежие данные.',
      tg: 'Масъаларо нависед — ман фавран ҷавоб медиҳам ё худ дар Linux кор мекунам: кодро иҷро мекунам, файлҳо месозам, маълумоти нав меҷӯям.',
      en: 'Describe a task \u2014 I will answer right away or work in Linux myself: run code, create files, find fresh data.' },
    { ru: 'Khirad может ошибаться. Проверяйте важную информацию',
      tg: 'Khirad метавонад хато кунад. Маълумоти муҳимро санҷед',
      en: 'Khirad can make mistakes. Verify important information' },
    // — диалог выбора медиа-модели —
    { ru: 'Изображения, аудио и видео', tg: 'Тасвирҳо, аудио ва видео', en: 'Images, audio and video' },
    { ru: 'Один интерфейс для всех типов генерации. После выбора модели просто напишите промпт в обычном поле чата — результат появится в переписке.',
      tg: 'Яке интерфейс барои ҳамаи намудҳои генератсия. Баъди интихоби модель промптро дар майдони маъмулии чат нависед — натиҷа дар сӯҳбат пайдо мешавад.',
      en: 'One interface for every generation type. After picking a model, just type the prompt in the usual chat field \u2014 the result appears in the conversation.' },
    { ru: 'Все', tg: 'Ҳама', en: 'All' },
    { ru: 'Поиск модели или провайдера…', tg: 'Ҷустуҷӯи модель ё провайдер…', en: 'Search model or provider…' },
    { ru: 'Пробные кредиты', tg: 'Кредитҳои санҷишӣ', en: 'Trial credits' },
    { ru: 'Платные', tg: 'Пардохтӣ', en: 'Paid' },
    { ru: 'Загрузка каталога…', tg: 'Боркунии каталог…', en: 'Loading catalog…' },
    { ru: 'Загружаю модели…', tg: 'Модельҳоро бор мекунам…', en: 'Loading models…' },
    { ru: 'Выберите модель — промпт пишется в обычном поле чата.',
      tg: 'Модельро интихоб кунед — промпт дар майдони маъмулии чат нависед.',
      en: 'Pick a model \u2014 the prompt goes in the usual chat field.' },
    { ru: 'Готово', tg: 'Тайёр', en: 'Done' },
    { ru: 'Закрыть', tg: 'Пӯшидан', en: 'Close' },
    { ru: 'Модели генерации медиа', tg: 'Модельҳои генератсияи медиа', en: 'Media generation models' },
    // — командная панель —
    { ru: 'Команда, режим или действие…', tg: 'Фармон, реҷа ё амал…', en: 'Command, mode or action…' },
    { ru: 'Команды', tg: 'Фармонҳо', en: 'Commands' },
    // — подсказки режимов —
    { ru: 'Введите запрос для поиска...', tg: 'Дархостро барои ҷустуҷӯ ворид кунед...', en: 'Enter a search query...' },
    { ru: 'Опишите изображение для генерации...', tg: 'Тасвирро барои сохтан тасвир кунед...', en: 'Describe an image to generate...' },
    { ru: 'Опишите, какой код создать...', tg: 'Тасвир кунед, кадом код сохтан...', en: 'Describe what code to create...' },
    { ru: 'Введите город для погоды...', tg: 'Шаҳрро барои ҳаво ворид кунед...', en: 'Enter a city for weather...' },
    { ru: 'Введите валюты (USD RUB)...', tg: 'Асъорҳоро ворид кунед (USD RUB)...', en: 'Enter currencies (USD RUB)...' },
    { ru: 'Введите запрос для Википедии...', tg: 'Дархостро барои Википедия ворид кунед...', en: 'Enter a Wikipedia query...' },
    // — статусы и шаги ИИ —
    { ru: 'Думаю…', tg: 'Фикр мекунам…', en: 'Thinking…' },
    { ru: 'Рассуждаю…', tg: 'Фикрронӣ…', en: 'Reasoning…' },
    { ru: 'Ищу в интернете', tg: 'Дар интернет меҷӯям', en: 'Searching the web' },
    { ru: 'Отвечаю без поиска', tg: 'Бе ҷустуҷӯ ҷавоб медиҳам', en: 'Answering without search' },
    { ru: 'Свежие данные не нужны — отвечаю по знаниям модели',
      tg: 'Маълумоти нав лозим нест — аз дониши модель ҷавоб медиҳам',
      en: 'No fresh data needed \u2014 answering from the model\u2019s knowledge' },
    { ru: 'Собираю ответ', tg: 'Ҷавобро ҷамъ мекунам', en: 'Composing the answer' },
    { ru: 'Анализирую запрос', tg: 'Дархостро таҳлил мекунам', en: 'Analyzing the request' },
    { ru: 'Загрузка…', tg: 'Боркунӣ…', en: 'Loading…' },
    { ru: 'Анимации включены', tg: 'Аниматсияҳо фаъол', en: 'Animations on' },
    { ru: 'Спокойный режим: фоновые анимации выключены', tg: 'Реҷаи ором: аниматсияҳои замина хомӯш', en: 'Calm mode: background animations off' },
    { ru: 'Поиск вкл', tg: 'Ҷустуҷӯ фаъол', en: 'Search on' },
    { ru: 'Автопоиск выключен', tg: 'Ҷустуҷӯи худкор хомӯш', en: 'Auto search off' },
    { ru: 'Рассуждение выключено', tg: 'Фикрронӣ хомӯш', en: 'Reasoning off' },
    // — окно настроек —
    { ru: 'Настройки AI', tg: 'Танзимоти AI', en: 'AI Settings' },
    { ru: 'Вернуться в чат', tg: 'Бозгашт ба чат', en: 'Back to chat' },
    { ru: 'ПРОВАЙДЕРЫ И МОДЕЛИ', tg: 'ПРОВАЙДЕРӢҲО ВА МОДЕЛӢҲО', en: 'PROVIDERS AND MODELS' },
    { ru: 'Выберите движок для чата', tg: 'Движокро барои чат интихоб кунед', en: 'Choose an engine for the chat' },
    { ru: 'Список моделей проверяется напрямую через API провайдера. Ключи остаются только на backend и не передаются в браузер.',
      tg: 'Рӯйхати модельҳо бевосита аз тариқи API-и провайдер санҷида мешавад. Калидҳо танҳо дар backend мемонанд ва ба браузер дода намешаванд.',
      en: 'The model list is verified directly through the provider\u2019s API. Keys stay on the backend and are never sent to the browser.' },
    { ru: 'Обновить всё', tg: 'Ҳамаро нав кардан', en: 'Refresh all' },
    { ru: 'Загружаю провайдеров…', tg: 'Провайдерҳоро бор мекунам…', en: 'Loading providers…' },
    { ru: 'Доступные модели', tg: 'Модельҳои дастрас', en: 'Available models' },
    { ru: 'Выберите провайдера', tg: 'Провайдерро интихоб кунед', en: 'Select a provider' },
    { ru: 'Обновить список', tg: 'Рӯйхатро нав кардан', en: 'Refresh list' },
    { ru: 'Цена: все', tg: 'Нарх: ҳама', en: 'Price: all' },
    { ru: 'Только бесплатные', tg: 'Танҳо ройгон', en: 'Free only' },
    { ru: 'Только платные', tg: 'Танҳо пардохтӣ', en: 'Paid only' },
    { ru: 'Сначала рекомендуемые', tg: 'Аввал тавсияшуда', en: 'Recommended first' },
    { ru: 'Поиск модели…', tg: 'Ҷустуҷӯи модель…', en: 'Model search…' },
    { ru: 'Модели для генерации', tg: 'Модельҳо барои генератсия', en: 'Generation models' },
    { ru: 'Модели ещё не загружены', tg: 'Модельҳо ҳанӯз бор нашудаанд', en: 'Models not loaded yet' },
    { ru: 'Живой каталог для изображений, аудио и видео с анализом бесплатного тарифа и стоимости.',
      tg: 'Каталоги зинда барои тасвирҳо, аудио ва видео бо таҳлили тарифи ройгон ва нарх.',
      en: 'A live catalog for images, audio and video with free-tier and pricing analysis.' },
    { ru: 'Проверить API', tg: 'Санҷиши API', en: 'Check API' },
    { ru: 'Поиск в интернете', tg: 'Ҷустуҷӯ дар интернет', en: 'Web search' },
    { ru: 'Проверяю поисковые бэкенды…', tg: 'Бэкендҳои ҷустуҷӯро месанҷам…', en: 'Checking search backends…' },
    { ru: 'Проверка бэкендов…', tg: 'Санҷиши бэкендҳо…', en: 'Checking backends…' },
    { ru: 'Проверить', tg: 'Санҷидан', en: 'Check' },
    { ru: 'Язык интерфейса', tg: 'Забони интерфейс', en: 'Interface language' },
    { ru: 'Выберите язык: интерфейс, подсказки и сообщения переключаются сразу.',
      tg: 'Забонро интихоб кунед: интерфейс, маслиҳатҳо ва паёмҳо фавран иваз мешаванд.',
      en: 'Pick a language: the interface, hints and messages switch right away.' },
    { ru: 'Загрузка', tg: 'Боркунӣ', en: 'Loading' },

    // — динамические сообщения app.js / linux.js / settings.js —
    { ru: 'Копирование интерфейса запрещено. Выделите текст в сообщении.', tg: 'Нусхабардории интерфейс манъ аст. Матнро дар паём интихоб кунед.', en: 'Copying the interface is not allowed. Select text in a message.' },
    { ru: 'Анимации включены', tg: 'Аниматсияҳо фаъоланд', en: 'Animations enabled' },
    { ru: 'Спокойный режим: фоновые анимации выключены', tg: 'Реҷаи ором: аниматсияҳои замина хомӯшанд', en: 'Calm mode: background animations are off' },
    { ru: 'Поиск вкл', tg: 'Ҷустуҷӯ фаъол', en: 'Search on' },
    { ru: '🔍 Автопоиск включён — ИИ ищет в интернете через Linux и отвечает с источниками', tg: '🔍 Ҷустуҷӯи худкор фаъол — AI тавассути Linux дар интернет меҷӯяд ва бо манбаъҳо ҷавоб медиҳад', en: '🔍 Auto search enabled — AI searches the web through Linux and answers with sources' },
    { ru: 'Автопоиск выключен', tg: 'Ҷустуҷӯи худкор хомӯш аст', en: 'Auto search disabled' },
    { ru: '🧠 Режим рассуждения включён — покажу ход мыслей модели', tg: '🧠 Реҷаи фикрронӣ фаъол — раванди фикрронии модел нишон дода мешавад', en: '🧠 Reasoning mode enabled — the model reasoning will be shown' },
    { ru: 'Рассуждение выключено', tg: 'Фикрронӣ хомӯш аст', en: 'Reasoning disabled' },
    { ru: '🔍 Авто-поиск включён', tg: '🔍 Ҷустуҷӯи худкор фаъол аст', en: '🔍 Auto search enabled' },
    { ru: 'Авто-поиск выключен', tg: 'Ҷустуҷӯи худкор хомӯш аст', en: 'Auto search disabled' },
    { ru: 'Читаю файл…', tg: 'Файлро мехонам…', en: 'Reading file…' },
    { ru: 'Анализ завершён, но ответ пустой.', tg: 'Таҳлил анҷом ёфт, вале ҷавоб холӣ аст.', en: 'Analysis finished, but the response was empty.' },
    { ru: 'Ошибка анализа вложения', tg: 'Хатои таҳлили замима', en: 'Attachment analysis error' },
    { ru: 'Расшифровываю аудио…', tg: 'Аудиоро ба матн табдил медиҳам…', en: 'Transcribing audio…' },
    { ru: 'Текст не распознан.', tg: 'Матн шинохта нашуд.', en: 'No text was recognized.' },
    { ru: 'Ошибка загрузки аудио', tg: 'Хатои боркунии аудио', en: 'Audio upload error' },
    { ru: 'Загружаю видео…', tg: 'Видео бор карда мешавад…', en: 'Uploading video…' },
    { ru: '❌ Ошибка загрузки видео', tg: '❌ Хатои боркунии видео', en: '❌ Video upload error' },
    { ru: 'ПРОБНЫЕ КРЕДИТЫ', tg: 'КРЕДИТҲОИ САНҶИШӢ', en: 'TRIAL CREDITS' },
    { ru: 'ЦЕНА НЕ ПОДТВЕРЖДЕНА', tg: 'НАРХ ТАСДИҚ НАШУДААСТ', en: 'PRICE NOT CONFIRMED' },
    { ru: 'Запрос каталога…', tg: 'Каталог дархост карда мешавад…', en: 'Requesting catalog…' },
    { ru: 'Нет ключа в .env', tg: 'Калид дар .env нест', en: 'No key in .env' },
    { ru: 'Выбрать', tg: 'Интихоб кардан', en: 'Select' },
    { ru: 'Выбрать (кредиты)', tg: 'Интихоб кардан (кредитҳо)', en: 'Select (credits)' },
    { ru: 'Выбрать (платно)', tg: 'Интихоб кардан (пардохтӣ)', en: 'Select (paid)' },
    { ru: 'Выбрать (цена неизвестна)', tg: 'Интихоб кардан (нарх номаълум)', en: 'Select (price unknown)' },
    { ru: 'Обычный текстовый чат', tg: 'Чати оддии матнӣ', en: 'Regular text chat' },
    { ru: 'Медиа', tg: 'Медиа', en: 'Media' },
    { ru: 'Изображение', tg: 'Тасвир', en: 'Image' },
    { ru: 'Аудио', tg: 'Аудио', en: 'Audio' },
    { ru: 'Видео', tg: 'Видео', en: 'Video' },
    { ru: 'Думаю…', tg: 'Фикр мекунам…', en: 'Thinking…' },
    { ru: 'Поищу в интернете', tg: 'Дар интернет меҷӯям', en: 'I will search the web' },
    { ru: 'Рассуждаю…', tg: 'Фикрронӣ мекунам…', en: 'Reasoning…' },
    { ru: 'Готово', tg: 'Тайёр', en: 'Done' },
    { ru: 'Неизвестная ошибка', tg: 'Хатои номаълум', en: 'Unknown error' },
    { ru: 'Скопировано в буфер обмена', tg: 'Ба буфер нусха шуд', en: 'Copied to clipboard' },
    { ru: 'Скопировано', tg: 'Нусха шуд', en: 'Copied' },
    { ru: 'Не удалось скопировать', tg: 'Нусхабардорӣ нашуд', en: 'Could not copy' },
    { ru: 'Браузер не поддерживает озвучку', tg: 'Браузер хондани овозиро дастгирӣ намекунад', en: 'The browser does not support speech synthesis' },
    { ru: '🔊 Читаю ответ…', tg: '🔊 Ҷавобро мехонам…', en: '🔊 Reading the answer…' },
    { ru: 'Остановить генерацию', tg: 'Генератсияро қатъ кардан', en: 'Stop generation' },
    { ru: 'Генерация остановлена', tg: 'Генератсия қатъ шуд', en: 'Generation stopped' },
    { ru: 'AI не вернул текст ответа', tg: 'AI матни ҷавобро барнагардонд', en: 'AI did not return an answer' },
    { ru: 'Поиск завершился без ответа модели', tg: 'Ҷустуҷӯ бе ҷавоби модел анҷом ёфт', en: 'Search finished without a model response' },
    { ru: 'Выполняю команду…', tg: 'Фармонро иҷро мекунам…', en: 'Executing command…' },
    { ru: 'Ошибка:', tg: 'Хато:', en: 'Error:' },
    { ru: 'Ошибка соединения', tg: 'Хатои пайвастшавӣ', en: 'Connection error' },
    { ru: 'неизвестная ошибка', tg: 'хатои номаълум', en: 'unknown error' },
    { ru: 'Поиск нашёл ссылки, но модель не ответила', tg: 'Ҷустуҷӯ истинодҳоро ёфт, вале модел ҷавоб надод', en: 'Search found links, but the model did not answer' },
    { ru: 'Поиск не удался — отвечаю без интернета', tg: 'Ҷустуҷӯ ноком шуд — бе интернет ҷавоб медиҳам', en: 'Search failed — answering without the internet' },
    { ru: 'Режим выключен — обычный чат', tg: 'Реҷа хомӯш шуд — чати оддӣ', en: 'Mode disabled — regular chat' },
    { ru: 'Скопировать ответ', tg: 'Нусхабардории ҷавоб', en: 'Copy answer' },
    { ru: 'Озвучить ответ', tg: 'Ҷавобро бо овоз хондан', en: 'Read answer aloud' },
    { ru: 'Ответить заново', tg: 'Аз нав ҷавоб додан', en: 'Regenerate answer' },
    { ru: 'Скрыть боковую панель', tg: 'Панели канориро пинҳон кардан', en: 'Hide sidebar' },
    { ru: 'Открыть боковую панель', tg: 'Панели канориро кушодан', en: 'Open sidebar' },
    { ru: 'Чат очищен', tg: 'Чат тоза шуд', en: 'Chat cleared' },
    { ru: 'Новый диалог', tg: 'Сӯҳбати нав', en: 'New chat' },
    { ru: 'Удалить чат', tg: 'Чатро нест кардан', en: 'Delete chat' },
    { ru: 'Сегодня', tg: 'Имрӯз', en: 'Today' },
    { ru: 'Вчера', tg: 'Дирӯз', en: 'Yesterday' },
    { ru: 'Последние 7 дней', tg: '7 рӯзи охир', en: 'Last 7 days' },
    { ru: 'Ранее', tg: 'Пештар', en: 'Earlier' },
    { ru: 'Команды', tg: 'Фармонҳо', en: 'Commands' },
    { ru: 'Настройки ИИ и провайдеров', tg: 'Танзимоти AI ва провайдерҳо', en: 'AI and provider settings' },
    { ru: 'Проверить поиск и модель', tg: 'Ҷустуҷӯ ва моделро санҷидан', en: 'Check search and model' },
    { ru: 'Озвучить последний ответ', tg: 'Ҷавоби охиринро бо овоз хондан', en: 'Read the last answer aloud' },
    { ru: 'Открыть панель медиа-моделей', tg: 'Панели моделҳои медиаро кушодан', en: 'Open media models panel' },
    { ru: 'Отключить активный режим', tg: 'Реҷаи фаъолро хомӯш кардан', en: 'Disable active mode' },
    { ru: 'Спокойный режим анимаций', tg: 'Реҷаи ороми аниматсияҳо', en: 'Calm animation mode' },
    { ru: 'Очистить чат', tg: 'Тоза кардани чат', en: 'Clear chat' },
    { ru: 'Включить/выключить поиск в интернете', tg: 'Ҷустуҷӯи интернетро фаъол/хомӯш кардан', en: 'Toggle web search' },
    { ru: 'Включить/выключить рассуждения', tg: 'Фикррониро фаъол/хомӯш кардан', en: 'Toggle reasoning' },
    { ru: 'Команда: список команд', tg: 'Фармон: рӯйхати фармонҳо', en: 'Command: command list' },
    { ru: 'Режим: погода', tg: 'Реҷа: ҳаво', en: 'Mode: weather' },
    { ru: 'Режим: курс валют', tg: 'Реҷа: курси асъор', en: 'Mode: exchange rates' },
    { ru: 'Режим: Википедия', tg: 'Реҷа: Википедия', en: 'Mode: Wikipedia' },
    { ru: 'Режим: код', tg: 'Реҷа: код', en: 'Mode: code' },
    { ru: 'Режим: изображение', tg: 'Реҷа: тасвир', en: 'Mode: image' },
    { ru: 'Запрос…', tg: 'Дархост…', en: 'Query…' },
    { ru: 'Введите город…', tg: 'Шаҳрро ворид кунед…', en: 'Enter a city…' },
    { ru: 'Какой код создать…', tg: 'Кадом код сохтан…', en: 'What code should I create…' },
    { ru: 'Опишите изображение…', tg: 'Тасвирро тавсиф кунед…', en: 'Describe the image…' },
    { ru: 'AI Ассистент', tg: 'AI Ёрдамчи', en: 'AI Assistant' },
    { ru: 'Офлайн-модель', tg: 'Модели офлайн', en: 'Offline model' },
    { ru: 'Соединение восстановлено', tg: 'Пайвастшавӣ барқарор шуд', en: 'Connection restored' },
    { ru: 'Нет соединения с интернетом', tg: 'Пайвастшавӣ ба интернет нест', en: 'No internet connection' },
    { ru: 'ИИ недоступен', tg: 'AI дастрас нест', en: 'AI unavailable' },
    { ru: 'Linux-окружение недоступно', tg: 'Муҳити Linux дастрас нест', en: 'Linux environment unavailable' },
    { ru: 'ИИ не вернул ответ', tg: 'AI ҷавоб барнагардонд', en: 'AI did not return a response' },
    { ru: 'Дождитесь окончания ответа', tg: 'То анҷоми ҷавоб интизор шавед', en: 'Wait for the response to finish' },
    { ru: 'Генерация остановлена.', tg: 'Генератсия қатъ шуд.', en: 'Generation stopped.' },
    { ru: 'Ошибка голосового ввода:', tg: 'Хатои воридкунии овозӣ:', en: 'Voice input error:' },
    { ru: 'Браузер не поддерживает голосовой ввод', tg: 'Браузер воридкунии овозиро дастгирӣ намекунад', en: 'The browser does not support voice input' },
    { ru: 'Нажмите для записи', tg: 'Барои сабт пахш кунед', en: 'Click to record' },
    { ru: 'Готово — проверьте текст', tg: 'Тайёр — матнро санҷед', en: 'Done — check the text' },
    { ru: 'Не удалось опросить задачу:', tg: 'Вазифаро пурсидан натавонист:', en: 'Could not poll the task:' },
    { ru: 'Провайдер отменил задачу', tg: 'Провайдер вазифаро бекор кард', en: 'The provider cancelled the task' },
    { ru: 'Браузер не поддерживает потоковый ответ', tg: 'Браузер ҷавоби ҷараёниро дастгирӣ намекунад', en: 'The browser does not support streaming responses' },
    { ru: 'Остановить', tg: 'Қатъ кардан', en: 'Stop' },
    { ru: 'План', tg: 'Нақша', en: 'Plan' },
    { ru: 'шаг', tg: 'қадам', en: 'step' },
    { ru: 'шага', tg: 'қадам', en: 'steps' },
    { ru: 'шагов', tg: 'қадам', en: 'steps' },
    { ru: 'к выполнению', tg: 'барои иҷро', en: 'to do' },
    { ru: 'в работе', tg: 'дар кор', en: 'in progress' },
    { ru: 'готово', tg: 'тайёр', en: 'done' },
    { ru: 'Задача', tg: 'Вазифа', en: 'Task' },
    { ru: 'офлайн', tg: 'офлайн', en: 'offline' },
    { ru: 'с поиском', tg: 'бо ҷустуҷӯ', en: 'with search' },
    { ru: 'модель', tg: 'модел', en: 'model' },
    { ru: 'источников', tg: 'манбаъ', en: 'sources' },
    { ru: 'ошибок', tg: 'хато', en: 'errors' },
    { ru: 'код', tg: 'код', en: 'code' },
    { ru: 'Объяснить этот код', tg: 'Ин кодро шарҳ додан', en: 'Explain this code' },
    { ru: 'Написать и запустить тесты', tg: 'Тестҳоро навиштан ва иҷро кардан', en: 'Write and run tests' },
    { ru: 'Скопировать код', tg: 'Кодро нусха кардан', en: 'Copy code' },
    { ru: 'Подключено', tg: 'Пайваст шуд', en: 'Connected' },
    { ru: 'Нажми — подключить', tg: 'Пахш кунед — пайваст шудан', en: 'Click to connect' },
    { ru: 'Настройки AI', tg: 'Танзимоти AI', en: 'AI settings' },
    { ru: 'Провайдеры не найдены', tg: 'Провайдерҳо ёфт нашуданд', en: 'No providers found' },
    { ru: 'Проверяю ключ и загружаю каталог моделей…', tg: 'Калидро месанҷам ва каталоги моделҳоро бор мекунам…', en: 'Checking the key and loading the model catalog…' },
    { ru: 'Модель доступна через выбранного провайдера.', tg: 'Модел тавассути провайдери интихобшуда дастрас аст.', en: 'The model is available through the selected provider.' },
    { ru: '✓ Используется в чате', tg: '✓ Дар чат истифода мешавад', en: '✓ Used in chat' },
    { ru: 'Выбрать модель', tg: 'Интихоби модел', en: 'Select model' },
    { ru: 'Не удалось выбрать модель', tg: 'Интихоби модел ноком шуд', en: 'Could not select model' },
    { ru: 'Проверяю актуальный каталог…', tg: 'Каталоги навро месанҷам…', en: 'Checking the latest catalog…' },
    { ru: 'Проверяю провайдеров и цены…', tg: 'Провайдерҳо ва нархҳоро месанҷам…', en: 'Checking providers and prices…' },
    { ru: 'Модели не найдены', tg: 'Моделҳо ёфт нашуданд', en: 'No models found' },
    { ru: 'Для этого типа моделей ничего не найдено', tg: 'Барои ин навъи моделҳо чизе ёфт нашуд', en: 'Nothing found for this model type' },
    { ru: 'Цены определены по данным провайдера/официального каталога', tg: 'Нархҳо аз маълумоти провайдер/каталоги расмӣ муайян шудаанд', en: 'Prices are based on provider data or the official catalog' },
    { ru: 'Проверяю поисковые бэкенды…', tg: 'Бэкендҳои ҷустуҷӯро месанҷам…', en: 'Checking search backends…' },
    { ru: 'Ни один поисковый бэкенд не ответил. Проверьте интернет или задайте SEARCH_BACKENDS / SEARXNG_INSTANCES в .env', tg: 'Ягон бэкенди ҷустуҷӯ ҷавоб надод. Интернетро санҷед ё SEARCH_BACKENDS / SEARXNG_INSTANCES-ро дар .env таъин кунед', en: 'No search backend responded. Check the internet or set SEARCH_BACKENDS / SEARXNG_INSTANCES in .env' },
    { ru: 'Бэкенды не настроены', tg: 'Бэкендҳо танзим нашудаанд', en: 'Backends are not configured' },
    { ru: 'нет ответа', tg: 'ҷавоб нест', en: 'no response' },
    { ru: 'бесплатно', tg: 'ройгон', en: 'free' },
    { ru: 'платный', tg: 'пардохтӣ', en: 'paid' },
    { ru: 'есть бесплатный тариф', tg: 'тарифи ройгон мавҷуд аст', en: 'free tier available' },
    { ru: 'нужен ключ', tg: 'калид лозим аст', en: 'key required' },
    { ru: 'доступен', tg: 'дастрас', en: 'available' },
    { ru: 'цена не определена', tg: 'нарх муайян нашудааст', en: 'price unknown' },
    { ru: 'Проверить доступность', tg: 'Дастрасиро санҷидан', en: 'Check availability' },
    { ru: 'Настроить ключ', tg: 'Калидро танзим кардан', en: 'Configure key' },
    { ru: 'Модель сейчас недоступна', tg: 'Модел ҳоло дастрас нест', en: 'Model is currently unavailable' },
    { ru: 'Проверьте ключ в .env и перезапустите сервер.', tg: 'Калидро дар .env санҷед ва серверро аз нав оғоз кунед.', en: 'Check the key in .env and restart the server.' },
    { ru: 'Ключ найден · каталог доступен', tg: 'Калид ёфт шуд · каталог дастрас аст', en: 'Key found · catalog available' },
    { ru: 'Не удалось загрузить провайдеров', tg: 'Боркунии провайдерҳо ноком шуд', en: 'Failed to load providers' },
    { ru: 'Не удалось получить media-каталог', tg: 'Каталоги медиа гирифта нашуд', en: 'Failed to get the media catalog' },
    // — служебные —
    { ru: 'бесплатно', tg: 'ройгон', en: 'free' },
  ];

  // Индексы: любая строка на любом из трёх языков → запись словаря.
  const INDEX = new Map();
  for (const entry of ENTRIES) {
    for (const lang of SUPPORTED) INDEX.set(entry[lang], entry);
  }

  // Шаблонные строки: динамическая часть сохраняется.
  const PATTERNS = [
    { re: /^Поисковый запрос: (.+)$/,
      tg: (m) => `Ҷустуҷӯи дархост: ${m[1]}`, en: (m) => `Search query: ${m[1]}` },
    { re: /^Запускаю (\d+) поисковых систем параллельно…$/,
      tg: (m) => `${m[1]} системаи ҷустуҷӯро параллелӣ оғоз мекунам…`, en: (m) => `Launching ${m[1]} search engines in parallel…` },
    { re: /^(.+): найдено (\d+) результатов за (\d+) мс$/,
      tg: (m) => `${m[1]}: ${m[2]} натиҷа дар ${m[3]} мс ёфт шуд`, en: (m) => `${m[1]}: found ${m[2]} results in ${m[3]} ms` },
    { re: /^Обрабатываю (\d+) источника(.*)$/,
      tg: (m) => `${m[1]} манбаъро коркард мекунам${m[2]}`, en: (m) => `Processing ${m[1]} sources${m[2]}` },
    { re: /^Первая волна пуста — пробую исходную формулировку…$/,
      tg: () => 'Мавҷи аввал холист — матни аслиро месанҷам…', en: () => 'First wave empty \u2014 trying the original wording…' },
    { re: /^Ищу через (.+)…$/, tg: (m) => `Аз ${m[1]} меҷӯям…`, en: (m) => `Searching via ${m[1]}…` },
    { re: /^Проверяю (.+)…$/, tg: (m) => `${m[1]}-ро месанҷам…`, en: (m) => `Checking ${m[1]}…` },
    { re: /^Поиск работает: доступно (\d+) из (\d+) бэкендов$/,
      tg: (m) => `Ҷустуҷӯ кор мекунад: аз ${m[2]} бэкенд ${m[1]} дастрас`, en: (m) => `Search works: ${m[1]} of ${m[2]} backends available` },
    { re: /^(\d+) моделей$/, tg: (m) => `${m[1]} модель`, en: (m) => `${m[1]} models` },
    { re: /^Нажмите «Выбрать», чтобы использовать модель в чате$/,
      tg: () => 'Барои истифодаи модель дар чат «Интихоб»-ро пахш кунед', en: () => 'Click \u201cSelect\u201d to use the model in chat' },
  ];

  // ─────────────────────────── движок ───────────────────────────

  let lang = normalizeLang(localStorage.getItem(STORAGE_KEY)) || DEFAULT_LANG;

  function normalizeLang(value) {
    const v = String(value || '').trim().toLowerCase();
    return SUPPORTED.includes(v) ? v : null;
  }

  function translateText(text) {
    if (!text) return text;
    const key = text.trim();
    if (!key) return text;
    const entry = INDEX.get(key);
    if (entry) return entry[lang];
    for (const p of PATTERNS) {
      const m = key.match(p.re);
      if (m) {
        const out = lang === 'ru' ? key : p[lang](m);
        return text.replace(key, out);
      }
    }
    return text;
  }

  const ATTRS = ['placeholder', 'title', 'aria-label', 'data-default-placeholder'];
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'CODE', 'PRE']);

  function translateTree(root) {
    if (!root || root.nodeType !== 1 || SKIP_TAGS.has(root.tagName)) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => {
        const parent = node.parentElement;
        if (!parent || SKIP_TAGS.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
        return node.nodeValue && node.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    let node;
    while ((node = walker.nextNode())) {
      const next = translateText(node.nodeValue);
      if (next !== node.nodeValue) node.nodeValue = next;
    }
    for (const attr of ATTRS) {
      if (root.hasAttribute && root.hasAttribute(attr)) {
        const value = root.getAttribute(attr);
        const next = translateText(value);
        if (next !== value) root.setAttribute(attr, next);
      }
    }
    root.querySelectorAll && root.querySelectorAll('*').forEach((el) => {
      if (SKIP_TAGS.has(el.tagName)) return;
      for (const attr of ATTRS) {
        if (!el.hasAttribute(attr)) continue;
        const value = el.getAttribute(attr);
        const next = translateText(value);
        if (next !== value) el.setAttribute(attr, next);
      }
    });
  }

  let scheduled = false;
  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      translateTree(document.body);
      paintLangButtons();
    });
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === 'childList') {
        for (const node of m.addedNodes) {
          if (node.nodeType === 1) translateTree(node);
          else if (node.nodeType === 3) {
            const next = translateText(node.nodeValue);
            if (next !== node.nodeValue) node.nodeValue = next;
          }
        }
      } else if (m.type === 'characterData' && m.target.nodeValue) {
        const next = translateText(m.target.nodeValue);
        if (next !== m.target.nodeValue) m.target.nodeValue = next;
      } else if (m.type === 'attributes') {
        const value = m.target.getAttribute(m.attributeName);
        const next = translateText(value);
        if (next !== value) m.target.setAttribute(m.attributeName, next);
      }
    }
  });

  function paintLangButtons() {
    document.querySelectorAll('[data-lang]').forEach((el) => {
      el.classList.toggle('active', el.dataset.lang === lang);
    });
  }

  async function setLang(next) {
    const value = normalizeLang(next);
    if (!value || value === lang) return;
    lang = value;
    try { localStorage.setItem(STORAGE_KEY, value); } catch (_) { /* private mode */ }
    // Сообщаем серверу — язык переживёт перезапуск и другие устройства.
    try {
      await fetch('/api/settings/lang', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language: value }),
      });
    } catch (_) { /* сервер недоступен — язык всё равно сохранён локально */ }
    location.reload(); // чистый перезапуск гарантирует согласованность всего UI
  }

  function boot() {
    document.documentElement.lang = lang === 'tg' ? 'tg' : lang;
    if (!localStorage.getItem(STORAGE_KEY)) {
      // Первый визит: спросим сервер (вдруг язык уже выбран на другом устройстве).
      fetch('/api/settings/lang')
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          const serverLang = normalizeLang(data && data.language);
          if (serverLang && serverLang !== lang) {
            lang = serverLang;
            try { localStorage.setItem(STORAGE_KEY, serverLang); } catch (_) {}
            location.reload();
          }
        })
        .catch(() => {});
    }
    translateTree(document.body);
    document.addEventListener('click', (event) => {
      const button = event.target.closest('[data-lang]');
      if (button) setLang(button.dataset.lang);
    });
    observer.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS,
    });
    paintLangButtons();
  }

  window.I18N = { getLang: () => lang, setLang, translateText, apply: scheduleApply };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
