import socket


def _force_ipv4():
    """Патч socket.getaddrinfo так, чтобы всегда предпочитать IPv4.
    Вызывается явно из main() до старта бота."""
    original_getaddrinfo = socket.getaddrinfo

    def _ipv4_only_getaddrinfo(*args, **kwargs):
        responses = original_getaddrinfo(*args, **kwargs)
        ipv4_responses = [r for r in responses if r[0] == socket.AF_INET]
        return ipv4_responses or responses

    socket.getaddrinfo = _ipv4_only_getaddrinfo


import os
import html
import logging
import random
import sqlite3
import time
import asyncio
import functools
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
    CallbackQueryHandler, MessageReactionHandler
)
import httpx
import yaml

from utils import (
    sanitize_filename,
    is_mentioned,
    placeholders,
    safe_format,
    msg_edit_ts,
    trim_history,
)

# --- Пути (не зависим от CWD) ---
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "karma.db")
LOG_DIR = BASE_DIR / "logs"
PRIVATE_LOG_DIR = LOG_DIR / "private"
GROUP_LOG_DIR = LOG_DIR / "group"
BOT_LOG_FILE = LOG_DIR / "system.log"

# Создаём папку логов заранее — иначе RotatingFileHandler ниже упадёт
# на системах, где logs/ ещё нет.
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --- Логирование (московское время) ---
MOSCOW_TZ = timezone(timedelta(hours=3))


class MoscowTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=MOSCOW_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        RotatingFileHandler(
            str(BOT_LOG_FILE),
            maxBytes=10_000_000,
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
    force=True,
)
_formatter = MoscowTimeFormatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
for _h in logging.getLogger().handlers:
    _h.setFormatter(_formatter)

logger = logging.getLogger(__name__)

# --- Фоновые задачи (держим ссылки, чтобы GC не съел) ---
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Запускает корутину как фоновую задачу с сохранением ссылки и логированием
    необработанных исключений (иначе asyncio тихо напишет
    'Task exception was never retrieved')."""
    t = asyncio.create_task(coro)

    def _on_done(task: asyncio.Task):
        _background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Фоновая задача упала: %r", exc, exc_info=exc)

    _background_tasks.add(t)
    t.add_done_callback(_on_done)
    return t


# --- Хранилище ---
pending_jobs = {}
WAITING_FOR_NAMES = 1
BOT_VERSION = "2.5.8"

# --- Rate-limit на LLM: (chat_id, user_id) -> monotonic ts последнего запроса ---
_llm_last_call: dict[tuple[int, int], float] = {}

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("Токен не найден!")

CONFIG_PATH = os.getenv("CONFIG_PATH", str(BASE_DIR / "config.yaml"))


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _validate_config(cfg: dict) -> None:
    """Проверяет, что в конфиге есть все критичные ключи и они валидны.

    Проверяются и наличие, и (там где важно) типы/значения — иначе ошибку в yaml
    придётся искать уже во время работы бота."""
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml: корень должен быть словарём")
    for section in ("phrases", "placeholders", "prompts", "reactions", "llm", "new_member"):
        if section not in cfg:
            raise ValueError(f"config.yaml: отсутствует секция '{section}'")

    if not isinstance(cfg["phrases"], list) or not cfg["phrases"]:
        raise ValueError("config.yaml: 'phrases' должен быть непустым списком")
    if not all(isinstance(p, str) for p in cfg["phrases"]):
        raise ValueError("config.yaml: все элементы 'phrases' должны быть строками")

    if not isinstance(cfg["placeholders"], list) or not cfg["placeholders"]:
        raise ValueError("config.yaml: 'placeholders' должен быть непустым списком")
    if not all(isinstance(p, str) for p in cfg["placeholders"]):
        raise ValueError("config.yaml: все элементы 'placeholders' должны быть строками")

    r = cfg["reactions"]
    if not isinstance(r, dict):
        raise ValueError("config.yaml: 'reactions' должен быть словарём")
    pos = r.get("positive") or []
    neg = r.get("negative") or []
    if not isinstance(pos, list) or not isinstance(neg, list):
        raise ValueError("config.yaml: 'reactions.positive'/'negative' должны быть списками")
    if not pos and not neg:
        raise ValueError("config.yaml: нужна хотя бы одна реакция (positive/negative)")

    if not isinstance(cfg["prompts"], dict):
        raise ValueError("config.yaml: 'prompts' должен быть словарём")
    if not isinstance(cfg["prompts"].get("system"), str) or not cfg["prompts"]["system"].strip():
        raise ValueError("config.yaml: 'prompts.system' отсутствует, не строка или пуст")

    llm = cfg["llm"]
    if not isinstance(llm, dict):
        raise ValueError("config.yaml: 'llm' должен быть словарём")
    num_keys = (
        "thinking_delay", "hard_timeout", "max_words", "max_input_chars",
        "history_max_messages", "history_max_chars",
        "num_ctx", "num_predict", "temperature", "top_p", "repeat_penalty",
    )
    for key in num_keys:
        if key not in llm:
            raise ValueError(f"config.yaml: 'llm.{key}' отсутствует")
        if not isinstance(llm[key], (int, float)):
            raise ValueError(f"config.yaml: 'llm.{key}' должен быть числом")
    if llm.get("user_cooldown") is not None and not isinstance(llm["user_cooldown"], (int, float)):
        raise ValueError("config.yaml: 'llm.user_cooldown' должен быть числом")

    nm = cfg["new_member"]
    if not isinstance(nm, dict):
        raise ValueError("config.yaml: 'new_member' должен быть словарём")
    for key in ("kick_after_seconds", "max_message_age_seconds"):
        if key not in nm:
            raise ValueError(f"config.yaml: 'new_member.{key}' отсутствует")
        if not isinstance(nm[key], (int, float)) or nm[key] < 0:
            raise ValueError(f"config.yaml: 'new_member.{key}' должен быть числом ≥ 0")
    if nm["kick_after_seconds"] <= 0:
        raise ValueError("config.yaml: 'new_member.kick_after_seconds' должен быть > 0")


try:
    CFG = load_config()
    _validate_config(CFG)
    logger.info(
        "config.yaml загружен: %d фраз, %d затычек, %d символов в промпте",
        len(CFG['phrases']), len(CFG['placeholders']), len(CFG['prompts']['system']),
    )
except Exception as e:
    logger.critical(f"Не удалось загрузить config.yaml: {e}")
    raise SystemExit(1)

OWNER_ID_RAW = os.getenv('OWNER_ID', '').strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None

# --- Константы ---
MAX_MESSAGE_AGE_SECONDS = CFG["new_member"]["max_message_age_seconds"]

# Кулдаун на пользователя: из ENV, иначе из конфига, иначе 2.0.
LLM_USER_COOLDOWN = float(
    os.getenv("LLM_USER_COOLDOWN", CFG["llm"].get("user_cooldown", 2.0))
)


def _kick_human() -> str:
    """Человекочитаемая длительность таймаута подтверждения, из актуального CFG."""
    sec = CFG["new_member"]["kick_after_seconds"]
    return f"{sec // 60} мин" if sec >= 60 else f"{sec} сек"


# --- Дедупликация апдейтов (LRU через OrderedDict) ---
# Ключ: (chat_id, message_id, edit_date_ts). Для обычных сообщений edit_date=0.
# Редактирование меняет edit_date и, соответственно, ключ — edit с новым
# упоминанием бота проходит дедуп, а повторный апдейт того же edit'а — нет.
_seen_messages = OrderedDict()
_SEEN_LIMIT = 5000


def _is_duplicate(chat_id: int, message_id: int, edit_date_ts: int = 0) -> bool:
    key = (chat_id, message_id, edit_date_ts)
    if key in _seen_messages:
        _seen_messages.move_to_end(key)
        return True
    _seen_messages[key] = None
    if len(_seen_messages) > _SEEN_LIMIT:
        _seen_messages.popitem(last=False)
    return False


# --- История диалогов (thread-based, LRU) ---
_chat_histories = OrderedDict()     # (chat_id, thread_id) -> list[{"role","content"}]
_CHAT_HISTORIES_LIMIT = 500
_msg_to_thread = OrderedDict()      # (chat_id, message_id) -> thread_id
_MSG_TO_THREAD_LIMIT = 5000

# Локи per-thread: сериализуют LLM-ответы внутри одного треда, чтобы порядок
# сообщений/ответов в истории не путался при concurrent_updates.
# Это осознанный трейд-офф: сообщения одного пользователя в одном треде
# обрабатываются строго последовательно (второе сообщение подождёт первого),
# зато user/assistant-пары в истории всегда лежат в правильном порядке.
_history_locks: "OrderedDict[tuple[int, int], asyncio.Lock]" = OrderedDict()
_HISTORY_LOCKS_LIMIT = 2000


def _get_thread_lock(chat_id: int, thread_id: int) -> asyncio.Lock:
    """Возвращает (создаёт при необходимости) asyncio.Lock для пары (chat, thread).
    При переполнении вытесняются только свободные локи — захваченные не трогаем,
    чтобы не отобрать лок у работающей корутины. В редком случае, когда все
    локи заняты, словарь может временно превысить лимит — это допустимо."""
    key = (chat_id, thread_id)
    lock = _history_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _history_locks[key] = lock
        if len(_history_locks) > _HISTORY_LOCKS_LIMIT:
            evicted = False
            for k in list(_history_locks.keys()):
                if k == key:
                    continue
                if not _history_locks[k].locked():
                    _history_locks.pop(k, None)
                    evicted = True
                    break
            if not evicted:
                # Все локи заняты — вытеснять некого. Словарь временно превысит
                # лимит; логируем, чтобы заметить аномалию (например, залипший лок).
                logger.warning(
                    "_get_thread_lock: все %d локов заняты, словарь растёт "
                    "(chat=%s thread=%s)",
                    _HISTORY_LOCKS_LIMIT, chat_id, thread_id,
                )
    else:
        _history_locks.move_to_end(key)
    return lock


def _drop_thread_mappings(chat_id: int, thread_id: int) -> None:
    """Убирает все записи message→thread для указанного треда."""
    stale = [
        k for k, v in _msg_to_thread.items()
        if k[0] == chat_id and v == thread_id
    ]
    for k in stale:
        _msg_to_thread.pop(k, None)


def _resolve_thread_key(chat, msg):
    if chat.type == "private":
        return chat.id
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None:
        parent_thread = _msg_to_thread.get((chat.id, reply.message_id))
        if parent_thread is not None:
            return parent_thread
    return msg.message_id


def _remember_message(chat_id, message_id, thread_id):
    key = (chat_id, message_id)
    _msg_to_thread[key] = thread_id
    _msg_to_thread.move_to_end(key)
    if len(_msg_to_thread) > _MSG_TO_THREAD_LIMIT:
        _msg_to_thread.popitem(last=False)


def _get_history(chat_id, thread_id):
    key = (chat_id, thread_id)
    if key not in _chat_histories:
        _chat_histories[key] = []
        if len(_chat_histories) > _CHAT_HISTORIES_LIMIT:
            old_chat, old_thread = _chat_histories.popitem(last=False)[0]
            _drop_thread_mappings(old_chat, old_thread)
    else:
        _chat_histories.move_to_end(key)
    return _chat_histories[key]


def _clear_thread(chat_id, thread_id):
    """Убирает историю, связанные записи в маппинге и lock для треда."""
    _chat_histories.pop((chat_id, thread_id), None)
    _drop_thread_mappings(chat_id, thread_id)
    # Lock снимаем, только если он свободен.
    lock = _history_locks.get((chat_id, thread_id))
    if lock is not None and not lock.locked():
        _history_locks.pop((chat_id, thread_id), None)


POSITIVE_REACTIONS = set(CFG["reactions"]["positive"])
NEGATIVE_REACTIONS = set(CFG["reactions"]["negative"])

PHRASES = CFG["phrases"]

# --- LLM ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:7b")
logger.info("LLM: model=%s, url=%s", OLLAMA_MODEL, OLLAMA_URL)

LLM_THINKING_DELAY = CFG["llm"]["thinking_delay"]
LLM_HARD_TIMEOUT = CFG["llm"]["hard_timeout"]
LLM_MAX_WORDS = CFG["llm"]["max_words"]
LLM_MAX_INPUT_CHARS = CFG["llm"]["max_input_chars"]

THINKING_PLACEHOLDERS = CFG["placeholders"]

LLM_PROMPT_STATIC = safe_format(CFG["prompts"]["system"], max_words=LLM_MAX_WORDS)

# --- Общий httpx-клиент (переиспользуем соединения) ---
_http_client: "httpx.AsyncClient | None" = None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0, read=LLM_HARD_TIMEOUT, write=10.0, pool=5.0
            )
        )
    return _http_client


async def _close_http_client():
    global _http_client
    if _http_client is not None:
        try:
            await _http_client.aclose()
        except Exception as e:
            logger.warning(f"Не удалось закрыть httpx-клиент: {e}")
        _http_client = None


# --- Раздельные логи по чатам (LRU) ---
PRIVATE_LOG_DIR.mkdir(parents=True, exist_ok=True)
GROUP_LOG_DIR.mkdir(parents=True, exist_ok=True)

_chat_loggers = OrderedDict()
_CHAT_LOGGERS_LIMIT = 200


def get_chat_logger(chat_id: int, chat_type: str,
                    user=None, chat_title: str = None) -> logging.Logger:
    key = (chat_id, chat_type)
    cached = _chat_loggers.get(key)
    if cached is not None:
        _chat_loggers.move_to_end(key)
        return cached

    if chat_type == "private":
        # В привате chat_id == user.id, поэтому ключуемся по chat_id —
        # так имя файла не зависит от того, передали ли user.
        if user is not None:
            uname = sanitize_filename(user.username or user.first_name or "unknown")
        else:
            uname = "unknown"
        fname = f"{chat_id}_{uname}.log"
        name = f"chat.private.{chat_id}"
        path = os.path.join(str(PRIVATE_LOG_DIR), fname)
    else:
        title = sanitize_filename(chat_title or "group")
        fname = f"{chat_id}_{title}.log"
        path = os.path.join(str(GROUP_LOG_DIR), fname)
        name = f"chat.group.{chat_id}"

    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        fh = RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(MoscowTimeFormatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        lg.addHandler(fh)

    _chat_loggers[key] = lg
    _chat_loggers.move_to_end(key)

    while len(_chat_loggers) > _CHAT_LOGGERS_LIMIT:
        _old_key, old_lg = _chat_loggers.popitem(last=False)
        for h in list(old_lg.handlers):
            try:
                old_lg.removeHandler(h)
                h.close()
            except Exception:
                pass

    return lg


def get_user_mention(user):
    return f"@{user.username}" if user.username else (user.first_name or "Товарищ")


async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return False
    if chat.type == "private":
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


# =========================================================
#                 БАЗА ДАННЫХ / КАРМА (sync)
# =========================================================

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # journal_mode=WAL — persistent-настройка самого файла БД, ставится один раз
    # в init_db(). Здесь только per-connection параметры.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        c = conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER,
                message_id INTEGER,
                user_id INTEGER,
                date INTEGER,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS reactions (
                chat_id INTEGER,
                message_id INTEGER,
                reactor_id INTEGER,
                reaction TEXT,
                date INTEGER,
                PRIMARY KEY (chat_id, message_id, reactor_id, reaction)
            );
            CREATE INDEX IF NOT EXISTS idx_reactions_date ON reactions(date);
            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
            CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);
            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_reactions_msg ON reactions(chat_id, message_id);
        """)
        conn.commit()
    finally:
        conn.close()


def db_upsert_user(user_id: int, username: str, first_name: str):
    conn = _connect()
    try:
        if username:
            conn.execute(
                "UPDATE users SET username = NULL WHERE username = ? COLLATE NOCASE AND user_id != ?",
                (username, user_id)
            )
        conn.execute(
            """INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username = excluded.username,
                 first_name = excluded.first_name""",
            (user_id, username, first_name)
        )
        conn.commit()
    finally:
        conn.close()


def db_save_message(chat_id: int, message_id: int, user_id: int, date_ts: int):
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO messages (chat_id, message_id, user_id, date)
               VALUES (?, ?, ?, ?)""",
            (chat_id, message_id, user_id, date_ts)
        )
        conn.commit()
    finally:
        conn.close()


def db_get_message_author(chat_id: int, message_id: int):
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT user_id FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def db_replace_reactions(chat_id: int, message_id: int, reactor_id: int,
                         old_emojis: set, new_emojis: set, date_ts: int):
    conn = _connect()
    try:
        removed = old_emojis - new_emojis
        for emoji in removed:
            conn.execute(
                """DELETE FROM reactions
                   WHERE chat_id = ? AND message_id = ? AND reactor_id = ? AND reaction = ?""",
                (chat_id, message_id, reactor_id, emoji)
            )
        added = new_emojis - old_emojis
        for emoji in added:
            conn.execute(
                """INSERT OR IGNORE INTO reactions
                   (chat_id, message_id, reactor_id, reaction, date)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_id, message_id, reactor_id, emoji, date_ts)
            )
        conn.commit()
    finally:
        conn.close()


def db_delete_reactions_for_message(chat_id: int, message_id: int) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM reactions WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        deleted = cur.rowcount
        conn.execute(
            "DELETE FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        conn.commit()
        try:
            conn.execute("PRAGMA optimize")
        except Exception:
            pass
        return deleted
    finally:
        conn.close()


def db_delete_reactions_older_than(days: int):
    conn = _connect()
    try:
        if days == 0:
            # Полная очистка: не полагаемся на date < now.
            cur = conn.execute("DELETE FROM reactions")
            n_reactions = cur.rowcount
            cur = conn.execute("DELETE FROM messages")
            n_messages = cur.rowcount
        else:
            cutoff = int(time.time()) - days * 24 * 3600
            cur = conn.execute("DELETE FROM reactions WHERE date < ?", (cutoff,))
            n_reactions = cur.rowcount
            cur = conn.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
            n_messages = cur.rowcount
        conn.commit()
        try:
            conn.execute("PRAGMA optimize")
        except Exception:
            pass
        return n_reactions, n_messages
    finally:
        conn.close()


def db_get_karma(user_id: int, since: int = 0) -> int:
    pos = list(POSITIVE_REACTIONS)
    neg = list(NEGATIVE_REACTIONS)
    query = f"""
        SELECT COALESCE(SUM(
            CASE
                WHEN r.reaction IN ({placeholders(pos)}) THEN 1
                WHEN r.reaction IN ({placeholders(neg)}) THEN -1
                ELSE 0
            END
        ), 0)
        FROM reactions r
        JOIN messages m
          ON r.chat_id = m.chat_id AND r.message_id = m.message_id
        WHERE m.user_id = ? AND r.date >= ?
    """
    conn = _connect()
    try:
        cur = conn.execute(query, (*pos, *neg, user_id, since))
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def db_get_rank(user_id: int, since: int = 0, karma_user: int = None) -> int:
    """Возвращает место пользователя в рейтинге.
    `karma_user` можно передать, чтобы избежать повторного вызова db_get_karma.

    Ранг = 1 + количество пользователей со строго большей кармой.
    Это даёт корректный результат и для нулевой, и для отрицательной кармы."""
    pos = list(POSITIVE_REACTIONS)
    neg = list(NEGATIVE_REACTIONS)
    if karma_user is None:
        karma_user = db_get_karma(user_id, since)

    query = f"""
        SELECT COUNT(*) + 1 FROM (
            SELECT m.user_id AS uid, SUM(
                CASE
                    WHEN r.reaction IN ({placeholders(pos)}) THEN 1
                    WHEN r.reaction IN ({placeholders(neg)}) THEN -1
                    ELSE 0
                END
            ) AS karma
            FROM reactions r
            JOIN messages m
              ON r.chat_id = m.chat_id AND r.message_id = m.message_id
            WHERE r.date >= ?
            GROUP BY m.user_id
            HAVING karma > ?
        )
    """
    params = (*pos, *neg, since, karma_user)

    conn = _connect()
    try:
        cur = conn.execute(query, params)
        row = cur.fetchone()
        return int(row[0]) if row else 1
    finally:
        conn.close()


def db_get_top(limit: int = 5, since: int = 0):
    pos = list(POSITIVE_REACTIONS)
    neg = list(NEGATIVE_REACTIONS)
    query = f"""
        SELECT m.user_id,
               u.username,
               u.first_name,
               SUM(
                   CASE
                       WHEN r.reaction IN ({placeholders(pos)}) THEN 1
                       WHEN r.reaction IN ({placeholders(neg)}) THEN -1
                       ELSE 0
                   END
               ) AS karma
        FROM reactions r
        JOIN messages m
          ON r.chat_id = m.chat_id AND r.message_id = m.message_id
        LEFT JOIN users u ON m.user_id = u.user_id
        WHERE r.date >= ?
        GROUP BY m.user_id
        HAVING karma != 0
        ORDER BY karma DESC, m.user_id ASC
        LIMIT ?
    """
    conn = _connect()
    try:
        cur = conn.execute(query, (*pos, *neg, since, limit))
        return cur.fetchall()
    finally:
        conn.close()


def db_find_user_by_username(username: str):
    conn = _connect()
    try:
        cur = conn.execute(
            """SELECT user_id, first_name, username FROM users
               WHERE username = ? COLLATE NOCASE
               ORDER BY user_id DESC LIMIT 1""",
            (username,)
        )
        return cur.fetchone()
    finally:
        conn.close()


def extract_emojis(reactions) -> set:
    """Из списка ReactionType возвращает set строк-идентификаторов реакций.

    Кастомные эмодзи маркируются префиксом 'custom:<id>'.
    В POSITIVE_REACTIONS / NEGATIVE_REACTIONS такие строки не задаются —
    кастомные реакции по умолчанию игнорируются. Если нужно учитывать —
    добавьте их id в конфиг в формате 'custom:<id>'."""
    result = set()
    if not reactions:
        return result
    for r in reactions:
        emoji = getattr(r, "emoji", None)
        if emoji:
            result.add(emoji)
            continue
        custom_id = getattr(r, "custom_emoji_id", None)
        if custom_id:
            result.add(f"custom:{custom_id}")
    return result


def _entity_text(msg, ent) -> str:
    """Безопасно достаёт текст сущности, учитывая UTF-16 offset (Telegram API)."""
    text = msg.text or msg.caption or ""
    try:
        encoded = text.encode("utf-16-le")
        start = ent.offset * 2
        end = (ent.offset + ent.length) * 2
        return encoded[start:end].decode("utf-16-le")
    except Exception:
        return text[ent.offset:ent.offset + ent.length]


# --- Async-обёртки для БД (не блокируем event loop) ---

def _to_thread(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


db_upsert_user_a = _to_thread(db_upsert_user)
db_save_message_a = _to_thread(db_save_message)
db_get_message_author_a = _to_thread(db_get_message_author)
db_replace_reactions_a = _to_thread(db_replace_reactions)
db_delete_reactions_for_message_a = _to_thread(db_delete_reactions_for_message)
db_delete_reactions_older_than_a = _to_thread(db_delete_reactions_older_than)
db_get_karma_a = _to_thread(db_get_karma)
db_get_rank_a = _to_thread(db_get_rank)
db_get_top_a = _to_thread(db_get_top)
db_find_user_by_username_a = _to_thread(db_find_user_by_username)


# =========================================================
#                       ХЕНДЛЕРЫ
# =========================================================

async def save_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or user.is_bot:
        return
    if msg.chat is None:
        return
    ts = int(msg.date.timestamp()) if msg.date else int(time.time())
    try:
        await db_upsert_user_a(user.id, user.username or "", user.first_name or "")
        await db_save_message_a(msg.chat.id, msg.message_id, user.id, ts)
    except Exception as e:
        logger.error(f"save_message_handler: {e}")


async def reaction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction = update.message_reaction
    if reaction is None:
        return

    reactor = reaction.user
    if reactor is None:
        logger.debug("reaction без user (аноним) chat=%s msg=%s",
                     reaction.chat.id, reaction.message_id)
        return
    if reactor.is_bot:
        return

    chat_id = reaction.chat.id
    message_id = reaction.message_id
    reactor_id = reactor.id
    now = int(time.time())

    old_emojis = extract_emojis(reaction.old_reaction)
    new_emojis = extract_emojis(reaction.new_reaction)

    author_id = await db_get_message_author_a(chat_id, message_id)
    if author_id is None:
        logger.info(
            "reaction: msg %s в чате %s не найден в БД — пропускаем "
            "(reactor=%s, old=%s, new=%s)",
            message_id, chat_id, reactor_id, old_emojis, new_emojis
        )
        return

    # Запрет самолайка: реакции на собственные сообщения не учитываются.
    if reactor_id == author_id:
        logger.debug(
            "reaction: self-reaction ignored chat=%s msg=%s user=%s",
            chat_id, message_id, reactor_id,
        )
        return

    try:
        await db_replace_reactions_a(
            chat_id, message_id, reactor_id, old_emojis, new_emojis, now
        )
        logger.info(
            "reaction: chat=%s msg=%s автор=%s реактор=%s old=%s new=%s "
            "(удалено=%s, добавлено=%s)",
            chat_id, message_id, author_id, reactor_id,
            old_emojis, new_emojis,
            old_emojis - new_emojis, new_emojis - old_emojis,
        )
    except Exception as e:
        logger.error(f"reaction_handler: {e}")


async def karma_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return

    caller = update.effective_user
    if caller is None:
        return

    target_id = None
    target_name = None
    source = None

    # 1) Аргумент @username или user_id
    if context.args:
        raw = context.args[0].lstrip("@").strip()
        if raw:
            row = await db_find_user_by_username_a(raw)
            if row:
                target_id = row[0]
                target_name = row[1] or row[2] or str(row[0])
                source = "arg_username"
            else:
                try:
                    target_id = int(raw)
                    target_name = str(target_id)
                    source = "arg_user_id"
                except ValueError:
                    await msg.reply_text(
                        "❌ Пользователь не найден. Укажите @username или выберите "
                        "участника из списка через @."
                    )
                    return

    # 2) text_mention / mention
    if target_id is None:
        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        mention_not_found = False
        for ent in entities:
            if ent.type == "text_mention" and ent.user is not None and not ent.user.is_bot:
                u = ent.user
                target_id = u.id
                target_name = getattr(u, "full_name", None) or u.username or str(u.id)
                source = "text_mention"
                try:
                    await db_upsert_user_a(u.id, u.username or "", u.first_name or "")
                except Exception as e:
                    logger.error(f"karma: не удалось обновить users для {u.id}: {e}")
                break
            if ent.type == "mention":
                raw = _entity_text(msg, ent).lstrip("@").strip()
                if raw:
                    row = await db_find_user_by_username_a(raw)
                    if row:
                        target_id = row[0]
                        target_name = row[1] or row[2] or str(row[0])
                        source = "mention"
                        break
                    mention_not_found = True
        # Если пользователь явно упомянул кого-то через @, но мы его не знаем —
        # не подменяем цель на отправителя, а честно сообщаем.
        if target_id is None and mention_not_found:
            await msg.reply_text(
                "❌ Пользователь с таким @username не найден в базе. "
                "Попросите его написать что-нибудь в чат или укажите user_id."
            )
            return

    # 3) Своя карма
    if target_id is None:
        target_id = caller.id
        target_name = caller.full_name or caller.username or str(caller.id)
        source = "self"

    now = int(time.time())
    month_ago = now - 30 * 24 * 3600

    # Считаем каждую метрику один раз и переиспользуем для ранга.
    total_karma = await db_get_karma_a(target_id)
    month_karma = await db_get_karma_a(target_id, month_ago)
    total_rank = await db_get_rank_a(target_id, since=0, karma_user=total_karma)
    month_rank = await db_get_rank_a(target_id, since=month_ago, karma_user=month_karma)

    logger.info(
        "karma: запрос от %s в чате %s → target=%s (%s), source=%s, "
        "karma_total=%s, karma_month=%s",
        caller.id, msg.chat.id, target_id, target_name, source, total_karma, month_karma
    )

    safe_name = html.escape(str(target_name))
    await msg.reply_text(
        f"📊 <b>Карма: {safe_name}</b>\n\n"
        f"🔹 Всего: <b>{total_karma}</b> (место в рейтинге: {total_rank})\n"
        f"🔸 За месяц: <b>{month_karma}</b> (место в рейтинге: {month_rank})",
        parse_mode='HTML'
    )


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return

    now = int(time.time())
    month_ago = now - 30 * 24 * 3600

    top_total = await db_get_top_a(5, 0)
    top_month = await db_get_top_a(5, month_ago)

    def fmt(rows):
        if not rows:
            return "— пока нет данных —"
        lines = []
        for i, (uid, username, first_name, karma) in enumerate(rows, 1):
            raw = first_name or (f"@{username}" if username else str(uid))
            name = html.escape(str(raw))
            lines.append(f"{i}. {name} — <b>{karma}</b>")
        return "\n".join(lines)

    await msg.reply_text(
        "🏆 <b>Топ-5 за всё время</b>\n"
        f"{fmt(top_total)}\n\n"
        "📅 <b>Топ-5 за месяц</b>\n"
        f"{fmt(top_month)}",
        parse_mode='HTML'
    )


async def karmaclean_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or msg is None:
        return
    if chat.type == "private":
        await msg.reply_text("❌ Команда доступна только в группах.")
        return
    if not await check_admin(update, context):
        await msg.reply_text("❌ Только для администраторов чата.")
        return
    if msg.reply_to_message is None:
        await msg.reply_text("↩️ Ответьте этой командой на сообщение, реакции которого нужно удалить.")
        return
    chat_id = msg.chat.id
    message_id = msg.reply_to_message.message_id
    n = await db_delete_reactions_for_message_a(chat_id, message_id)
    await msg.reply_text(f"🧹 Удалено реакций: {n} (сообщение {message_id}).")


async def karmapurge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or msg is None:
        return
    if chat.type == "private":
        await msg.reply_text("❌ Команда доступна только в группах.")
        return
    if not await check_admin(update, context):
        await msg.reply_text("❌ Только для администраторов чата.")
        return
    if not context.args:
        await msg.reply_text(
            "Использование: <code>/karmapurge &lt;дни&gt;</code>\n"
            "<code>0</code> — очистить всё (требует подтверждения).\n"
            "Например: <code>/karmapurge 180</code>",
            parse_mode='HTML'
        )
        return
    try:
        days = int(context.args[0])
        if days < 0:
            raise ValueError
    except ValueError:
        await msg.reply_text("❌ Нужно целое число дней ≥ 0 (0 = очистить всё).")
        return

    # Защита от случайного сноса всей истории: /karmapurge 0 требует confirm.
    if days == 0 and (len(context.args) < 2 or context.args[1].lower() != "confirm"):
        await msg.reply_text(
            "⚠️ <code>/karmapurge 0</code> сотрёт <b>всю</b> историю кармы и сообщений.\n"
            "Если уверены — повторите: <code>/karmapurge 0 confirm</code>",
            parse_mode='HTML'
        )
        return

    n_reactions, n_messages = await db_delete_reactions_older_than_a(days)
    if days == 0:
        await msg.reply_text(f"🧹 Полная очистка: удалено реакций {n_reactions}, сообщений {n_messages}.")
    else:
        await msg.reply_text(f"🧹 Удалено реакций старше {days} дн.: {n_reactions}, сообщений: {n_messages}.")


# =========================================================
#                       ОСНОВНЫЕ КОМАНДЫ
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"Привет, {get_user_mention(user)}!")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"{get_user_mention(user)}, я умею:\n"
        "/pair – случайные пары из слов\n"
        "/karma – показать СВОЮ карму\n"
        "/karma @username – карма указанного пользователя\n"
        "/top – топ-5 за месяц и за всё время\n"
        "/version – версия бота\n"
        f"А ещё я проверяю новых участников и удаляю их, если они не нажимают "
        f"кнопку подтверждения в течение {_kick_human()}.\n"
        "И считаю карму по реакциям (👍 +1, 👎 −1)."
    )


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🤖 Версия бота: {BOT_VERSION}")


async def _call_ollama_async(
    user_message: str,
    chat_id: "int | None" = None,
    thread_id: "int | None" = None,
) -> str:
    system_content = LLM_PROMPT_STATIC

    messages = [{"role": "system", "content": system_content}]

    if chat_id is not None and thread_id is not None:
        hist = trim_history(
            _get_history(chat_id, thread_id),
            CFG["llm"]["history_max_messages"],
            CFG["llm"]["history_max_chars"],
        )
        messages.extend(hist)

    messages.append({"role": "user", "content": user_message[:LLM_MAX_INPUT_CHARS]})

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "num_ctx": CFG["llm"]["num_ctx"],
            "num_predict": CFG["llm"]["num_predict"],
            "temperature": CFG["llm"]["temperature"],
            "top_p": CFG["llm"]["top_p"],
            "repeat_penalty": CFG["llm"]["repeat_penalty"],
        },
    }
    # Общий клиент — переиспользуем HTTP-соединения.
    client = await _get_http_client()
    r = await client.post(OLLAMA_URL, json=payload)
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "").strip()


async def send_smart_reply(message_obj, user_message: str,
                           chat_id: "int | None" = None,
                           chat_type: "str | None" = None,
                           chat_title: "str | None" = None,
                           thread_id: "int | None" = None,
                           user=None):
    # _spawn держит ссылку в _background_tasks — задача отменится на shutdown.
    llm_task = _spawn(_call_ollama_async(user_message, chat_id, thread_id))
    placeholder = None
    reply_text = None
    used_fallback = False
    last_ph = None

    try:
        done, _ = await asyncio.wait({llm_task}, timeout=LLM_THINKING_DELAY)

        if llm_task not in done:
            last_ph = random.choice(THINKING_PLACEHOLDERS)
            try:
                placeholder = await message_obj.reply_text(last_ph)
                if chat_id is not None:
                    try:
                        get_chat_logger(chat_id, chat_type, user=user,
                                        chat_title=chat_title).info(
                            f".. [{last_ph}]"
                        )
                    except Exception as e:
                        logger.error(f"placeholder log write failed: {e}")
            except Exception as e:
                logger.error(f"placeholder send failed: {e}")

            deadline = time.monotonic() + LLM_HARD_TIMEOUT
            while True:
                if llm_task.done():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait_for = min(LLM_THINKING_DELAY, remaining)
                done, _ = await asyncio.wait({llm_task}, timeout=wait_for)
                if llm_task in done:
                    break
                candidates = [p for p in THINKING_PLACEHOLDERS if p != last_ph]
                new_ph = random.choice(candidates) if candidates else random.choice(THINKING_PLACEHOLDERS)
                last_ph = new_ph
                if placeholder is not None:
                    try:
                        await placeholder.edit_text(new_ph)
                        if chat_id is not None:
                            try:
                                get_chat_logger(chat_id, chat_type, user=user,
                                                chat_title=chat_title).info(
                                    f".. [{new_ph}]"
                                )
                            except Exception as e:
                                logger.error(f"placeholder log write failed: {e}")
                    except Exception as e:
                        logger.debug(f"placeholder edit skipped: {e}")

        if llm_task.done() and not llm_task.cancelled():
            try:
                raw = llm_task.result()
            except Exception as e:
                logger.error(f"LLM error: {e}")
                raw = None
        else:
            logger.warning("LLM hard timeout → fallback")
            llm_task.cancel()
            try:
                await llm_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            raw = None

        if raw:
            candidate = raw.strip().strip('"').strip("«»").split("\n")[0].strip()
            if candidate and len(candidate.split()) <= LLM_MAX_WORDS:
                reply_text = candidate
            else:
                logger.warning(f"LLM ответ не прошёл валидацию: {raw[:80]!r}")
        else:
            logger.warning("LLM returned empty result")

    except Exception as e:
        logger.error(f"send_smart_reply: {e}")

    if not reply_text:
        reply_text = random.choice(PHRASES)
        used_fallback = True

    if chat_id is not None:
        try:
            chat_lg = get_chat_logger(chat_id, chat_type, user=user, chat_title=chat_title)
            source = "fallback" if used_fallback else "LLM"
            chat_lg.info(f"-> [{source}]: {reply_text[:1000]}")
        except Exception as e:
            logger.error(f"chat log write failed: {e}")

    # В историю пишем только успешный ответ LLM — фолбэк-фразы не должны
    # попадать в контекст, иначе они загрязняют промпт и портят следующие ответы.
    if (chat_id is not None and thread_id is not None
            and user_message.strip() and not used_fallback):
        hist = _get_history(chat_id, thread_id)
        hist.append({"role": "user", "content": user_message[:LLM_MAX_INPUT_CHARS]})
        hist.append({"role": "assistant", "content": reply_text})
        _chat_histories[(chat_id, thread_id)] = trim_history(
            hist,
            CFG["llm"]["history_max_messages"],
            CFG["llm"]["history_max_chars"],
        )

    sent_msg = None
    if placeholder:
        try:
            sent_msg = await placeholder.edit_text(reply_text)
        except Exception as e:
            logger.error(f"edit_text failed: {e}")

    if sent_msg is None:
        try:
            sent_msg = await message_obj.reply_text(reply_text)
        except Exception as e:
            logger.error(f"reply_text failed: {e}")

    if sent_msg is not None and chat_id is not None and thread_id is not None:
        _remember_message(chat_id, sent_msg.message_id, thread_id)


async def _handle_llm_turn(update, context, edit_ts=None):
    global _llm_last_call

    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    user_message = msg.text or msg.caption or ""

    if edit_ts is None:
        edit_ts = msg_edit_ts(msg)
    if _is_duplicate(chat.id, msg.message_id, edit_ts):
        logger.info("skip duplicate: chat=%s msg_id=%s edit=%s",
                    chat.id, msg.message_id, edit_ts)
        return

    # Rate limit: не даём спамить LLM одному пользователю.
    now = time.monotonic()
    rl_key = (chat.id, user.id)
    last = _llm_last_call.get(rl_key, 0.0)
    if now - last < LLM_USER_COOLDOWN:
        logger.info(
            "LLM cooldown: chat=%s user=%s (осталось %.2fs)",
            chat.id, user.id, LLM_USER_COOLDOWN - (now - last),
        )
        return
    _llm_last_call[rl_key] = now
    if len(_llm_last_call) > 10000:
        cutoff = now - 60.0
        _llm_last_call = {k: v for k, v in _llm_last_call.items() if v > cutoff}

    thread_id = _resolve_thread_key(chat, msg)
    _remember_message(chat.id, msg.message_id, thread_id)

    try:
        chat_lg = get_chat_logger(chat.id, chat.type, user=user, chat_title=chat.title)
        mention = f"@{user.username}" if user.username else (user.full_name or "unknown")
        chat_lg.info(f"<- [{mention} id={user.id}]: {user_message[:1000]}")
    except Exception as e:
        logger.error(f"chat log write failed: {e}")

    # Сериализуем обработку внутри треда: пока LLM не ответил на предыдущее
    # сообщение, следующее в том же треде подождёт. Это гарантирует корректный
    # порядок пар user/assistant в истории при concurrent_updates.
    # Трейд-офф: сообщения одного юзера в одном треде обрабатываются подряд.
    lock = _get_thread_lock(chat.id, thread_id)

    async def _do_turn():
        async with lock:
            await send_smart_reply(
                msg, user_message,
                chat_id=chat.id, chat_type=chat.type, chat_title=chat.title,
                thread_id=thread_id,
                user=user,
            )

    _spawn(_do_turn())


async def talk_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
    # Игнорируем апдейты без текста/подписи (стикеры, войсы, гифки и т.п.).
    if not (msg.text or msg.caption):
        return

    text = msg.text or msg.caption or ""
    bot_username = context.bot.username
    mentioned = is_mentioned(text, bot_username)
    reply_to_bot = (
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == context.bot.id
    )

    edit_ts = msg_edit_ts(msg)

    if update.effective_chat.type != 'private':
        if not mentioned and not reply_to_bot:
            return
    elif edit_ts != 0:
        # В привате обычные сообщения идут к LLM без условий. Но edit —
        # это правка опечатки или случайный триггер; реагируем только если
        # пользователь явно упомянул бота или ответил на его сообщение.
        if not mentioned and not reply_to_bot:
            return

    await _handle_llm_turn(update, context, edit_ts=edit_ts)


async def pair_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👥 Введите имена (или слова) через пробел.\n"
        "Например: Ашот Вазген Сигизмунд Гимнокалициум\n\n"
        "Или отправьте /cancel, чтобы отменить."
    )
    return WAITING_FOR_NAMES


async def pair_receive_names(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    names = text.split()
    if len(names) < 2:
        await update.message.reply_text("❌ Нужно минимум 2 слова. Попробуйте ещё раз.")
        return WAITING_FOR_NAMES

    shuffled = names.copy()
    random.shuffle(shuffled)
    pairs = []
    i = 0
    while i + 1 < len(shuffled):
        pairs.append(f"{shuffled[i]} — {shuffled[i+1]}")
        i += 2
    if i < len(shuffled):
        pairs.append(f"{shuffled[i]} (без пары)")

    answer = "🎲 Случайные пары:\n" + "\n".join(pairs)
    await update.message.reply_text(answer)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


async def remove_user(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    chat_id = data['chat_id']
    user_id = data['user_id']
    user_name = data.get('user_name', 'Unknown')
    user_username = data.get('user_username', 'no_username')
    message_id = data.get('message_id')

    key = (chat_id, user_id)
    if pending_jobs.get(key) is not job:
        logger.info(f"remove_user: job для {user_id} в чате {chat_id} уже неактуален")
        return
    pending_jobs.pop(key, None)

    # ban и unban — независимо: если ban упал, unban всё равно нужен,
    # чтобы не оставить пользователя забаненным по ошибке.
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        logger.info(f"Забанен {user_name} (@{user_username}) [ID: {user_id}] в чате {chat_id}")
    except Exception as e:
        logger.error(f"Не удалось забанить {user_id} в чате {chat_id}: {e}")

    try:
        await context.bot.unban_chat_member(chat_id, user_id)
        logger.info(f"Разбанен {user_name} (@{user_username}) [ID: {user_id}] в чате {chat_id}")
    except Exception as e:
        logger.error(f"Не удалось разбанить {user_id} в чате {chat_id}: {e}")

    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ Время истекло, бот пристрелен"
            )
        except Exception as e:
            logger.error(f"Не удалось отредактировать сообщение {message_id}: {e}")


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    key = (chat_id, user_id)

    job = pending_jobs.get(key)
    if job is None:
        await query.answer("⏳ Время истекло или вы уже подтверждены.", show_alert=True)
        return

    # Сначала убираем из словаря, потом снимаем job — чтобы remove_user
    # не сработал параллельно.
    pending_jobs.pop(key, None)
    try:
        job.remove()
    except Exception:
        pass

    await query.answer("✅ Подтверждено")

    user = query.from_user
    user_name = user.full_name or user.first_name or "No name"
    user_username = user.username or "no_username"
    logger.info(f"Пользователь {user_name} (@{user_username}) [ID: {user_id}] подтверждён в чате {chat_id} (кнопка)")

    try:
        await query.edit_message_text("✅ Вы подтверждены! Добро пожаловать в чат.")
    except Exception as e:
        logger.error(f"confirm_callback edit failed: {e}")


async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return

    chat_id = update.message.chat_id

    now_utc = datetime.now(timezone.utc)
    msg_time = update.effective_message.date
    if msg_time < now_utc - timedelta(seconds=MAX_MESSAGE_AGE_SECONDS):
        logger.info(f"Пропускаем устаревшее сообщение о новом участнике от {msg_time}")
        return

    # JobQueue проверяем до всех действий — иначе можем отправить кнопку,
    # а джоба на удаление не будет.
    if context.job_queue is None:
        logger.error("JobQueue не инициализирован — пропускаем обработку нового участника")
        return

    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    except Exception as e:
        logger.error(f"Не удалось получить информацию о боте в чате {chat_id}: {e}")
        return

    # У ChatMemberOwner нет can_restrict_members — берём через getattr.
    can_restrict = getattr(bot_member, "can_restrict_members", False)
    if bot_member.status not in ("creator", "administrator") or not can_restrict:
        logger.warning(f"Бот не имеет прав на удаление участников в чате {chat_id}")
        return

    # Дубли апдейта о новом участнике плодят вторую кнопку/джоб — режем.
    # Дедуп ставим после валидации, чтобы не «съесть» потенциально валидный
    # повтор, если в первый раз бот не имел прав.
    if _is_duplicate(chat_id, update.message.message_id, 0):
        logger.info(
            "skip duplicate new_member: chat=%s msg_id=%s",
            chat_id, update.message.message_id,
        )
        return

    kick_human = _kick_human()

    for new_member in update.message.new_chat_members:
        if new_member.id == context.bot.id:
            continue

        user_id = new_member.id
        key = (chat_id, user_id)

        if key in pending_jobs:
            old_job = pending_jobs.pop(key)
            if old_job:
                try:
                    old_job.remove()
                except Exception:
                    pass

        user_full_name = new_member.full_name or new_member.first_name or "No name"
        user_username = new_member.username or "no_username"

        keyboard = [[InlineKeyboardButton("✅ Я человек", callback_data="confirm")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            sent_msg = await update.message.reply_text(
                f"Привет, {new_member.mention_html()}! Нажми кнопку ниже, чтобы подтвердить, что ты человек.\n"
                f"Если не нажмёшь в течение {kick_human}, я удалю тебя из чата.",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
            continue

        job = context.job_queue.run_once(
            remove_user,
            when=CFG["new_member"]["kick_after_seconds"],
            data={
                'chat_id': chat_id,
                'user_id': user_id,
                'user_name': user_full_name,
                'user_username': user_username,
                'message_id': sent_msg.message_id
            },
            name=f"remove_{chat_id}_{user_id}"
        )
        pending_jobs[key] = job

        logger.info(
            f"Новый пользователь {user_full_name} (@{user_username}) [ID: {user_id}] "
            f"зашёл в чат {chat_id}, отправлена кнопка подтверждения."
        )


async def warmup_llm():
    logger.info("LLM warmup started...")
    try:
        result = await _call_ollama_async("привет")
        logger.info(f"LLM warmup done: {result[:60]!r}")
    except Exception as e:
        logger.warning(f"LLM warmup failed: {e}")


# ----- post_init / post_shutdown -----
async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Запустить бота"),
            BotCommand("help", "Список возможностей"),
            BotCommand("pair", "Случайные пары из слов"),
            BotCommand("karma", "Показать карму"),
            BotCommand("top", "Топ-5 за месяц и за всё время"),
            BotCommand("version", "Версия бота"),
        ])
        logger.info("Команды бота обновлены через set_my_commands")
    except Exception as e:
        logger.error(f"Не удалось установить команды: {e}")

    _spawn(warmup_llm())


async def post_shutdown(application: Application):
    # Ожидающие кики не переживают рестарт — логируем, чтобы понимать масштаб.
    if pending_jobs:
        logger.warning(
            "При завершении осталось %d необработанных задач на кик. "
            "Они не переживут рестарт — соответствующие пользователи "
            "останутся в чатах без подтверждения.",
            len(pending_jobs),
        )

    # Сначала гасим фоновые задачи (могут держать соединения),
    # затем закрываем общий HTTP-клиент и чистим локи.
    if _background_tasks:
        logger.info("Отменяю %d фоновых задач...", len(_background_tasks))
        for t in list(_background_tasks):
            t.cancel()
        await asyncio.gather(*_background_tasks, return_exceptions=True)
        _background_tasks.clear()
    _history_locks.clear()
    await _close_http_client()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    update_id = getattr(update, "update_id", None)
    chat_id = None
    try:
        chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    except Exception:
        pass
    logger.error(
        "Ошибка при обработке апдейта: update_id=%s chat_id=%s",
        update_id, chat_id, exc_info=context.error,
    )


async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CFG, LLM_PROMPT_STATIC, PHRASES, THINKING_PLACEHOLDERS
    global LLM_THINKING_DELAY, LLM_HARD_TIMEOUT, LLM_MAX_WORDS, LLM_MAX_INPUT_CHARS
    global POSITIVE_REACTIONS, NEGATIVE_REACTIONS, MAX_MESSAGE_AGE_SECONDS
    global LLM_USER_COOLDOWN
    global _chat_histories

    user = update.effective_user
    if OWNER_ID is None or user.id != OWNER_ID:
        return

    try:
        new_cfg = load_config()
        _validate_config(new_cfg)
        CFG = new_cfg

        # Сбрасываем и истории, и маппинг message→thread — они связаны,
        # держать одну без другой неаккуратно.
        _chat_histories = OrderedDict()
        _msg_to_thread.clear()
        # Свободные локи тоже не нужны — они были привязаны к старым историям.
        for k in list(_history_locks.keys()):
            if not _history_locks[k].locked():
                _history_locks.pop(k, None)

        LLM_THINKING_DELAY = CFG["llm"]["thinking_delay"]
        LLM_HARD_TIMEOUT = CFG["llm"]["hard_timeout"]
        LLM_MAX_WORDS = CFG["llm"]["max_words"]
        LLM_MAX_INPUT_CHARS = CFG["llm"]["max_input_chars"]
        LLM_USER_COOLDOWN = float(
            os.getenv("LLM_USER_COOLDOWN", CFG["llm"].get("user_cooldown", 2.0))
        )

        LLM_PROMPT_STATIC = safe_format(CFG["prompts"]["system"], max_words=LLM_MAX_WORDS)
        PHRASES = CFG["phrases"]
        THINKING_PLACEHOLDERS = CFG["placeholders"]
        POSITIVE_REACTIONS = set(CFG["reactions"]["positive"])
        NEGATIVE_REACTIONS = set(CFG["reactions"]["negative"])
        MAX_MESSAGE_AGE_SECONDS = CFG["new_member"]["max_message_age_seconds"]

        await update.message.reply_text(
            f"✅ Конфиг перезагружен.\n"
            f"фраз: {len(PHRASES)}, затычек: {len(THINKING_PLACEHOLDERS)}, "
            f"temperature: {CFG['llm']['temperature']}, "
            f"cooldown: {LLM_USER_COOLDOWN}с"
        )
        logger.info("config.yaml перезагружен через /reload")
    except Exception as e:
        logger.error(f"reload failed: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message

    if OWNER_ID is None or user.id != OWNER_ID:
        return
    if chat is None or msg is None:
        return

    thread_id = _resolve_thread_key(chat, msg)
    _clear_thread(chat.id, thread_id)
    await msg.reply_text("🧹 История этой ветки очищена.")
    logger.info(f"thread cleared: chat={chat.id} thread={thread_id}")


# ----- Главная -----
def main():
    _force_ipv4()

    init_db()

    application = (
        Application.builder()
        .token(TOKEN)
        # По умолчанию — официальный Telegram API. Прокси включается явно через ENV.
        .base_url(os.getenv("TG_BASE_URL", "https://api.telegram.org/bot"))
        .base_file_url(os.getenv("TG_BASE_FILE_URL", "https://api.telegram.org/file/bot"))
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        # Параллельная обработка апдейтов: пока один пользователь ждёт LLM,
        # остальные сообщения/реакции/входы обрабатываются.
        # 64 — жёсткий потолок параллельных корутин (сглаживает пики апдейтов).
        .concurrent_updates(64)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    if application.job_queue is None:
        logger.critical(
            "JobQueue не инициализирован. Установите python-telegram-bot[job-queue] "
            "или передайте JobQueue() в ApplicationBuilder."
        )
        return

    application.add_error_handler(error_handler)

    application.add_handler(
        MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, save_message_handler),
        group=-1
    )
    application.add_handler(MessageReactionHandler(reaction_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    application.add_handler(CallbackQueryHandler(confirm_callback, pattern="confirm"))

    pair_handler = ConversationHandler(
        entry_points=[CommandHandler("pair", pair_start)],
        states={WAITING_FOR_NAMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, pair_receive_names)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(pair_handler)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("version", version_command))
    application.add_handler(CommandHandler("karma", karma_command))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CommandHandler("karmaclean", karmaclean_command))
    application.add_handler(CommandHandler("karmapurge", karmapurge_command))
    application.add_handler(CommandHandler("reload", reload_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk_response))

    logger.info("Бот запущен!")
    application.run_polling(
        allowed_updates=[
            "message",
            "edited_message",
            "callback_query",
            "message_reaction",
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
