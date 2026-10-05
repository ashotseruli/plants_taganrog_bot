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
import time
import asyncio

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

# Важно: .env должен быть загружен ДО импорта наших модулей,
# потому что db.py / config.py / llm.py читают env на этапе import.
load_dotenv()

import db
import config
import threads
import llm
from utils import (
    sanitize_filename,
    is_mentioned,
    safe_format,
    msg_edit_ts,
    trim_history,
)

# --- Пути (не зависим от CWD) ---
BASE_DIR = Path(__file__).resolve().parent
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

# httpx пишет INFO на каждый HTTP-запрос — это шум в system.log.
# Ошибки (WARNING+) остаются видны.
logging.getLogger("httpx").setLevel(logging.WARNING)

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
# (chat_id, user_id) -> {"sent_at": monotonic}
pending_verify: dict = {}
WAITING_FOR_NAMES = 1
BOT_VERSION = "2.8.0"

# Окно trap: если callback пришёл быстрее — считаем ботом.
TRAP_WINDOW_SECONDS = 1.0

# --- Rate-limit на LLM: (chat_id, user_id) -> monotonic ts последнего запроса ---
_llm_last_call: dict[tuple[int, int], float] = {}

TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("Токен не найден!")

try:
    CFG = config.load_config()
    config.validate_config(CFG)
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


db.POSITIVE_REACTIONS = set(CFG["reactions"]["positive"])
db.NEGATIVE_REACTIONS = set(CFG["reactions"]["negative"])

PHRASES = CFG["phrases"]

# --- LLM ---
logger.info("LLM: model=%s, url=%s", CFG["llm"]["model"], llm.OLLAMA_URL)
llm.set_model(CFG["llm"]["model"])

LLM_THINKING_DELAY = CFG["llm"]["thinking_delay"]
LLM_HARD_TIMEOUT = CFG["llm"]["hard_timeout"]
llm.set_hard_timeout(LLM_HARD_TIMEOUT)
LLM_MAX_WORDS = CFG["llm"]["max_words"]
LLM_MAX_INPUT_CHARS = CFG["llm"]["max_input_chars"]

THINKING_PLACEHOLDERS = CFG["placeholders"]

LLM_PROMPT_STATIC = safe_format(CFG["prompts"]["system"], max_words=LLM_MAX_WORDS)


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


def extract_emojis(reactions) -> set:
    """Из списка ReactionType возвращает set строк-идентификаторов реакций.

    Кастомные эмодзи маркируются префиксом 'custom:<id>'.
    В POSITIVE_REACTIONS / NEGATIVE_REACTIONS такие строки не задаются —
    кастомные реакции по умолчанию игнорируются."""
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
        await db.db_upsert_user_a(user.id, user.username or "", user.first_name or "")
        await db.db_save_message_a(msg.chat.id, msg.message_id, user.id, ts)
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

    author_id = await db.db_get_message_author_a(chat_id, message_id)
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
        await db.db_replace_reactions_a(
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
            row = await db.db_find_user_by_username_a(raw)
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
                    await db.db_upsert_user_a(u.id, u.username or "", u.first_name or "")
                except Exception as e:
                    logger.error(f"karma: не удалось обновить users для {u.id}: {e}")
                break
            if ent.type == "mention":
                raw = _entity_text(msg, ent).lstrip("@").strip()
                if raw:
                    row = await db.db_find_user_by_username_a(raw)
                    if row:
                        target_id = row[0]
                        target_name = row[1] or row[2] or str(row[0])
                        source = "mention"
                        break
                    mention_not_found = True
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

    total_karma = await db.db_get_karma_a(target_id)
    month_karma = await db.db_get_karma_a(target_id, month_ago)
    total_rank = await db.db_get_rank_a(target_id, since=0, karma_user=total_karma)
    month_rank = await db.db_get_rank_a(target_id, since=month_ago, karma_user=month_karma)

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

    top_total = await db.db_get_top_a(5, 0)
    top_month = await db.db_get_top_a(5, month_ago)

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
    n = await db.db_delete_reactions_for_message_a(chat_id, message_id)
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

    if days == 0 and (len(context.args) < 2 or context.args[1].lower() != "confirm"):
        await msg.reply_text(
            "⚠️ <code>/karmapurge 0</code> сотрёт <b>всю</b> историю кармы и сообщений.\n"
            "Если уверены — повторите: <code>/karmapurge 0 confirm</code>",
            parse_mode='HTML'
        )
        return

    n_reactions, n_messages = await db.db_delete_reactions_older_than_a(days)
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


# =========================================================
#                  ВЕРИФИКАЦИЯ НОВЫХ УЧАСТНИКОВ
# =========================================================

def _verify_keyboard() -> InlineKeyboardMarkup:
    """Одна большая кнопка «Подтвердить». В ряду она одна — занимает всю ширину."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data="verify:ok")
    ]])


def _verify_text(mention_html: str, kick_human: str,
                 first_time: bool = True) -> str:
    if first_time:
        head = f"Привет, {mention_html}!"
    else:
        head = "⚠️ <b>Не та кнопка!</b>"
    return (
        f"{head} Нажми большую кнопку «✅ Подтвердить», чтобы доказать, "
        f"что ты человек.\n"
        f"Если не нажмёшь в течение {kick_human}, я удалю тебя из чата."
    )


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.message is None:
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    key = (chat_id, user_id)

    if query.data != "verify:ok":
        await query.answer()
        return

    # Нет активной верификации (истекла или уже подтверждён)
    if key not in pending_jobs or key not in pending_verify:
        await query.answer("⏳ Время истекло или вы уже подтверждены.",
                           show_alert=True)
        return

    info = pending_verify[key]
    elapsed = time.monotonic() - info.get("sent_at", 0.0)

    # --- Trap: слишком быстро для человека ---
    if elapsed < TRAP_WINDOW_SECONDS:
        pending_verify.pop(key, None)
        job = pending_jobs.pop(key, None)
        if job is not None:
            try:
                job.schedule_removal()
            except Exception:
                pass

        logger.info(
            "verify trap: chat=%s user=%s elapsed=%.3fs — кик",
            chat_id, user_id, elapsed,
        )

        try:
            await query.answer("❌ Слишком быстро. Похоже, ты бот.",
                               show_alert=True)
        except Exception:
            pass

        try:
            await query.edit_message_text("❌ Похоже, ты бот. Удалён.")
        except Exception as e:
            logger.error(f"trap edit failed: {e}")

        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)
            logger.info(f"trap: забанен и разбанен {user_id} в чате {chat_id}")
        except Exception as e:
            logger.error(f"trap ban/unban failed: {e}")
        return

    # --- Успех ---
    pending_verify.pop(key, None)
    job = pending_jobs.pop(key, None)
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass

    await query.answer("✅ Подтверждено")

    user = query.from_user
    user_name = user.full_name or user.first_name or "No name"
    user_username = user.username or "no_username"
    logger.info(
        "Пользователь %s (@%s) [ID: %s] подтверждён в чате %s (за %.2fs)",
        user_name, user_username, user_id, chat_id, elapsed,
    )

    try:
        await query.edit_message_text("✅ Вы подтверждены! Добро пожаловать в чат.")
    except Exception as e:
        logger.error(f"verify_callback edit failed: {e}")


async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return

    chat_id = update.message.chat_id

    now_utc = datetime.now(timezone.utc)
    msg_time = update.effective_message.date
    if msg_time < now_utc - timedelta(seconds=MAX_MESSAGE_AGE_SECONDS):
        logger.info(f"Пропускаем устаревшее сообщение о новом участнике от {msg_time}")
        return

    if context.job_queue is None:
        logger.error("JobQueue не инициализирован — пропускаем обработку нового участника")
        return

    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    except Exception as e:
        logger.error(f"Не удалось получить информацию о боте в чате {chat_id}: {e}")
        return

    can_restrict = getattr(bot_member, "can_restrict_members", False)
    if bot_member.status not in ("creator", "administrator") or not can_restrict:
        logger.warning(f"Бот не имеет прав на удаление участников в чате {chat_id}")
        return

    if threads.is_duplicate(chat_id, update.message.message_id, 0):
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
                    old_job.schedule_removal()
                except Exception:
                    pass

        user_full_name = new_member.full_name or new_member.first_name or "No name"
        user_username = new_member.username or "no_username"

        try:
            sent_msg = await update.message.reply_text(
                _verify_text(new_member.mention_html(), kick_human),
                reply_markup=_verify_keyboard(),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
            continue

        # Запоминаем момент отправки для trap-проверки в callback.
        pending_verify[key] = {"sent_at": time.monotonic()}

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
    pending_verify.pop(key, None)

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


# =========================================================
#                       LLM
# =========================================================

async def _call_ollama_async(
    user_message: str,
    chat_id: "int | None" = None,
    thread_id: "int | None" = None,
) -> str:
    messages = [{"role": "system", "content": LLM_PROMPT_STATIC}]

    if chat_id is not None and thread_id is not None:
        hist = trim_history(
            threads.get_history(chat_id, thread_id),
            CFG["llm"]["history_max_messages"],
            CFG["llm"]["history_max_chars"],
        )
        messages.extend(hist)

    messages.append({"role": "user", "content": user_message[:LLM_MAX_INPUT_CHARS]})

    options = {
        "num_ctx": CFG["llm"]["num_ctx"],
        "num_predict": CFG["llm"]["num_predict"],
        "temperature": CFG["llm"]["temperature"],
        "top_p": CFG["llm"]["top_p"],
        "repeat_penalty": CFG["llm"]["repeat_penalty"],
    }
    return await llm.post_chat(messages, options)


async def send_smart_reply(message_obj, user_message: str,
                           chat_id: "int | None" = None,
                           chat_type: "str | None" = None,
                           chat_title: "str | None" = None,
                           thread_id: "int | None" = None,
                           user=None):
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
    # попадать в контекст.
    if (chat_id is not None and thread_id is not None
            and user_message.strip() and not used_fallback):
        hist = threads.get_history(chat_id, thread_id)
        hist.append({"role": "user", "content": user_message[:LLM_MAX_INPUT_CHARS]})
        hist.append({"role": "assistant", "content": reply_text})
        hist[:] = trim_history(
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
        threads.remember_message(chat_id, sent_msg.message_id, thread_id)


async def _handle_llm_turn(update, context, edit_ts=None):
    global _llm_last_call

    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    user_message = msg.text or msg.caption or ""

    if edit_ts is None:
        edit_ts = msg_edit_ts(msg)
    if threads.is_duplicate(chat.id, msg.message_id, edit_ts):
        logger.info("skip duplicate: chat=%s msg_id=%s edit=%s",
                    chat.id, msg.message_id, edit_ts)
        return

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

    thread_id = threads.resolve_thread_key(chat, msg)
    threads.remember_message(chat.id, msg.message_id, thread_id)

    try:
        chat_lg = get_chat_logger(chat.id, chat.type, user=user, chat_title=chat.title)
        mention = f"@{user.username}" if user.username else (user.full_name or "unknown")
        chat_lg.info(f"<- [{mention} id={user.id}]: {user_message[:1000]}")
    except Exception as e:
        logger.error(f"chat log write failed: {e}")

    lock = threads.get_thread_lock(chat.id, thread_id)

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
        if not mentioned and not reply_to_bot:
            return

    await _handle_llm_turn(update, context, edit_ts=edit_ts)


# =========================================================
#                       PAIR
# =========================================================

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


# =========================================================
#                  WARMUP / LIFECYCLE
# =========================================================

async def warmup_llm():
    logger.info("LLM warmup started...")
    try:
        result = await _call_ollama_async("привет")
        logger.info(f"LLM warmup done: {result[:60]!r}")
    except Exception as e:
        logger.warning(f"LLM warmup failed: {e}")


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
    if pending_jobs:
        logger.warning(
            "При завершении осталось %d необработанных задач на кик. "
            "Они не переживут рестарт — соответствующие пользователи "
            "останутся в чатах без подтверждения.",
            len(pending_jobs),
        )

    if _background_tasks:
        logger.info("Отменяю %d фоновых задач...", len(_background_tasks))
        for t in list(_background_tasks):
            t.cancel()
        await asyncio.gather(*_background_tasks, return_exceptions=True)
        _background_tasks.clear()
    threads.clear_all_locks()
    await llm.close_http_client()


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


# =========================================================
#                  СКРЫТЫЕ КОМАНДЫ
# =========================================================

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CFG, LLM_PROMPT_STATIC, PHRASES, THINKING_PLACEHOLDERS
    global LLM_THINKING_DELAY, LLM_HARD_TIMEOUT, LLM_MAX_WORDS, LLM_MAX_INPUT_CHARS
    global MAX_MESSAGE_AGE_SECONDS
    global LLM_USER_COOLDOWN

    user = update.effective_user
    if OWNER_ID is None or user.id != OWNER_ID:
        return

    try:
        new_cfg = config.load_config()
        config.validate_config(new_cfg)
        CFG = new_cfg

        threads.clear_all_histories()

        LLM_THINKING_DELAY = CFG["llm"]["thinking_delay"]
        LLM_HARD_TIMEOUT = CFG["llm"]["hard_timeout"]
        llm.set_hard_timeout(LLM_HARD_TIMEOUT)
        llm.set_model(CFG["llm"]["model"])
        LLM_MAX_WORDS = CFG["llm"]["max_words"]
        LLM_MAX_INPUT_CHARS = CFG["llm"]["max_input_chars"]
        LLM_USER_COOLDOWN = float(
            os.getenv("LLM_USER_COOLDOWN", CFG["llm"].get("user_cooldown", 2.0))
        )

        LLM_PROMPT_STATIC = safe_format(CFG["prompts"]["system"], max_words=LLM_MAX_WORDS)
        PHRASES = CFG["phrases"]
        THINKING_PLACEHOLDERS = CFG["placeholders"]
        db.POSITIVE_REACTIONS = set(CFG["reactions"]["positive"])
        db.NEGATIVE_REACTIONS = set(CFG["reactions"]["negative"])
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

    thread_id = threads.resolve_thread_key(chat, msg)
    threads.clear_thread(chat.id, thread_id)
    await msg.reply_text("🧹 История этой ветки очищена.")
    logger.info(f"thread cleared: chat={chat.id} thread={thread_id}")


# =========================================================
#                       MAIN
# =========================================================

def main():
    _force_ipv4()

    db.init_db()

    application = (
        Application.builder()
        .token(TOKEN)
        .base_url(os.getenv("TG_BASE_URL", "https://api.telegram.org/bot"))
        .base_file_url(os.getenv("TG_BASE_FILE_URL", "https://api.telegram.org/file/bot"))
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
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
    application.add_handler(
        CallbackQueryHandler(verify_callback, pattern=r"^verify:ok$")
    )

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
