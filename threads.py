"""Управление историей диалогов и дедупликацией апдейтов.

Здесь всё, что связано с thread-based историей:
- какие сообщения уже видели (дедуп)
- к какому thread относится сообщение
- какие сообщения этому thread принадлежат
- локи для сериализации LLM-ответов внутри треда

Глобальное состояние (OrderedDict'ы) живёт в этом модуле.
Наружу торчат только функции — чтобы app.py не лез в словари напрямую.
"""
import asyncio
import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)

# --- Дедупликация апдейтов (LRU через OrderedDict) ---
# Ключ: (chat_id, message_id, edit_date_ts). Для обычных сообщений edit_date=0.
# Редактирование меняет edit_date и, соответственно, ключ — edit с новым
# упоминанием бота проходит дедуп, а повторный апдейт того же edit'а — нет.
_seen_messages = OrderedDict()
_SEEN_LIMIT = 5000


def is_duplicate(chat_id: int, message_id: int, edit_date_ts: int = 0) -> bool:
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
_history_locks: "OrderedDict[tuple[int, int], asyncio.Lock]" = OrderedDict()
_HISTORY_LOCKS_LIMIT = 2000


def get_thread_lock(chat_id: int, thread_id: int) -> asyncio.Lock:
    """Возвращает (создаёт при необходимости) asyncio.Lock для пары (chat, thread).
    При переполнении вытесняются только свободные локи — захваченные не трогаем,
    чтобы не отобрать лок у работающей корутины."""
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
                logger.warning(
                    "get_thread_lock: все %d локов заняты, словарь растёт "
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


def resolve_thread_key(chat, msg):
    """Возвращает thread_id для сообщения.
    В личке thread_id == chat.id. В группе — либо thread родителя (если это reply),
    либо собственный message_id (новый thread)."""
    if chat.type == "private":
        return chat.id
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None:
        parent_thread = _msg_to_thread.get((chat.id, reply.message_id))
        if parent_thread is not None:
            return parent_thread
    return msg.message_id


def remember_message(chat_id, message_id, thread_id):
    key = (chat_id, message_id)
    _msg_to_thread[key] = thread_id
    _msg_to_thread.move_to_end(key)
    if len(_msg_to_thread) > _MSG_TO_THREAD_LIMIT:
        _msg_to_thread.popitem(last=False)


def get_history(chat_id, thread_id):
    key = (chat_id, thread_id)
    if key not in _chat_histories:
        _chat_histories[key] = []
        if len(_chat_histories) > _CHAT_HISTORIES_LIMIT:
            old_chat, old_thread = _chat_histories.popitem(last=False)[0]
            _drop_thread_mappings(old_chat, old_thread)
    else:
        _chat_histories.move_to_end(key)
    return _chat_histories[key]


def clear_thread(chat_id, thread_id):
    """Убирает историю, связанные записи в маппинге и lock для треда."""
    _chat_histories.pop((chat_id, thread_id), None)
    _drop_thread_mappings(chat_id, thread_id)
    lock = _history_locks.get((chat_id, thread_id))
    if lock is not None and not lock.locked():
        _history_locks.pop((chat_id, thread_id), None)


def clear_all_histories():
    """Полный сброс историй и маппинга. Используется в /reload."""
    global _chat_histories
    _chat_histories = OrderedDict()
    _msg_to_thread.clear()
    # Свободные локи тоже не нужны — они были привязаны к старым историям.
    for k in list(_history_locks.keys()):
        if not _history_locks[k].locked():
            _history_locks.pop(k, None)


def clear_all_locks():
    """Полный сброс локов. Используется в post_shutdown."""
    _history_locks.clear()
