import socket

# --- ПРИНУДИТЕЛЬНОЕ ИСПОЛЬЗОВАНИЕ IPv4 ---
_original_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(*args, **kwargs):
    responses = _original_getaddrinfo(*args, **kwargs)
    # Оставляем только IPv4 адреса, если они есть
    ipv4_responses = [r for r in responses if r[0] == socket.AF_INET]
    return ipv4_responses or responses # Если IPv4 нет, возвращаем как было
socket.getaddrinfo = _ipv4_only_getaddrinfo
# ----------------------------------------
import os
import logging
import random
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, JobQueue,
    CallbackQueryHandler, MessageReactionHandler
)

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
    level=logging.WARNING,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
formatter = MoscowTimeFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
for handler in logging.getLogger().handlers:
    handler.setFormatter(formatter)

logger = logging.getLogger(__name__)

# --- Хранилище ---
pending_jobs = {}
WAITING_FOR_NAMES = 1
BOT_VERSION = "2.0.0"

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("Токен не найден!")

# --- Константы ---
MAX_MESSAGE_AGE_SECONDS = 300

DB_PATH = "karma.db"

POSITIVE_REACTIONS = {"👍"}
NEGATIVE_REACTIONS = {"👎"}

PHRASES = [
    "все должны сеять кактусы!",
    "с вас пять тыщь!",
    "все должны выращивать чайный гриб!",
    "гимнокалициумы - цветут!",
    "все должны рожать детей!",
    "написал донос на тебя!",
    "не покупай - посей!",
    "склонен поддержать предыдущего оратора",
    "иди и не греши!",
    "зачем это всё?",
    "ну чего?",
    "кто здесь?",
    "я хочу ясности!",
    "кибернетические организмы не знают усталости",
    "роллтон - лучший горшок!",
    "здесь могла быть ваша реклама!",
    "нет ты!",
    "но почему?",
    "и наоборот",
    "иди спать",
    "и восстали машины из пепла ядерного огня...",
    "давай устроим срач!",
    "как слышно, приём!",
    "это база",
    "шо, опять?",
    "а я че, я ниче",
    "за бутылку водки",
    "как там твои лопухи?",
    "поговорим о погоде?",
    "нам надо поговорить",
    "может ну его?",
    "я против",
    "почему?",
    "диктую номер карты...",
    "категорически поддерживаю!",
    "опа, срач!",
    "ладно, на самом деле я не бот",
    "не понимаю, о чем ты",
    "че?",
    "ой, всё",
    "подписывайтесь на мой канал (https://t.me/zero_love_level_point)",
    "все должны подписываться на инстаграм through_the_limbo",
    "где мой роллс-ройс?",
    "ты мне скажи",
    "а всё-таки, на чем делать настоящую окрошку?",
    "нет",
    "за деньги - да",
    "пруф или не было!",
    "так мы срач не разведём",
    "хватит баловаться!"
]

def get_user_mention(user):
    return f"@{user.username}" if user.username else (user.first_name or "Друг")

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
#                 БАЗА ДАННЫХ / КАРМА
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_reactions_msg ON reactions(chat_id, message_id);
    """)
    conn.commit()
    conn.close()


def db_upsert_user(user_id: int, username: str, first_name: str):
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
        return deleted
    finally:
        conn.close()


def db_delete_reactions_older_than(days: int):
    cutoff = int(time.time()) - days * 24 * 3600
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("DELETE FROM reactions WHERE date < ?", (cutoff,))
        n_reactions = cur.rowcount
        cur = conn.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
        n_messages = cur.rowcount
        conn.commit()
        return n_reactions, n_messages
    finally:
        conn.close()


def _placeholders(seq):
    return ",".join("?" * len(seq))


def db_get_karma(user_id: int, since: int = 0) -> int:
    pos = list(POSITIVE_REACTIONS)
    neg = list(NEGATIVE_REACTIONS)
    query = f"""
        SELECT COALESCE(SUM(
            CASE
                WHEN r.reaction IN ({_placeholders(pos)}) THEN 1
                WHEN r.reaction IN ({_placeholders(neg)}) THEN -1
                ELSE 0
            END
        ), 0)
        FROM reactions r
        JOIN messages m
          ON r.chat_id = m.chat_id AND r.message_id = m.message_id
        WHERE m.user_id = ? AND r.date >= ?
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(query, (*pos, *neg, user_id, since))
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def db_get_rank(user_id: int, since: int = 0) -> int:
    pos = list(POSITIVE_REACTIONS)
    neg = list(NEGATIVE_REACTIONS)
    karma_user = db_get_karma(user_id, since)

    if karma_user == 0:
        query = f"""
            SELECT COUNT(*) + 1 FROM (
                SELECT m.user_id AS uid, SUM(
                    CASE
                        WHEN r.reaction IN ({_placeholders(pos)}) THEN 1
                        WHEN r.reaction IN ({_placeholders(neg)}) THEN -1
                        ELSE 0
                    END
                ) AS karma
                FROM reactions r
                JOIN messages m
                  ON r.chat_id = m.chat_id AND r.message_id = m.message_id
                WHERE r.date >= ?
                GROUP BY m.user_id
                HAVING karma != 0
            )
        """
        params = (*pos, *neg, since)
    else:
        query = f"""
            SELECT COUNT(*) + 1 FROM (
                SELECT m.user_id AS uid, SUM(
                    CASE
                        WHEN r.reaction IN ({_placeholders(pos)}) THEN 1
                        WHEN r.reaction IN ({_placeholders(neg)}) THEN -1
                        ELSE 0
                    END
                ) AS karma
                FROM reactions r
                JOIN messages m
                  ON r.chat_id = m.chat_id AND r.message_id = m.message_id
                WHERE r.date >= ?
                GROUP BY m.user_id
                HAVING karma != 0 AND karma > ?
            )
        """
        params = (*pos, *neg, since, karma_user)

    conn = sqlite3.connect(DB_PATH)
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
                       WHEN r.reaction IN ({_placeholders(pos)}) THEN 1
                       WHEN r.reaction IN ({_placeholders(neg)}) THEN -1
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
        ORDER BY karma DESC
        LIMIT ?
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(query, (*pos, *neg, since, limit))
        return cur.fetchall()
    finally:
        conn.close()


def db_find_user_by_username(username: str):
    conn = sqlite3.connect(DB_PATH)
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
    result = set()
    if not reactions:
        return result
    for r in reactions:
        emoji = getattr(r, "emoji", None)
        if emoji:
            result.add(emoji)
    return result


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
        db_upsert_user(user.id, user.username or "", user.first_name or "")
        db_save_message(msg.chat.id, msg.message_id, user.id, ts)
    except Exception as e:
        logger.error(f"save_message_handler: {e}")


async def reaction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction = update.message_reaction
    if reaction is None:
        return

    reactor = reaction.user
    if reactor is None:
        logger.debug(f"reaction без user (аноним) chat={reaction.chat.id} msg={reaction.message_id}")
        return
    if reactor.is_bot:
        return

    chat_id = reaction.chat.id
    message_id = reaction.message_id
    reactor_id = reactor.id
    now = int(time.time())

    old_emojis = extract_emojis(reaction.old_reaction)
    new_emojis = extract_emojis(reaction.new_reaction)

    author_id = db_get_message_author(chat_id, message_id)
    if author_id is None:
        logger.info(
            f"reaction: msg {message_id} в чате {chat_id} не найден в БД — пропускаем "
            f"(reactor={reactor_id}, old={old_emojis}, new={new_emojis})"
        )
        return

    try:
        db_replace_reactions(chat_id, message_id, reactor_id, old_emojis, new_emojis, now)
        logger.info(
            f"reaction: chat={chat_id} msg={message_id} автор={author_id} "
            f"реактор={reactor_id} old={old_emojis} new={new_emojis} "
            f"(удалено={old_emojis - new_emojis}, добавлено={new_emojis - old_emojis})"
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
            row = db_find_user_by_username(raw)
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

    # 2) text_mention — упоминание через выбор из списка участников
    if target_id is None:
        entities = list(msg.entities or []) + list(msg.caption_entities or [])
        for ent in entities:
            if ent.type == "text_mention" and ent.user is not None and not ent.user.is_bot:
                u = ent.user
                target_id = u.id
                target_name = u.full_name or u.username or str(u.id)
                source = "text_mention"
                try:
                    db_upsert_user(u.id, u.username or "", u.first_name or "")
                except Exception as e:
                    logger.error(f"karma: не удалось обновить users для {u.id}: {e}")
                break
            if ent.type == "mention":
                raw = msg.text[ent.offset:ent.offset + ent.length].lstrip("@").strip()
                if raw:
                    row = db_find_user_by_username(raw)
                    if row:
                        target_id = row[0]
                        target_name = row[1] or row[2] or str(row[0])
                        source = "mention"
                        break

    # 3) Своя карма — всегда, если нет аргументов и упоминаний.
    #    Reply игнорируется.
    if target_id is None:
        target_id = caller.id
        target_name = caller.full_name or caller.username or str(caller.id)
        source = "self"

    now = int(time.time())
    month_ago = now - 30 * 24 * 3600

    total_karma = db_get_karma(target_id)
    month_karma = db_get_karma(target_id, month_ago)
    total_rank = db_get_rank(target_id)
    month_rank = db_get_rank(target_id, month_ago)

    logger.info(
        f"karma: запрос от {caller.id} в чате {msg.chat.id} "
        f"→ target={target_id} ({target_name}), source={source}, "
        f"karma_total={total_karma}, karma_month={month_karma}"
    )

    await msg.reply_text(
        f"📊 *Карма: {target_name}*\n\n"
        f"🔹 Всего: *{total_karma}* (место в рейтинге: {total_rank})\n"
        f"🔸 За месяц: *{month_karma}* (место в рейтинге: {month_rank})",
        parse_mode='Markdown'
    )


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return

    now = int(time.time())
    month_ago = now - 30 * 24 * 3600

    top_total = db_get_top(5, 0)
    top_month = db_get_top(5, month_ago)

    def fmt(rows):
        if not rows:
            return "— пока нет данных —"
        lines = []
        for i, (uid, username, first_name, karma) in enumerate(rows, 1):
            name = first_name or (f"@{username}" if username else str(uid))
            lines.append(f"{i}. {name} — *{karma}*")
        return "\n".join(lines)

    await msg.reply_text(
        "🏆 *Топ-5 за всё время*\n"
        f"{fmt(top_total)}\n\n"
        "📅 *Топ-5 за месяц*\n"
        f"{fmt(top_month)}",
        parse_mode='Markdown'
    )


async def karmaclean_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        await update.effective_message.reply_text("❌ Только для администраторов чата.")
        return
    msg = update.effective_message
    if msg is None or msg.reply_to_message is None:
        await msg.reply_text("↩️ Ответьте этой командой на сообщение, реакции которого нужно удалить.")
        return
    chat_id = msg.chat.id
    message_id = msg.reply_to_message.message_id
    n = db_delete_reactions_for_message(chat_id, message_id)
    await msg.reply_text(f"🧹 Удалено реакций: {n} (сообщение {message_id}).")


async def karmapurge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        await update.effective_message.reply_text("❌ Только для администраторов чата.")
        return
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Использование: `/karmapurge <дни>`\n`0` — очистить всё.\nНапример: `/karmapurge 180`", parse_mode='Markdown')
        return
    try:
        days = int(context.args[0])
        if days < 0:
            raise ValueError
    except ValueError:
        await msg.reply_text("❌ Нужно целое число дней ≥ 0 (0 = очистить всё).")
        return
    n_reactions, n_messages = db_delete_reactions_older_than(days)
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
        "/talk – случайная фраза\n"
        "/pair – случайные пары из слов\n"
        "/karma – показать СВОЮ карму\n"
        "/karma @username – карма указанного пользователя\n"
        "/top – топ-5 за месяц и за всё время\n"
        "/version – версия бота\n"
        "А ещё я проверяю новых участников и удаляю их, если они не нажимают кнопку подтверждения в течение 1 минуты.\n"
        "И считаю карму по реакциям (👍 +1, 👎 −1 и т.п.)."
    )

async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"🤖 Версия бота: {BOT_VERSION}\n"
    )

async def talk_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        bot_username = context.bot.username
        text = update.effective_message.text or ''
        is_mentioned = f"@{bot_username}" in text
        reply_to_bot = (
            update.effective_message.reply_to_message
            and update.effective_message.reply_to_message.from_user
            and update.effective_message.reply_to_message.from_user.id == context.bot.id
        )
        if not is_mentioned and not reply_to_bot:
            return
    user = update.effective_user
    phrase = random.choice(PHRASES)
    await update.message.reply_text(f"{get_user_mention(user)}, {phrase}")

async def talk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    phrase = random.choice(PHRASES)
    await update.message.reply_text(f"{get_user_mention(user)}, {phrase}")

async def pair_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👥 Введите имена (или слова) через пробел.\n"
        "Например: Ашот Вазген Сигизмунд Гимнокалициум\n\n"
        "Или отправьте /cancel, чтобы отменить."
    )
    return WAITING_FOR_NAMES

async def pair_receive_names(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != 'private':
        bot_username = context.bot.username
        text = update.effective_message.text or ''
        is_mentioned = f"@{bot_username}" in text
        reply_to_bot = (
            update.effective_message.reply_to_message
            and update.effective_message.reply_to_message.from_user
            and update.effective_message.reply_to_message.from_user.id == context.bot.id
        )
        if not is_mentioned and not reply_to_bot:
            return WAITING_FOR_NAMES

    names = update.message.text.split()
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

    answer = "🎲 *Случайные пары:*\n" + "\n".join(pairs)
    await update.message.reply_text(answer, parse_mode='Markdown')
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

    pending_jobs.pop((chat_id, user_id), None)

    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
        logger.info(f"Удалён пользователь {user_name} (@{user_username}) [ID: {user_id}] из чата {chat_id}")
    except Exception as e:
        logger.error(f"Не удалось забанить/разбанить пользователя {user_id} в чате {chat_id}: {e}")

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
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    key = (chat_id, user_id)

    job = pending_jobs.get(key)
    if job is None:
        await query.answer("⏳ Время истекло или вы уже подтверждены.", show_alert=False)
        return

    job.remove()
    pending_jobs.pop(key, None)

    user = query.from_user
    user_name = user.full_name or user.first_name or "No name"
    user_username = user.username or "no_username"
    logger.info(f"Пользователь {user_name} (@{user_username}) [ID: {user_id}] подтверждён в чате {chat_id} (кнопка)")

    await query.edit_message_text("✅ Вы подтверждены! Добро пожаловать в чат.")

async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return

    now_utc = datetime.now(timezone.utc)
    msg_time = update.effective_message.date
    if msg_time < now_utc - timedelta(seconds=MAX_MESSAGE_AGE_SECONDS):
        logger.info(f"Пропускаем устаревшее сообщение о новом участнике от {msg_time}")
        return

    chat_id = update.message.chat_id

    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    except Exception as e:
        logger.error(f"Не удалось получить информацию о боте в чате {chat_id}: {e}")
        return

    if bot_member.status not in ("creator", "administrator") or not bot_member.can_restrict_members:
        logger.warning(f"Бот не имеет прав на удаление участников в чате {chat_id}")
        return

    for new_member in update.message.new_chat_members:
        if new_member.id == context.bot.id:
            continue

        user_id = new_member.id
        key = (chat_id, user_id)

        if key in pending_jobs:
            old_job = pending_jobs.pop(key)
            if old_job:
                old_job.remove()

        user_full_name = new_member.full_name or new_member.first_name or "No name"
        user_username = new_member.username or "no_username"

        keyboard = [[InlineKeyboardButton("✅ Я человек", callback_data="confirm")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            sent_msg = await update.message.reply_text(
                f"Привет, {new_member.mention_html()}! Нажми кнопку ниже, чтобы подтвердить, что ты человек.\n"
                "Если не нажмёшь в течение 1 минуты, я удалю тебя из чата.",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
            continue

        if context.job_queue is None:
            logger.error("JobQueue не инициализирован")
            return

        job = context.job_queue.run_once(
            remove_user,
            when=60,
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

        logger.info(f"Новый пользователь {user_full_name} (@{user_username}) [ID: {user_id}] зашёл в чат {chat_id}, отправлена кнопка подтверждения.")

# ----- post_init: установка команд в меню Telegram -----
async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Запустить бота"),
            BotCommand("help", "Список возможностей"),
            BotCommand("talk", "Случайная фраза"),
            BotCommand("pair", "Случайные пары из слов"),
            BotCommand("karma", "Показать свою карму"),
            BotCommand("top", "Топ-5 за месяц и за всё время"),
            BotCommand("version", "Версия бота"),
        ])
        logger.info("Команды бота обновлены через set_my_commands")
    except Exception as e:
        logger.error(f"Не удалось установить команды: {e}")

# ----- Главная -----
def main():
    init_db()

    application = (
        Application.builder()
        .token(TOKEN)
        .base_url("https://tg-api-proxy.ashot-seruli.workers.dev/bot")
        .base_file_url("https://tg-api-proxy.ashot-seruli.workers.dev/file/bot")
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .post_init(post_init)
        .build()
    )

    if application.job_queue is None:
        job_queue = JobQueue()
        job_queue.set_application(application)
        application.job_queue = job_queue
        job_queue.start()

    if application.job_queue is None:
        logger.critical("JobQueue не инициализирован")
        return

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
    application.add_handler(CommandHandler("talk", talk_command))
    application.add_handler(CommandHandler("version", version_command))
    application.add_handler(CommandHandler("karma", karma_command))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CommandHandler("karmaclean", karmaclean_command))
    application.add_handler(CommandHandler("karmapurge", karmapurge_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk_response))

    logger.info("Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
