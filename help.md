Службы (systemd)

# Управление ботом
sudo systemctl status tgbot
sudo systemctl restart tgbot
sudo systemctl stop tgbot
sudo systemctl start tgbot

# Управление Ollama
sudo systemctl status ollama
sudo systemctl restart ollama

# Автозапуск
sudo systemctl enable tgbot
sudo systemctl enable ollama

# Логи в реальном времени
sudo journalctl -u tgbot -f
sudo journalctl -u ollama -f

# Последние 100 строк
sudo journalctl -u tgbot -n 100

# С момента времени
sudo journalctl -u tgbot --since "10 minutes ago"
--------------------------------------------------

Ollama
# Список скачанных моделей
ollama list

# Что сейчас в памяти
ollama ps

# Скачать модель
ollama pull hf.co/MilyaShams/T-lite-it-1.0-Q4_K_M-GGUF:Q4_K_M

# Прогреть модель вручную (без бота)
curl -s http://127.0.0.1:11434/api/chat -d '{
  "model":"hf.co/MilyaShams/T-lite-it-1.0-Q4_K_M-GGUF:Q4_K_M",
  "messages":[{"role":"user","content":"привет"}],
  "stream":false,
  "options":{"num_ctx":2048,"num_predict":8}
}' | python3 -m json.tool

# Информация о модели
ollama show <model>

# Выгрузить модель из памяти
ollama stop <model>

# Изменить параметры Ollama (override systemd)
sudo systemctl edit ollama.service
# там: Environment="OLLAMA_NUM_THREADS=3" и т.д.
sudo systemctl daemon-reload
sudo systemctl restart ollama
-------------------------------------------------------

Тесты

cd ~/TgBot/plants_taganrog_bot
source venv/bin/activate

# Все тесты
pytest -v

# Один файл
pytest tests/test_db.py -v

# Один класс
pytest tests/test_db.py::TestKarma -v

# Один тест
pytest tests/test_db.py::TestKarma::test_positive -v

# С полным трейсбеком
pytest -v --tb=long

# Остановиться на первой ошибке
pytest -x

# Показать print() из тестов
pytest -v -s

# Проверка синтаксиса без запуска
python -m py_compile app.py db.py config.py threads.py llm.py utils.py
----------------------------------------------------

Python окружение

cd ~/TgBot/plants_taganrog_bot
source venv/bin/activate        # войти в venv
deactivate                       # выйти
pip list                         # что установлено
pip install <pkg>                # поставить пакет
pip freeze > requirements.txt    # зафиксировать версии
------------------------------------------------------

Файлы и логи

cd ~/TgBot/plants_taganrog_bot
tail -30 logs/system.log                          # системные события
tail -30 logs/private/<ваш_id>_*.log              # ваша личка с ботом
tail -30 logs/group/<chat_id>_*.log               # групповой чат
ls logs/                                          # что вообще есть

# Смотреть системный лог в реальном времени
tail -f logs/system.log
-------------------------------------------------------

Git

cd ~/TgBot/plants_taganrog_bot

# Что изменилось
git status
git diff

# Последние коммиты
git log --oneline -10
git log --oneline --all --graph

# Список тегов
git tag -l

# Коммит + тег
git add -A
git commit -m "v2.6.5: что-то сделал"
git tag -a v2.6.5 -m "Короткое описание"

# Push (если работает)
git push
git push --tags

# Откат одного файла к тегу
git checkout v2.6.4 -- app.py

# Полный откат к тегу (осторожно)
git checkout v2.6.4

# Вернуться к main
git checkout main
------------------------------------------

База данных

cd ~/TgBot/plants_taganrog_bot

# Прямой доступ к SQLite
sqlite3 karma.db

# внутри: .tables .schema users .quit

# Проверить размер
du -h karma.db

# Бэкап (безопасно при работающем боте)
sqlite3 karma.db ".backup karma-backup.db"

# Очистить всё
sudo systemctl stop tgbot
rm karma.db   # пересоздастся при старте
sudo systemctl start tgbot
--------------------------------------------

Мониторинг

# Память
free -h
watch -n 5 free -h

# Топ процессов по RAM
ps aux --sort=-%mem | head -n 8

# Загрузка CPU
htop   # если установлен; иначе top

# Что ест shared memory
sudo lsof /dev/shm 2>/dev/null | head
