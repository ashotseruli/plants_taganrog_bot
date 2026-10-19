"""SQLite-слой: схема, функции, async-обёртки.

Здесь всё, что работает с karma.db:
- схема БД (init_db)
- чтение/запись users/messages/reactions
- расчёт кармы и рейтингов
- async-обёртки через asyncio.to_thread — чтобы не блокировать event loop

POSITIVE_REACTIONS / NEGATIVE_REACTIONS задаются снаружи (обычно app.py)
через прямую установку атрибутов модуля — чтобы db.py не зависел от конфига
и оставался тестируемым в изоляции.
"""
import asyncio
import functools
import logging
import sqlite3
import time
from pathlib import Path

from utils import placeholders

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "karma.db")

# Задаются извне (app.py) или в тестах. По умолчанию пусты — карма всегда 0.
POSITIVE_REACTIONS: set = set()
NEGATIVE_REACTIONS: set = set()


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

    Ранг = 1 + количество пользователей со строго большей кармой."""
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

def db_get_user(user_id: int):
    """Возвращает (user_id, username, first_name) или None."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT user_id, username, first_name FROM users WHERE user_id = ?",
            (user_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()
        
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
db_get_user_a = _to_thread(db_get_user)
