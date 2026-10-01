/* Khirad · NovaMind — язык интерфейса (тоҷикӣ / русский / English).
 *
 * Ключ перевода — сама русская строка, поэтому шаблоны остаются читаемыми,
 * а если перевода нет, показывается русский текст.
 *   t('Новый диалог')                 → «Сӯҳбати нав» / «New chat»
 *   t('Видео: {name}', { name })      → подстановка
 *   tn(5, 'шаг', 'шага', 'шагов')     → склонение по числу
 * Статичный текст страниц переводится автоматически (I18N.apply) —
 * кроме сообщений чата, истории и кода. Язык по умолчанию — таджикский.
 * Скрипт подключается в <head> раньше остальных.
 */
(function () {
  'use strict';

  const LANGS = [
    { code: 'tg', name: 'Тоҷикӣ', short: 'TJ' },
    { code: 'ru', name: 'Русский', short: 'RU' },
    { code: 'en', name: 'English', short: 'EN' },
  ];
  const DEFAULT_LANG = 'tg';
  const KEY = 'nova_lang';

  // 'ключ': [тоҷикӣ, English]. В английском вместо строки может стоять
  // [ед. число, мн. число] — для tn().
  const DICT = {
    // ── язык ──
    'Язык интерфейса': ['Забони интерфейс', 'Interface language'],
    'Язык': ['Забон', 'Language'],
    'Весь интерфейс и ответы ИИ переключатся на выбранный язык.': ['Тамоми интерфейс ва ҷавобҳои зеҳни сунъӣ ба забони интихобшуда мегузаранд.', 'The whole interface and the AI replies will switch to the selected language.'],
    'Язык изменён': ['Забон иваз шуд', 'Language changed'],
    '🌐 Язык': ['🌐 Забон', '🌐 Language'],
    '🌐 ЯЗЫК': ['🌐 ЗАБОН', '🌐 LANGUAGE'],

    // ── режимы из боковой панели ──
    '🔎 Веб-поиск': ['🔎 Ҷустуҷӯ дар веб', '🔎 Web search'],
    'Введите запрос для поиска...': ['Дархости ҷустуҷӯро ворид кунед...', 'Enter a search query...'],
    '🎨 Генерация изображений': ['🎨 Эҷоди тасвир', '🎨 Image generation'],
    'Опишите изображение для генерации...': ['Тасвири лозимиро тавсиф кунед...', 'Describe the image to generate...'],
    'Опишите, какой код создать...': ['Тавсиф кунед, ки кадом код сохта шавад...', 'Describe the code to write...'],
    'Введите город для погоды...': ['Шаҳрро барои обу ҳаво ворид кунед...', 'Enter a city for the weather...'],
    'Введите валюты (USD RUB)...': ['Асъорҳоро ворид кунед (USD TJS)...', 'Enter currencies (USD EUR)...'],
    'Введите запрос для Википедии...': ['Дархост барои Википедия ворид кунед...', 'Enter a Wikipedia query...'],

    // ── главная: боковая панель ──
    'Новый диалог': ['Сӯҳбати нав', 'New chat'],
    'Основные': ['Асосӣ', 'Main'],
    'Чат': ['Чат', 'Chat'],
    'Веб-поиск': ['Ҷустуҷӯ дар веб', 'Web search'],
    'Генерация изображений': ['Эҷоди тасвир', 'Image generation'],
    'Помощник кода': ['Ёрдамчии код', 'Code assistant'],
    'Сервисы': ['Хизматҳо', 'Services'],
    'Погода': ['Обу ҳаво', 'Weather'],
    'Курс валют': ['Қурби асъор', 'Exchange rates'],
    'Википедия': ['Википедия', 'Wikipedia'],
    'История чатов': ['Таърихи чатҳо', 'Chat history'],
    'Новый чат': ['Чати нав', 'New chat'],
    '+ Новый': ['+ Нав', '+ New'],
    'Панель управления': ['Панели идоракунӣ', 'Control panel'],
    'Настройки и ключи': ['Танзимот ва калидҳо', 'Settings and keys'],
    'Настройки AI': ['Танзимоти AI', 'AI settings'],
    'Пользователь': ['Корбар', 'User'],
    'Базовый план': ['Нақшаи асосӣ', 'Basic plan'],
    'Выйти': ['Баромадан', 'Log out'],
    'Открыть боковую панель': ['Кушодани панели паҳлӯ', 'Open sidebar'],
    'Скрыть боковую панель': ['Пинҳон кардани панели паҳлӯ', 'Hide sidebar'],
    'Состояние ИИ-движка': ['Ҳолати муҳаррики AI', 'AI engine status'],
    'AI Ассистент': ['Ёрдамчии AI', 'AI Assistant'],
    'Очистить чат': ['Тоза кардани чат', 'Clear chat'],
    'Привет! Я': ['Салом! Ман', 'Hi! I am'],
    'Просто напишите, что нужно. Я сам решу, как это сделать: отвечу, найду в интернете, нарисую картинку, озвучу текст, сделаю видео или поработаю в Linux.': ['Танҳо нависед, ки чӣ лозим аст. Худам қарор медиҳам, ки чӣ тавр иҷро кунам: ҷавоб медиҳам, дар интернет меёбам, расм мекашам, матнро садо медиҳам, видео месозам ё дар Linux кор мекунам.', 'Just write what you need. I will decide how to do it myself: answer, search the web, draw a picture, voice a text, make a video or work in Linux.'],
    'Автопоиск: ИИ сначала ищет в интернете и отвечает с источниками': ['Ҷустуҷӯи худкор: AI аввал дар интернет меҷӯяд ва бо манбаъҳо ҷавоб медиҳад', 'Auto-search: the AI searches the web first and answers with sources'],
    'Поиск': ['Ҷустуҷӯ', 'Search'],
    'Поиск вкл': ['Ҷустуҷӯ фаъол', 'Search on'],
    'Состояние поисковых бэкендов': ['Ҳолати системаҳои ҷустуҷӯ', 'Search backends status'],
    'Показывать ход рассуждений модели': ['Нишон додани раванди андешаи модел', 'Show the model reasoning'],
    'Рассуждение': ['Андеша', 'Reasoning'],
    'Прикрепить файл': ['Замима кардани файл', 'Attach file'],
    'Изображение': ['Тасвир', 'Image'],
    'Файл': ['Файл', 'File'],
    '🎧 Аудио': ['🎧 Аудио', '🎧 Audio'],
    '🎬 Видео': ['🎬 Видео', '🎬 Video'],
    'Выключить режим': ['Хомӯш кардани реҷа', 'Turn off mode'],
    'Напишите сообщение...': ['Паём нависед...', 'Write a message...'],
    'Голосовой ввод': ['Вуруди овозӣ', 'Voice input'],
    'Нажмите для записи': ['Барои сабт пахш кунед', 'Tap to record'],
    'Отправить (Enter)': ['Фиристодан (Enter)', 'Send (Enter)'],
    'Khirad может ошибаться. Проверяйте важную информацию': ['Khirad метавонад хато кунад. Маълумоти муҳимро санҷед', 'Khirad can make mistakes. Check important information'],
    'Команды': ['Фармонҳо', 'Commands'],
    'Команда, режим или действие…': ['Фармон, реҷа ё амал…', 'Command, mode or action…'],

    // ── главная: уведомления и статусы ──
    'Копирование интерфейса запрещено. Выделите текст в сообщении.': ['Нусхабардории интерфейс манъ аст. Матнро дар паём ҷудо кунед.', 'Copying the interface is disabled. Select text inside a message.'],
    '🔍 Автопоиск включён — ИИ ищет в интернете через Linux и отвечает с источниками': ['🔍 Ҷустуҷӯи худкор фаъол аст — AI тавассути Linux дар интернет меҷӯяд ва бо манбаъҳо ҷавоб медиҳад', '🔍 Auto-search is on — the AI searches the web via Linux and answers with sources'],
    'Автопоиск выключен': ['Ҷустуҷӯи худкор хомӯш аст', 'Auto-search is off'],
    '🧠 Режим рассуждения включён — покажу ход мыслей модели': ['🧠 Реҷаи андеша фаъол аст — раванди фикри моделро нишон медиҳам', '🧠 Reasoning mode is on — I will show the model\'s train of thought'],
    'Рассуждение выключено': ['Андеша хомӯш аст', 'Reasoning is off'],
    '🔍 Авто-поиск включён': ['🔍 Ҷустуҷӯи худкор фаъол аст', '🔍 Auto-search is on'],
    'Авто-поиск выключен': ['Ҷустуҷӯи худкор хомӯш аст', 'Auto-search is off'],
    'Подробно опиши изображение, распознай текст на нём и объясни важные детали.': ['Тасвирро муфассал тавсиф кун, матни дар он бударо шинос ва ҷузъиёти муҳимро шарҳ деҳ.', 'Describe the image in detail, recognise any text in it and explain the important details.'],
    'Проанализируй этот файл и объясни главное. Если это код — найди ошибки и предложи исправления.': ['Ин файлро таҳлил кун ва асосиашро шарҳ деҳ. Агар код бошад — хатогиҳоро ёб ва ислоҳ пешниҳод кун.', 'Analyse this file and explain the key points. If it is code, find bugs and suggest fixes.'],
    '📷 Что сделать с изображением?\n\nОставь пустым — AI сам распознает изображение и текст.': ['📷 Бо тасвир чӣ кор кунам?\n\nХолӣ гузоред — AI худаш тасвир ва матнро шинос мекунад.', '📷 What should I do with the image?\n\nLeave empty — the AI will recognise the image and the text itself.'],
    '📁 Что сделать с файлом?\n\nМожно написать: найди ошибки, сделай резюме, объясни код и т.д.': ['📁 Бо файл чӣ кор кунам?\n\nМетавонед нависед: хатогиҳоро ёб, хулоса соз, кодро шарҳ деҳ ва ғайра.', '📁 What should I do with the file?\n\nFor example: find bugs, summarise it, explain the code, etc.'],
    'Читаю файл…': ['Файлро мехонам…', 'Reading the file…'],
    'Сервер вернул не JSON (HTTP': ['Сервер JSON барнагардонд (HTTP', 'The server did not return JSON (HTTP'],
    'Ошибка обработки файла: HTTP': ['Хатои коркарди файл: HTTP', 'File processing error: HTTP'],
    'Анализ завершён, но ответ пустой.': ['Таҳлил анҷом ёфт, вале ҷавоб холӣ аст.', 'Analysis finished, but the answer is empty.'],
    'Ошибка анализа вложения': ['Хатои таҳлили замима', 'Attachment analysis error'],
    'Расшифровываю аудио…': ['Аудиоро ба матн мегардонам…', 'Transcribing audio…'],
    'Текст не распознан.': ['Матн шинохта нашуд.', 'No text recognised.'],
    '**Расшифровка {name}:**\n\n{v1}': ['**Матни {name}:**\n\n{v1}', '**Transcript of {name}:**\n\n{v1}'],
    'Ошибка загрузки аудио': ['Хатои боркунии аудио', 'Audio upload error'],
    'Загружаю видео…': ['Видеоро бор мекунам…', 'Uploading video…'],
    'Видео: {name}': ['Видео: {name}', 'Video: {name}'],
    '❌ Ошибка загрузки видео': ['❌ Хатои боркунии видео', '❌ Video upload error'],
    'Открыть файл': ['Кушодани файл', 'Open file'],
    'Аудио': ['Аудио', 'Audio'],
    'Видео': ['Видео', 'Video'],
    'видео': ['видео', 'video'],
    '⬇ Скачать': ['⬇ Боргирӣ', '⬇ Download'],
    'Скачать': ['Боргирӣ', 'Download'],
    'Открыть в новой вкладке': ['Дар варақаи нав кушодан', 'Open in a new tab'],
    'Сгенерировано ИИ': ['Аз ҷониби AI сохта шуд', 'Generated by AI'],
    'ИИ': ['AI', 'AI'],
    'Не удалось узнать статус:': ['Ҳолатро фаҳмидан нашуд:', 'Could not get the status:'],
    'Провайдер отменил задачу': ['Провайдер вазифаро бекор кард', 'The provider cancelled the task'],
    'Видео готовится у провайдера… ({state}, проверок: {polls})': ['Видео дар назди провайдер омода мешавад… ({state}, санҷишҳо: {polls})', 'The provider is preparing the video… ({state}, checks: {polls})'],
    'Видео всё ещё готовится — загляните позже.': ['Видео ҳоло ҳам омода мешавад — баъдтар нигоҳ кунед.', 'The video is still being prepared — check back later.'],
    '🎬 Задача видео у провайдера': ['🎬 Вазифаи видео дар назди провайдер', '🎬 Video task at the provider'],
    'Статус:': ['Ҳолат:', 'Status:'],
    'ID задачи:': ['ID-и вазифа:', 'Task ID:'],
    'Не удалось опросить задачу:': ['Ҳолати вазифаро санҷидан нашуд:', 'Could not poll the task:'],
    'Статус провайдера: {state} (опросов: {polls})': ['Ҳолати провайдер: {state} (санҷишҳо: {polls})', 'Provider status: {state} (polls: {polls})'],
    'Опрос остановлен: задача всё ещё выполняется у провайдера.': ['Санҷиш қатъ шуд: вазифа ҳоло дар назди провайдер иҷро мешавад.', 'Polling stopped: the task is still running at the provider.'],
    'Видео готовится у провайдера…': ['Видео дар назди провайдер омода мешавад…', 'The provider is preparing the video…'],
    'Думаю…': ['Фикр мекунам…', 'Thinking…'],
    'Рассуждаю…': ['Андеша мекунам…', 'Reasoning…'],
    'Поищу в интернете': ['Дар интернет меҷӯям', 'Searching the web'],
    'План': ['Нақша', 'Plan'],
    'Шаги ИИ': ['Қадамҳои AI', 'AI steps'],
    'Работа в Linux': ['Кор дар Linux', 'Working in Linux'],
    'шаг': ['қадам', ['step', 'steps']],
    'шага': ['қадам', ['step', 'steps']],
    'шагов': ['қадам', ['step', 'steps']],
    '· ошибок: {done}': ['· хатоҳо: {done}', '· errors: {done}'],
    'к выполнению': ['дар навбат', 'to do'],
    'в работе': ['дар ҷараён', 'in progress'],
    'готово': ['тайёр', 'done'],
    'Задача': ['Вазифа', 'Task'],
    '· шагов: {steps}': ['· қадамҳо: {steps}', '· steps: {steps}'],
    '🔗 Источники ·': ['🔗 Манбаъҳо ·', '🔗 Sources ·'],
    'модель: {v0}': ['модел: {v0}', 'model: {v0}'],
    'модель:': ['модел:', 'model:'],
    'офлайн': ['офлайн', 'offline'],
    'с поиском': ['бо ҷустуҷӯ', 'with search'],
    'шагов: {steps}': ['қадамҳо: {steps}', 'steps: {steps}'],
    'источников: {length}': ['манбаъҳо: {length}', 'sources: {length}'],
    'Скопировать ответ': ['Нусха гирифтани ҷавоб', 'Copy the answer'],
    '📋 Копировать': ['📋 Нусха', '📋 Copy'],
    'Озвучить ответ': ['Садо додани ҷавоб', 'Read the answer aloud'],
    '🔊 Озвучить': ['🔊 Садо додан', '🔊 Read aloud'],
    'Ответить заново': ['Аз нав ҷавоб додан', 'Answer again'],
    '↻ Заново': ['↻ Аз нав', '↻ Again'],
    '↻ Повторить': ['↻ Такрор', '↻ Retry'],
    'Неизвестная ошибка': ['Хатои номаълум', 'Unknown error'],
    'Скопировано в буфер обмена': ['Ба буфер нусха шуд', 'Copied to clipboard'],
    'Скопировано': ['Нусха шуд', 'Copied'],
    'Не удалось скопировать': ['Нусха гирифтан нашуд', 'Could not copy'],
    'Браузер не поддерживает озвучку': ['Браузер садодиҳиро дастгирӣ намекунад', 'The browser does not support speech'],
    '🔊 Читаю ответ…': ['🔊 Ҷавобро мехонам…', '🔊 Reading the answer…'],
    'Браузер не поддерживает потоковый ответ': ['Браузер ҷавоби ҷараёниро дастгирӣ намекунад', 'The browser does not support streaming responses'],
    'Остановить генерацию': ['Қатъ кардани эҷод', 'Stop generating'],
    'Генерация остановлена': ['Эҷод қатъ шуд', 'Generation stopped'],
    '_Генерация остановлена._': ['_Эҷод қатъ шуд._', '_Generation stopped._'],
    '_Остановлено._': ['_Қатъ шуд._', '_Stopped._'],
    'AI не вернул текст ответа': ['AI матни ҷавобро барнагардонд', 'The AI returned no answer text'],
    'Поиск завершился без ответа модели': ['Ҷустуҷӯ бе ҷавоби модел анҷом ёфт', 'The search finished without a model answer'],
    'Выполняю команду…': ['Фармонро иҷро мекунам…', 'Running the command…'],
    'Ошибка:': ['Хато:', 'Error:'],
    'Готово': ['Тайёр', 'Done'],
    'Ошибка соединения': ['Хатои пайвастшавӣ', 'Connection error'],
    '_Источники найдены, но ответ модель не отдала._': ['_Манбаъҳо ёфт шуданд, вале модел ҷавоб надод._', '_Sources were found, but the model gave no answer._'],
    'Откройте ссылки ниже или повторите запрос — обычно помогает': ['Пайвандҳои поёнро кушоед ё дархостро такрор кунед — одатан кумак мекунад', 'Open the links below or repeat the request — what usually helps is'],
    'смена модели или повторный запуск поиска.': ['иваз кардани модел ё аз нав оғоз кардани ҷустуҷӯ.', 'switching the model or running the search again.'],
    'Поиск нашёл ссылки, но модель не ответила': ['Ҷустуҷӯ пайвандҳо ёфт, вале модел ҷавоб надод', 'The search found links, but the model did not answer'],
    'Поиск не удался — отвечаю без интернета': ['Ҷустуҷӯ нашуд — бе интернет ҷавоб медиҳам', 'Search failed — answering without the internet'],
    '{message} · затем {message_}': ['{message} · баъд {message_}', '{message} · then {message_}'],
    'Режим выключен — обычный чат': ['Реҷа хомӯш шуд — чати оддӣ', 'Mode off — regular chat'],
    'Вы': ['Шумо', 'You'],
    'код': ['код', 'code'],
    'ИИ запустит этот код в Linux и покажет результат': ['AI ин кодро дар Linux иҷро карда, натиҷаро нишон медиҳад', 'The AI will run this code in Linux and show the result'],
    'Запустить': ['Иҷро', 'Run'],
    'Объяснить этот код': ['Шарҳ додани ин код', 'Explain this code'],
    'Объясни': ['Шарҳ', 'Explain'],
    'Написать и запустить тесты': ['Навиштан ва иҷрои тестҳо', 'Write and run tests'],
    'Тесты': ['Тестҳо', 'Tests'],
    'Скопировать код': ['Нусха гирифтани код', 'Copy code'],
    '← прокрути вправо →': ['← ба рост лағжонед →', '← scroll sideways →'],
    'Браузер не поддерживает голосовой ввод': ['Браузер вуруди овозиро дастгирӣ намекунад', 'The browser does not support voice input'],
    '● Слушаю... Нажмите для остановки': ['● Гӯш мекунам... Барои қатъ пахш кунед', '● Listening... Tap to stop'],
    'Готово — проверьте текст': ['Тайёр — матнро санҷед', 'Done — check the text'],
    'Ошибка голосового ввода:': ['Хатои вуруди овозӣ:', 'Voice input error:'],
    'Не удалось запустить голосовой ввод': ['Вуруди овозиро оғоз кардан нашуд', 'Could not start voice input'],
    'Чат очищен': ['Чат тоза шуд', 'Chat cleared'],
    'Начни диалог —': ['Сӯҳбатро оғоз кунед —', 'Start a conversation —'],
    'он появится здесь': ['он дар ин ҷо пайдо мешавад', 'it will appear here'],
    'сообщ.': ['паём', 'msg.'],
    'Удалить чат': ['Нест кардани чат', 'Delete chat'],
    'Сегодня': ['Имрӯз', 'Today'],
    'Вчера': ['Дирӯз', 'Yesterday'],
    'Последние 7 дней': ['7 рӯзи охир', 'Last 7 days'],
    'Ранее': ['Пештар', 'Earlier'],

    // ── палитра команд (Ctrl+K) ──
    'Включить/выключить поиск в интернете': ['Фаъол/хомӯш кардани ҷустуҷӯ дар интернет', 'Toggle web search'],
    'Включить/выключить рассуждения': ['Фаъол/хомӯш кардани андеша', 'Toggle reasoning'],
    'Выключить активный режим': ['Хомӯш кардани реҷаи фаъол', 'Turn off the active mode'],
    'Настройки ИИ и провайдеров': ['Танзимоти AI ва провайдерҳо', 'AI and provider settings'],
    'Проверить поиск и модель': ['Санҷидани ҷустуҷӯ ва модел', 'Check search and model'],
    'Озвучить последний ответ': ['Садо додани ҷавоби охирин', 'Read the last answer aloud'],
    'Команда: список команд': ['Фармон: рӯйхати фармонҳо', 'Command: list of commands'],
    'Режим: погода': ['Реҷа: обу ҳаво', 'Mode: weather'],
    '🌤 Погода': ['🌤 Обу ҳаво', '🌤 Weather'],
    'Введите город…': ['Шаҳрро ворид кунед…', 'Enter a city…'],
    'Режим: курс валют': ['Реҷа: қурби асъор', 'Mode: exchange rates'],
    '💱 Курс валют': ['💱 Қурби асъор', '💱 Exchange rates'],
    'Режим: Википедия': ['Реҷа: Википедия', 'Mode: Wikipedia'],
    '📚 Википедия': ['📚 Википедия', '📚 Wikipedia'],
    'Запрос…': ['Дархост…', 'Query…'],
    'Режим: код': ['Реҷа: код', 'Mode: code'],
    '💻 Помощник кода': ['💻 Ёрдамчии код', '💻 Code assistant'],
    'Какой код создать…': ['Кадом кодро созам…', 'What code should I write…'],
    'Ничего не найдено': ['Ҳеҷ чиз ёфт нашуд', 'Nothing found'],
    'модель': ['модел', ['model', 'models']],
    'модели': ['модел', ['model', 'models']],
    'моделей': ['модел', ['model', 'models']],
    'Офлайн-модель · {model}': ['Модели офлайн · {model}', 'Offline model · {model}'],
    'Поиск работает: {v0}': ['Ҷустуҷӯ кор мекунад: {v0}', 'Search works: {v0}'],
    'Поисковые бэкенды недоступны — проверьте интернет или SEARCH_BACKENDS в .env': ['Системаҳои ҷустуҷӯ дастрас нестанд — интернет ё SEARCH_BACKENDS-ро дар .env санҷед', 'Search backends are unavailable — check the internet or SEARCH_BACKENDS in .env'],
    'Не удалось проверить поиск:': ['Ҷустуҷӯро санҷидан нашуд:', 'Could not check search:'],
    '✅ Подключено': ['✅ Пайваст аст', '✅ Connected'],
    'Нажми — подключить': ['Пахш кунед — пайваст кардан', 'Tap to connect'],
    '🧩 Интеграции Composio — нажми для подключения:': ['🧩 Ҳамгироиҳои Composio — барои пайвастшавӣ пахш кунед:', '🧩 Composio integrations — tap to connect:'],
    '💡 После подключения используй': ['💡 Пас аз пайвастшавӣ истифода баред', '💡 After connecting, use'],
    'для проверки': ['барои санҷиш', 'to check'],
    'Подключить': ['Пайваст кардан', 'Connect'],
    'Нажми кнопку ниже — откроется страница авторизации.': ['Тугмаи поёнро пахш кунед — саҳифаи воридшавӣ кушода мешавад.', 'Tap the button below — the authorisation page will open.'],
    'После входа вернись и введи': ['Пас аз воридшавӣ баргардед ва ворид кунед', 'After signing in, come back and enter'],
    '🔐 Войти →': ['🔐 Ворид шудан →', '🔐 Sign in →'],
    'После:': ['Баъд:', 'Then:'],
    'Ctrl+K — команды · Enter — отправить · Shift+Enter — новая строка · Esc — закрыть': ['Ctrl+K — фармонҳо · Enter — фиристодан · Shift+Enter — сатри нав · Esc — пӯшидан', 'Ctrl+K — commands · Enter — send · Shift+Enter — new line · Esc — close'],
    'Соединение восстановлено': ['Пайвастшавӣ барқарор шуд', 'Connection restored'],
    'Нет соединения с интернетом': ['Пайвастшавӣ ба интернет нест', 'No internet connection'],

    // ── ИИ-агент (linux.js) ──
    'ИИ-агент недоступен': ['Агенти AI дастрас нест', 'The AI agent is unavailable'],
    'ищет в интернете': ['дар интернет меҷӯяд', 'searches the web'],
    'рисует, озвучивает и делает видео': ['расм мекашад, садо медиҳад ва видео месозад', 'draws, voices and makes videos'],
    'работает в Linux (код, файлы, терминал)': ['дар Linux кор мекунад (код, файлҳо, терминал)', 'works in Linux (code, files, terminal)'],
    'ИИ-агент сам выбирает, что делать: {v0}': ['Агенти AI худаш интихоб мекунад, ки чӣ кор кунад: {v0}', 'The AI agent decides what to do: {v0}'],
    'ИИ не вернул ответ': ['AI ҷавоб надод', 'The AI returned no answer'],
    'ИИ недоступен': ['AI дастрас нест', 'The AI is unavailable'],
    'Запусти этот код в Linux-окружении и покажи, что он выводит. Если есть ошибки — исправь и запусти снова.': ['Ин кодро дар муҳити Linux иҷро кун ва нишон деҳ, ки чӣ мебарорад. Агар хато бошад — ислоҳ карда, аз нав иҷро кун.', 'Run this code in the Linux environment and show its output. If there are errors, fix them and run it again.'],
    'Объясни этот код по пунктам: что делает, где может сломаться, что улучшить.': ['Ин кодро банд ба банд шарҳ деҳ: чӣ кор мекунад, дар куҷо метавонад вайрон шавад, чиро беҳтар кардан мумкин аст.', 'Explain this code point by point: what it does, where it can break, what to improve.'],
    'Напиши тесты к этому коду, запусти их в Linux-окружении и покажи результат.': ['Барои ин код тестҳо навис, онҳоро дар муҳити Linux иҷро кун ва натиҷаро нишон деҳ.', 'Write tests for this code, run them in the Linux environment and show the result.'],
    'Дождитесь окончания ответа': ['Анҷоми ҷавобро интизор шавед', 'Wait for the answer to finish'],

    // ── настройки ──
    'Настройки AI — Khirad': ['Танзимоти AI — Khirad', 'AI settings — Khirad'],
    'Вернуться в чат': ['Бозгашт ба чат', 'Back to chat'],
    '← Вернуться в чат': ['← Бозгашт ба чат', '← Back to chat'],
    'Загрузка…': ['Бор шуда истодааст…', 'Loading…'],
    'ПРОВАЙДЕРЫ И МОДЕЛИ': ['ПРОВАЙДЕРҲО ВА МОДЕЛҲО', 'PROVIDERS AND MODELS'],
    'Выберите движок для чата': ['Муҳаррики чатро интихоб кунед', 'Choose the chat engine'],
    'Список моделей проверяется напрямую через API провайдера. Ключи остаются только на backend и не передаются в браузер.': ['Рӯйхати моделҳо бевосита тавассути API-и провайдер санҷида мешавад. Калидҳо танҳо дар backend мемонанд ва ба браузер фиристода намешаванд.', 'The model list is checked directly through the provider API. Keys stay on the backend and are never sent to the browser.'],
    '↻ Обновить всё': ['↻ Ҳамаро нав кардан', '↻ Refresh all'],
    'Загружаю провайдеров…': ['Провайдерҳоро бор мекунам…', 'Loading providers…'],
    'Доступные модели': ['Моделҳои дастрас', 'Available models'],
    'Выберите провайдера': ['Провайдерро интихоб кунед', 'Choose a provider'],
    '↻ Обновить список': ['↻ Нав кардани рӯйхат', '↻ Refresh list'],
    'Фильтры моделей': ['Филтрҳои модел', 'Model filters'],
    'Поиск модели…': ['Ҷустуҷӯи модел…', 'Search models…'],
    'Цена': ['Нарх', 'Price'],
    'Цена: все': ['Нарх: ҳама', 'Price: all'],
    'Только бесплатные': ['Танҳо ройгон', 'Free only'],
    'Только платные': ['Танҳо пулакӣ', 'Paid only'],
    'Сортировка': ['Тартиб', 'Sorting'],
    'Сначала рекомендуемые': ['Аввал тавсияшуда', 'Recommended first'],
    'Большой контекст': ['Контексти калон', 'Large context'],
    'Огромные веса': ['Вазнҳои азим', 'Huge weights'],
    'Сначала бесплатные': ['Аввал ройгон', 'Free first'],
    'Ниже цена': ['Нархи пасттар', 'Lower price'],
    'Новые модели': ['Моделҳои нав', 'New models'],
    'Тип модели': ['Навъи модел', 'Model type'],
    'Тип: любой': ['Навъ: ҳар кадом', 'Type: any'],
    'Текст': ['Матн', 'Text'],
    'Текст + изображения': ['Матн + тасвирҳо', 'Text + images'],
    'Контекст': ['Контекст', 'Context'],
    'Контекст: любой': ['Контекст: ҳар кадом', 'Context: any'],
    'От 32K токенов': ['Аз 32K токен', 'From 32K tokens'],
    'От 100K токенов': ['Аз 100K токен', 'From 100K tokens'],
    'От 200K токенов': ['Аз 200K токен', 'From 200K tokens'],
    '0 моделей': ['0 модел', '0 models'],
    'Нажмите «Выбрать», чтобы использовать модель в чате': ['Барои истифодаи модел дар чат «Интихоб»-ро пахш кунед', 'Press “Select” to use a model in the chat'],
    'Модели ещё не загружены': ['Моделҳо ҳанӯз бор нашудаанд', 'Models are not loaded yet'],
    'Модели для генерации': ['Моделҳо барои эҷод', 'Generation models'],
    'Живой каталог для изображений, аудио и видео с анализом бесплатного тарифа и стоимости.': ['Феҳристи зинда барои тасвир, аудио ва видео бо таҳлили тарифи ройгон ва нарх.', 'A live catalogue of image, audio and video models with free-tier and price analysis.'],
    '↻ Проверить API': ['↻ Санҷидани API', '↻ Check API'],
    '🖼️ Изображения': ['🖼️ Тасвирҳо', '🖼️ Images'],
    '🔊 Аудио': ['🔊 Аудио', '🔊 Audio'],
    'Загружаю media-модели…': ['Моделҳои медиаро бор мекунам…', 'Loading media models…'],
    'Поиск в интернете': ['Ҷустуҷӯ дар интернет', 'Web search'],
    'Проверяю поисковые бэкенды…': ['Системаҳои ҷустуҷӯро месанҷам…', 'Checking search backends…'],
    '↻ Проверить': ['↻ Санҷидан', '↻ Check'],
    'Проверка бэкендов…': ['Санҷиши системаҳо…', 'Checking backends…'],
    'Бесплатные модели': ['Моделҳои ройгон', 'Free models'],
    'OpenRouter помечает бесплатные модели суффиксом': ['OpenRouter моделҳои ройгонро бо пасванди', 'OpenRouter marks free models with the suffix'],
    '. Такие модели могут иметь ограничения по скорости.': ['қайд мекунад. Чунин моделҳо метавонанд маҳдудияти суръат дошта бошанд.', '. Such models may be rate-limited.'],
    'Офлайн-модель': ['Модели офлайн', 'Offline model'],
    'Провайдер': ['Провайдери', 'The provider'],
    'работает без интернета и ключей. Он не ищет в сети, но чат никогда не остаётся без ответа.': ['бе интернет ва калидҳо кор мекунад. Он дар шабака намеҷӯяд, вале чат ҳеҷ гоҳ бе ҷавоб намемонад.', 'works without internet or keys. It does not search the web, but the chat is never left without an answer.'],
    'Что значит «огромные веса»': ['«Вазнҳои азим» чӣ маъно дорад', 'What “huge weights” means'],
    'Размеры вроде 70B или 671B — это количество параметров модели. Больше не всегда означает лучше: учитывайте контекст и задачу.': ['Андозаҳое чун 70B ё 671B — шумораи параметрҳои модел аст. Бештар на ҳамеша беҳтар аст: контекст ва вазифаро ба назар гиред.', 'Sizes like 70B or 671B are the number of model parameters. Bigger is not always better: consider the context and the task.'],
    'бесплатно': ['ройгон', 'free'],
    '${v0} / 1M токенов': ['${v0} / 1M токен', '${v0} / 1M tokens'],
    'цена по тарифу': ['нарх аз рӯи тариф', 'price per plan'],
    'Не удалось загрузить провайдеров': ['Провайдерҳоро бор кардан нашуд', 'Could not load providers'],
    'Ключ найден · каталог доступен': ['Калид ёфт шуд · феҳрист дастрас аст', 'Key found · catalogue available'],
    'Нет ключа в .env': ['Дар .env калид нест', 'No key in .env'],
    'Провайдеры не найдены': ['Провайдерҳо ёфт нашуданд', 'No providers found'],
    'Проверяю {v0}…': ['{v0}-ро месанҷам…', 'Checking {v0}…'],
    'Проверяю ключ и загружаю каталог моделей…': ['Калидро месанҷам ва феҳристи моделҳоро бор мекунам…', 'Checking the key and loading the model catalogue…'],
    'Проверьте, что llama-server запущен на 127.0.0.1:8080; резервные локальные модели всё равно доступны для выбора.': ['Санҷед, ки llama-server дар 127.0.0.1:8080 кор мекунад; моделҳои маҳаллии эҳтиётӣ ба ҳар ҳол барои интихоб дастрасанд.', 'Check that llama-server is running on 127.0.0.1:8080; the fallback local models are still available.'],
    'Проверьте ключ в .env и перезапустите сервер.': ['Калидро дар .env санҷед ва серверро аз нав оғоз кунед.', 'Check the key in .env and restart the server.'],
    '{v0} · каталог получен': ['{v0} · феҳрист гирифта шуд', '{v0} · catalogue received'],
    'По этим фильтрам модели не найдены': ['Бо ин филтрҳо модел ёфт нашуд', 'No models match these filters'],
    '⌗ Контекст': ['⌗ Контекст', '⌗ Context'],
    '◉ Веса': ['◉ Вазн', '◉ Weights'],
    '◆ Ввод': ['◆ Вуруд', '◆ Input'],
    'Модель доступна через выбранного провайдера.': ['Модел тавассути провайдери интихобшуда дастрас аст.', 'The model is available through the selected provider.'],
    '✓ Используется в чате': ['✓ Дар чат истифода мешавад', '✓ Used in chat'],
    'Выбрать модель': ['Интихоби модел', 'Select model'],
    'Не удалось выбрать модель': ['Моделро интихоб кардан нашуд', 'Could not select the model'],
    'Выбрано: {model}': ['Интихоб шуд: {model}', 'Selected: {model}'],
    'есть бесплатный тариф': ['тарифи ройгон ҳаст', 'has a free tier'],
    'платный': ['пулакӣ', 'paid'],
    'не определена': ['муайян нашуд', 'unknown'],
    'доступен': ['дастрас', 'available'],
    'нужен ключ': ['калид лозим', 'key required'],
    'Проверить доступность': ['Санҷидани дастрасӣ', 'Check availability'],
    'Настроить ключ': ['Танзими калид', 'Set up key'],
    'Проверяю актуальный каталог…': ['Феҳристи ҷориро месанҷам…', 'Checking the current catalogue…'],
    'Проверяю провайдеры и цены…': ['Провайдерҳо ва нархҳоро месанҷам…', 'Checking providers and prices…'],
    'Не удалось получить media-каталог': ['Феҳристи медиаро гирифтан нашуд', 'Could not get the media catalogue'],
    'Цены определены по данным провайдера/официального каталога': ['Нархҳо аз рӯи маълумоти провайдер/феҳристи расмӣ муайян шуданд', 'Prices come from the provider / official catalogue'],
    'Для этого типа моделей ничего не найдено': ['Барои ин навъи модел чизе ёфт нашуд', 'Nothing found for this model type'],
    'Модели не найдены. Подключите OpenRouter или Google AI Studio.': ['Модел ёфт нашуд. OpenRouter ё Google AI Studio-ро пайваст кунед.', 'No models found. Connect OpenRouter or Google AI Studio.'],
    'Проверяю…': ['Месанҷам…', 'Checking…'],
    'цена не определена': ['нарх муайян нашуд', 'price unknown'],
    '⚠ {id}: модель найдена, но API-ключ провайдера не настроен · {price}': ['⚠ {id}: модел ёфт шуд, вале калиди API-и провайдер танзим нашудааст · {price}', '⚠ {id}: model found, but the provider API key is not set · {price}'],
    '✓ {id}: каталог/API доступен · {price}': ['✓ {id}: феҳрист/API дастрас аст · {price}', '✓ {id}: catalogue/API available · {price}'],
    'Модель сейчас недоступна': ['Модел ҳоло дастрас нест', 'The model is unavailable right now'],
    'Ошибка проверки:': ['Хатои санҷиш:', 'Check error:'],
    'Опрашиваю SearXNG, DuckDuckGo, Википедию…': ['SearXNG, DuckDuckGo, Википедияро месанҷам…', 'Querying SearXNG, DuckDuckGo, Wikipedia…'],
    'Поиск работает: доступно {alive} из {length} бэкендов': ['Ҷустуҷӯ кор мекунад: {alive} аз {length} система дастрас', 'Search works: {alive} of {length} backends available'],
    'Ни один поисковый бэкенд не ответил. Проверьте интернет или задайте SEARCH_BACKENDS / SEARXNG_INSTANCES в .env': ['Ягон системаи ҷустуҷӯ ҷавоб надод. Интернетро санҷед ё SEARCH_BACKENDS / SEARXNG_INSTANCES-ро дар .env муайян кунед', 'No search backend answered. Check the internet or set SEARCH_BACKENDS / SEARXNG_INSTANCES in .env'],
    '{count} результатов · {ms} мс': ['{count} натиҷа · {ms} мс', '{count} results · {ms} ms'],
    'нет ответа': ['ҷавоб нест', 'no answer'],
    'Бэкенды не настроены': ['Системаҳо танзим нашудаанд', 'No backends configured'],

    // ── провайдеры (подписи приходят с сервера) ──
    'Локальная демо-модель (офлайн)': ['Модели намоишии маҳаллӣ (офлайн)', 'Local demo model (offline)'],
    'Бесплатный тариф, нужен GROQ_API_KEY': ['Тарифи ройгон, GROQ_API_KEY лозим', 'Free tier, needs GROQ_API_KEY'],
    'Нужен CEREBRAS_API_KEY': ['CEREBRAS_API_KEY лозим', 'Needs CEREBRAS_API_KEY'],
    'Нужен OPENROUTER_API_KEY': ['OPENROUTER_API_KEY лозим', 'Needs OPENROUTER_API_KEY'],
    'Нужен GEMINI_API_KEY': ['GEMINI_API_KEY лозим', 'Needs GEMINI_API_KEY'],
    'Каталог публичный; для генерации нужен POLLINATIONS_API_KEY': ['Феҳрист ошкор аст; барои эҷод POLLINATIONS_API_KEY лозим', 'Public catalogue; generation needs POLLINATIONS_API_KEY'],
    'Локальный llama-server / Ollama, ключ не обязателен': ['llama-server / Ollama-и маҳаллӣ, калид ҳатмӣ нест', 'Local llama-server / Ollama, no key required'],
    'Встроенная офлайн-модель: работает всегда, без сети и ключей': ['Модели офлайни дарунсохт: ҳамеша, бе шабака ва калид кор мекунад', 'Built-in offline model: always works, no network or keys'],

    // ── вход и регистрация ──
    'Khirad — Вход': ['Khirad — Воридшавӣ', 'Khirad — Sign in'],
    'Вход в аккаунт': ['Ворид шудан ба ҳисоб', 'Sign in to your account'],
    'Войдите, чтобы использовать AI-ассистента': ['Барои истифодаи ёрдамчии AI ворид шавед', 'Sign in to use the AI assistant'],
    'Никнейм': ['Тахаллус', 'Nickname'],
    'Введите никнейм': ['Тахаллусро ворид кунед', 'Enter your nickname'],
    'Код доступа': ['Рамзи дастрасӣ', 'Access code'],
    'Введите код': ['Рамзро ворид кунед', 'Enter the code'],
    'Запомнить меня': ['Маро дар ёд дор', 'Remember me'],
    'Войти': ['Ворид шудан', 'Sign in'],
    'или': ['ё', 'or'],
    'Гость': ['Меҳмон', 'Guest'],
    'Нет аккаунта?': ['Ҳисоб надоред?', 'No account?'],
    'Зарегистрироваться': ['Сабти ном', 'Sign up'],
    'Регистрация': ['Сабти ном', 'Sign up'],
    'Создайте аккаунт для доступа к AI': ['Барои дастрасӣ ба AI ҳисоб созед', 'Create an account to access the AI'],
    'Никнейм *': ['Тахаллус *', 'Nickname *'],
    'Ваш никнейм': ['Тахаллуси шумо', 'Your nickname'],
    'Код доступа *': ['Рамзи дастрасӣ *', 'Access code *'],
    'Придумайте код (минимум 3 символа)': ['Рамз созед (ақаллан 3 аломат)', 'Create a code (at least 3 characters)'],
    'Подтвердите код *': ['Рамзро тасдиқ кунед *', 'Confirm the code *'],
    'Повторите код': ['Рамзро такрор кунед', 'Repeat the code'],
    'Я согласен с правилами использования': ['Ман бо қоидаҳои истифода розӣ ҳастам', 'I agree to the terms of use'],
    'Уже есть аккаунт?': ['Аллакай ҳисоб доред?', 'Already have an account?'],
    'Ошибка входа через Google': ['Хатои воридшавӣ тавассути Google', 'Google sign-in error'],
    'Заполните все поля': ['Ҳамаи майдонҳоро пур кунед', 'Fill in all fields'],
    'Код должен быть не менее 3 символов': ['Рамз бояд ақаллан 3 аломат бошад', 'The code must be at least 3 characters'],
    'Коды не совпадают': ['Рамзҳо мувофиқ нестанд', 'The codes do not match'],
    'Примите условия использования': ['Шартҳои истифодаро қабул кунед', 'Accept the terms of use'],

    // ── Composio ──
    'Подключение внешних сервисов (GitHub, Gmail, Notion, Slack и другие) выполняется прямо в чате: введите': ['Пайваст кардани хизматҳои берунӣ (GitHub, Gmail, Notion, Slack ва ғайра) бевосита дар чат анҷом дода мешавад: ворид кунед', 'External services (GitHub, Gmail, Notion, Slack and others) are connected right in the chat: enter'],
    '— NovaMind пришлёт карточку авторизации.': ['— NovaMind корти воридшавиро мефиристад.', '— NovaMind will send an authorisation card.'],
    'Полезные команды:': ['Фармонҳои муфид:', 'Useful commands:'],

    // ── админ-панель ──
    'Админ-панель | AI Assistant': ['Панели админ | AI Assistant', 'Admin panel | AI Assistant'],
    '⚙️ Админ-панель': ['⚙️ Панели админ', '⚙️ Admin panel'],
    '🏠 Главная': ['🏠 Асосӣ', '🏠 Home'],
    '🚪 Выйти': ['🚪 Баромадан', '🚪 Log out'],
    '🔐 Вход в админ-панель': ['🔐 Воридшавӣ ба панели админ', '🔐 Admin sign-in'],
    'Логин': ['Логин', 'Login'],
    'Код': ['Рамз', 'Code'],
    '📊 Статистика': ['📊 Омор', '📊 Statistics'],
    '⚙️ Настройки': ['⚙️ Танзимот', '⚙️ Settings'],
    'Системный промпт': ['Промпти системавӣ', 'System prompt'],
    '💾 Сохранить': ['💾 Нигоҳ доштан', '💾 Save'],
    '💻 Редактор кода': ['💻 Муҳаррири код', '💻 Code editor'],
    'Название файла': ['Номи файл', 'File name'],
    'Код (Python)': ['Код (Python)', 'Code (Python)'],
    '💾 Сохранить и запустить': ['💾 Нигоҳ доштан ва иҷро', '💾 Save and run'],
    '💾 Только сохранить': ['💾 Танҳо нигоҳ доштан', '💾 Save only'],
    '▶ Запустить сохранённый': ['▶ Иҷрои нигоҳдошташуда', '▶ Run saved'],
    'Здесь появится результат выполнения...': ['Натиҷаи иҷро дар ин ҷо пайдо мешавад...', 'The run result will appear here...'],
    '📁 Сохранённые скрипты': ['📁 Скриптҳои нигоҳдошташуда', '📁 Saved scripts'],
    'Ошибка сети': ['Хатои шабака', 'Network error'],
    'Ошибка сети:': ['Хатои шабака:', 'Network error:'],
    'Сообщений в истории': ['Паёмҳо дар таърих', 'Messages in history'],
    'Плагинов': ['Плагинҳо', 'Plugins'],
    'Своих команд': ['Фармонҳои худӣ', 'Custom commands'],
    'Да': ['Ҳа', 'Yes'],
    'Нет': ['Не', 'No'],
    'Озвучка': ['Садодиҳӣ', 'Voice'],
    'Ошибка загрузки дашборда': ['Хатои боркунии панел', 'Dashboard loading error'],
    'Настройки сохранены!': ['Танзимот нигоҳ дошта шуд!', 'Settings saved!'],
    'Ошибка сохранения': ['Хатои нигоҳдорӣ', 'Save error'],
    'Код сохранён:': ['Код нигоҳ дошта шуд:', 'Code saved:'],
    'Запуск...': ['Иҷро...', 'Running...'],
    'Нет вывода': ['Натиҷа нест', 'No output'],
    'Нет сохранённых файлов': ['Файлҳои нигоҳдошташуда нестанд', 'No saved files'],
    'Ошибка загрузки': ['Хатои боркунӣ', 'Loading error'],
    'Файл загружен:': ['Файл бор шуд:', 'File loaded:'],
  };

  // ── текущий язык ──
  function readCookie() {
    const match = document.cookie.match(/(?:^|;\s*)nova_lang=([a-z]{2})/);
    return match ? match[1] : '';
  }
  function valid(code) { return LANGS.some((l) => l.code === code); }
  function detect() {
    let saved = '';
    try { saved = localStorage.getItem(KEY) || ''; } catch (_) { /* приватный режим */ }
    if (valid(saved)) return saved;
    const cookie = readCookie();
    return valid(cookie) ? cookie : DEFAULT_LANG;
  }
  let lang = detect();
  // cookie нужен серверу: на нём он выбирает язык ответов ИИ и статусов
  function persist(code) {
    try { localStorage.setItem(KEY, code); } catch (_) { /* ignore */ }
    document.cookie = `${KEY}=${code}; path=/; max-age=31536000; SameSite=Lax`;
  }
  persist(lang);
  document.documentElement.lang = lang;

  // ── перевод ──
  function lookup(core) {
    if (lang === 'ru') return null;
    const row = DICT[core];
    if (!row) return null;
    const value = row[lang === 'tg' ? 0 : 1];
    return Array.isArray(value) ? value[1] : value;
  }
  function format(text, vars) {
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, (all, name) => (name in vars && vars[name] != null ? String(vars[name]) : all));
  }
  /** Перевод строки. Пробелы по краям сохраняются: t('Ошибка: ') + e */
  function t(key, vars) {
    const raw = String(key == null ? '' : key);
    const match = raw.match(/^(\s*)([\s\S]*?)(\s*)$/);
    const found = lookup(match[2]);
    return match[1] + format(found == null ? match[2] : found, vars) + match[3];
  }
  /** Слово при числе: tn(5, 'шаг', 'шага', 'шагов') */
  function tn(n, one, few, many) {
    const count = Math.abs(Number(n) || 0);
    if (lang === 'ru') {
      const n10 = count % 10, n100 = count % 100;
      if (n10 === 1 && n100 !== 11) return one;
      if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
      return many;
    }
    const row = DICT[many] || DICT[one];
    if (!row) return many;
    const value = row[lang === 'tg' ? 0 : 1];
    if (Array.isArray(value)) return count === 1 ? value[0] : value[1];
    return value;
  }

  // ── статичный текст страницы ──
  const SKIP = '#chatContainer .message, #chatContainer .msg, #historyContainer, [data-no-i18n], script, style, code, pre';
  const ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];
  const originalText = new WeakMap();
  const originalAttr = new WeakMap();

  function translateTextNode(node) {
    // текст внутри поля ввода — это данные пользователя (у поля переводим только атрибуты)
    if (node.parentElement && node.parentElement.closest('textarea, [contenteditable="true"]')) return;
    const base = originalText.has(node) ? originalText.get(node) : node.nodeValue;
    const core = base.trim();
    if (!core || !/[А-Яа-яЁё]/.test(core)) return;
    if (!originalText.has(node)) originalText.set(node, base);
    const next = t(base);
    if (node.nodeValue !== next) node.nodeValue = next;
  }
  function translateAttrs(el) {
    let saved = originalAttr.get(el);
    for (const name of ATTRS) {
      const current = el.getAttribute(name);
      if (current == null) continue;
      const base = saved && name in saved ? saved[name] : current;
      if (!/[А-Яа-яЁё]/.test(base)) continue;
      if (!saved) { saved = {}; originalAttr.set(el, saved); }
      if (!(name in saved)) saved[name] = base;
      const next = t(base);
      if (current !== next) el.setAttribute(name, next);
    }
  }
  /** Перевести статичный текст под root (по умолчанию — вся страница). */
  function apply(root) {
    const scope = root || document.body;
    if (!scope) return;
    if (!root && document.title) {
      if (!originalText.has(document)) originalText.set(document, document.title);
      document.title = t(originalText.get(document));
    }
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode(node) {
        const el = node.nodeType === 1 ? node : node.parentElement;
        if (el && el.closest(SKIP)) return node.nodeType === 1 ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    if (scope.nodeType === 1 && !scope.closest(SKIP)) translateAttrs(scope);
    let node = walker.nextNode();
    while (node) {
      if (node.nodeType === 3) translateTextNode(node);
      else translateAttrs(node);
      node = walker.nextNode();
    }
  }

  /** Сменить язык. Страница перезагружается, чтобы всё — включая историю,
   *  палитру команд и ответы сервера — перестроилось на новом языке. */
  function setLang(code, options) {
    if (!valid(code)) return;
    const changed = code !== lang;
    lang = code;
    persist(code);
    document.documentElement.lang = code;
    window.dispatchEvent(new CustomEvent('nova:lang', { detail: { lang: code } }));
    if (changed && !(options && options.reload === false)) window.location.reload();
  }

  /** Переключатель языка: кнопки TJ / RU / EN (compact) или полные названия. */
  function picker(container, options) {
    if (!container) return;
    const compact = !!(options && options.compact);
    container.classList.add('lang-picker');
    if (compact) container.classList.add('lang-picker-compact');
    container.setAttribute('role', 'radiogroup');
    container.setAttribute('aria-label', t('Язык интерфейса'));
    container.setAttribute('data-no-i18n', '');
    container.innerHTML = '';
    for (const item of LANGS) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'lang-option' + (item.code === lang ? ' active' : '');
      button.dataset.lang = item.code;
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-checked', item.code === lang ? 'true' : 'false');
      button.textContent = compact ? item.short : item.name;
      button.title = item.name;
      button.addEventListener('click', () => setLang(item.code));
      container.appendChild(button);
    }
  }

  /** Язык для распознавания речи и озвучки. */
  function speechLang() {
    return { tg: 'tg-TJ', ru: 'ru-RU', en: 'en-US' }[lang];
  }
  /** Локаль для дат: если браузер не знает таджикский — русский формат. */
  function locale() {
    if (lang === 'en') return 'en-GB';
    if (lang === 'tg') {
      try { if (Intl.DateTimeFormat.supportedLocalesOf(['tg']).length) return 'tg-TJ'; } catch (_) { /* старый браузер */ }
    }
    return 'ru-RU';
  }

  const api = {
    LANGS, DEFAULT_LANG, DICT,
    get lang() { return lang; },
    t, tn, apply, setLang, picker, speechLang, locale,
  };
  window.I18N = api;
  window.t = t;
  window.tn = tn;

  function boot() {
    try {
      apply();
      document.querySelectorAll('[data-lang-picker]').forEach((el) => picker(el, { compact: el.dataset.langPicker === 'compact' }));
    } finally {
      document.documentElement.classList.remove('i18n-pending');
    }
  }
  // Пока текст не переведён, страница скрыта — без «мигания» русского текста
  if (lang !== 'ru') {
    document.documentElement.classList.add('i18n-pending');
    setTimeout(() => document.documentElement.classList.remove('i18n-pending'), 1500);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
