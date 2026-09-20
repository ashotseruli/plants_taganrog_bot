🎯 Что это
Telegram-бот для чата про комнатные растения (г. Таганрог). Два больших блока функциональности:

Карма-система. Считает реакции 👍/👎 на сообщения, показывает личный рейтинг, топ за месяц и за всё время. Команды: /karma, /top, karmaclean, /karmapurge.

Верификация новичков. При входе нового участника отправляет кнопку «Я человек», если не нажал за 60 сек — банит и разбанивает (мягкое удаление).

LLM-реплики. Отвечает в личке и в группах (по упоминанию/reply), используя локальную модель через Ollama.

🖥️ Железо и окружение
Сервер: ZX05 (Intel N100, 4 ядра, 10 ГБ доступной RAM, Ubuntu 26.04).

Модель: hf.co/MilyaShams/T-lite-it-1.0-Q4_K_M-GGUF:Q4_K_M (T-lite 7.6B на базе Qwen 2.5, Q4_K_M, ~4.9 ГБ RAM, скорость ~3.7 ток/с на CPU).

Ollama: установлена через официальный скрипт, override: OLLAMA_NUM_THREADS=3, OLLAMA_KEEP_ALIVE=-1, OLLAMA_NUM_PARALLEL=1, OLLAMA_MAX_LOADED_MODELS=1.

Python: 3.14, venv в папке проекта.

Telegram API: через прокси Cloudflare Worker (TG_BASE_URL в .env), из-за блокировок.

📁 Структура проекта
text
~/TgBot/plants_taganrog_bot/
├── app.py           ← точка входа: хендлеры Telegram + main()
├── config.py        ← load_config + validate_config для YAML
├── db.py            ← SQLite-слой (users/messages/reactions, карма)
├── threads.py       ← thread-based история диалогов + дедуп апдейтов
├── llm.py           ← HTTP-транспорт к Ollama (httpx, общий клиент)
├── utils.py         ← чистые утилиты без побочек
├── config.yaml      ← настройки: промпты, фразы, placeholders, параметры LLM
├── .env             ← секреты: BOT_TOKEN, OWNER_ID, OLLAMA_*, TG_BASE_URL
├── karma.db         ← SQLite база
├── owner_mode.flag  ← НЕ используется (удалён в 2.5.6)
├── logs/
│   ├── system.log       ← старт, ошибки, реакции, метрики
│   ├── private/         ← логи по каждому пользователю в личке
│   └── group/           ← логи по каждой группе
├── tests/
│   ├── conftest.py      ← env + временный config.yaml + фикстура temp_db
│   ├── test_utils.py    ← 30 тестов чистых функций
│   ├── test_db.py       ← 25 тестов БД
│   └── test_config.py   ← 20 тестов валидации
├── pytest.ini
├── requirements.txt
└── venv/
🔌 Как устроено взаимодействие
Поток обработки сообщения в личке:

Telegram → прокси Cloudflare → getUpdates в app.py.

save_message_handler (group=-1) сохраняет сообщение в БД.

talk_response → _handle_llm_turn:

проверка на дубликат (threads.is_duplicate с учётом edit_date);

rate-limit по (chat_id, user_id) через user_cooldown;

определение thread (threads.resolve_thread_key);

логирование в logs/private/<id>_<username>.log;

per-thread asyncio.Lock + _spawn(_do_turn()) — обработка идёт в фоне.

send_smart_reply:

запускает _call_ollama_async через _spawn;

через 5 сек показывает placeholder из THINKING_PLACEHOLDERS, потом меняет его каждые 5 сек;

ждёт до LLM_HARD_TIMEOUT;

валидирует ответ (≤ max_words), иначе fallback на random.choice(PHRASES);

пишет user+assistant в историю thread (только если ответ от LLM, не fallback);

редактирует placeholder → финальный ответ.

_call_ollama_async собирает messages (system + trimmed history + user), зовёт llm.post_chat.

llm.post_chat → httpx.AsyncClient → Ollama /api/chat → message.content.

В группе: те же шаги, но talk_response срабатывает только если есть @bot_username или reply на сообщение бота.

⚙️ Ключевые решения и почему
Thread-based история, а не per-chat. В группе ответ бота в thread Васи не должен путаться с диалогом Маши. Thread определяется по reply_to_message, маппинг message_id → thread_id живёт в threads._msg_to_thread (LRU на 5000).

asyncio.Lock per thread. Сообщения одного thread обрабатываются последовательно, чтобы пары user/assistant не путались в истории при concurrent_updates(64).

concurrent_updates(64). Пока один пользователь ждёт LLM, остальные апдейты (реакции, входы) обрабатываются сразу.

Fallback не пишется в историю. Иначе LLM-фразы из PHRASES загрязняют контекст.

Дедуп с учётом edit_date. Обычное сообщение и его правка — разные ключи, обе обрабатываются.

drop_pending_updates=True. После рестарта не отвечаем на «пропущенные» сообщения.

Один httpx-клиент. Переиспользует TCP-соединения к Ollama.

num_ctx=2048, num_predict=60. Баланс для N100: влезает в 10 ГБ RAM, ответ ~15–20 сек.

.env загружается до импорта модулей. Иначе llm.OLLAMA_MODEL берёт дефолт mistral:7b вместо T-lite.

🔑 Что знать о конфиге
config.yaml — можно править и применять через /reload (команда только для владельца). Не требует рестарта службы.

Ключи:

llm.thinking_delay — сек через плейсхолдер.

llm.hard_timeout — общий лимит на запрос к Ollama.

llm.max_words — валидация длины ответа.

llm.temperature, top_p, repeat_penalty, num_ctx, num_predict — параметры генерации.

llm.history_max_messages, history_max_chars — размер thread-истории.

llm.user_cooldown — сек между запросами от одного пользователя.

prompts.system — основной системный промпт.

phrases — fallback-фразы (при таймауте LLM).

placeholders — «хм…», «пупупу…» во время ожидания.

reactions.positive / negative — какие эмодзи считать.

new_member.kick_after_seconds — сколько ждать нажатия кнопки.

.env — требует systemctl restart tgbot:

text
BOT_TOKEN=...
OWNER_ID=...
OLLAMA_URL=http://127.0.0.1:11434/api/chat
OLLAMA_MODEL=hf.co/MilyaShams/T-lite-it-1.0-Q4_K_M-GGUF:Q4_K_M
TG_BASE_URL=https://tg-api-proxy.ashot-seruli.workers.dev/bot
TG_BASE_FILE_URL=https://tg-api-proxy.ashot-seruli.workers.dev/file/bot
🛠️ Скрытые команды
/reload — только для OWNER_ID. Перечитывает config.yaml, сбрасывает истории и HTTP-клиент, обновляет глобалы. Не трогает .env и модель.

/reset — только для OWNER_ID. Очищает thread, в котором вызвана.

📜 История версий
2.5.7 — bot.log → logs/system.log, LOG_DIR.mkdir до basicConfig.

2.5.8 — вынос utils.py, тесты на чистые функции.

2.5.10 — тесты на config.validate_config (тогда ещё в app.py).

2.6.0 — вынос db.py.

2.6.1 — вынос config.py.

2.6.2 — вынос threads.py. Удалены /talk и /owner (в 2.5.6, но здесь структурно).

2.6.3 — вынос llm.py.

2.6.4 — .env до импортов. Фикс модели mistral:7b вместо T-lite.

🎯 Что НЕ доделано (бэклог)
Промпт. Кактусы доминируют. «Цвету по выходным» — не нравится. Водка слишком часто. Пользователь не дал калибровку «нравится/не нравится», поэтому отложено.

Эмодзи. T-lite иногда ставит 🌵😏 — не критично, можно отфильтровать пост-обработкой.

Спам-фильтр через LLM. Изначальная идея, не реализована.

Бэкап karma.db. Отложен — «не важно».

Healthcheck. Обсуждался, не сделан.

Тесты на threads.py. Отложено (глобальное состояние).

app.py → handlers.py. Обсуждалось, отложено как косметика.

🚦 Если продолжаешь с нуля
Первые шаги:

cd ~/TgBot/plants_taganrog_bot && source venv/bin/activate

pytest -v — убедиться, что 80 тестов зелёные.

sudo systemctl status tgbot — работает ли.

ollama ps — загружена ли T-lite.

tail -20 logs/system.log — что было последним.

Если что-то не так:

Бот молчит → journalctl -u tgbot -n 50 + tail logs/system.log.

Модель не отвечает → ollama ps, ollama list, curl тест из шпаргалки.

Тесты падают → pytest -v --tb=long, скорее всего проблема в env (conftest.py).

Не пушится в git → GitHub блокируется, см. раздел про SSH через порт 443.

Перед любой правкой:

Ветка/тег сейчас какие? git log --oneline -3 && git tag -l | tail -3.

Что меняется в .env → рестарт. Что в config.yaml → /reload в Telegram.

После правки — py_compile + pytest -v + рестарт + 1–2 сообщения боту.

📞 Точки контакта
Владелец: OWNER_ID=263398704 (в .env), скрытые команды только для него.

Прокси Telegram: tg-api-proxy.ashot-seruli.workers.dev (Cloudflare Worker, владелец настроил для обхода блокировок).

Репозиторий: github.com/ashotseruli/plants_taganrog_bot (push может висеть — проблема с сетью из РФ).
