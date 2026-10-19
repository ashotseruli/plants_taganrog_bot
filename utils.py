"""Чистые утилиты без побочных эффектов.

Здесь только функции, которые:
- не читают .env, config.yaml, БД
- не пишут логи
- не зависят от глобального состояния app.py

Это делает их легко тестируемыми без моков.
"""

def sanitize_filename(name: str) -> str:
    if not name:
        return "unknown"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return safe[:60]


def is_mentioned(text: str, bot_username: "str | None") -> bool:
    if not bot_username or not text:
        return False
    return f"@{bot_username.lower()}" in text.lower()


def placeholders(seq) -> str:
    """? для SQL IN (...). Пустой seq → литерал NULL."""
    if not seq:
        return "NULL"
    return ",".join("?" * len(seq))


def safe_format(template: str, **kwargs) -> str:
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        result = template
        for k, v in kwargs.items():
            result = result.replace("{" + k + "}", str(v))
        return result


def msg_edit_ts(msg) -> int:
    ed = getattr(msg, "edit_date", None)
    if ed is None:
        return 0
    try:
        return int(ed.timestamp())
    except Exception:
        return 0


def trim_history(hist, max_msgs, max_chars):
    if len(hist) > max_msgs:
        hist = hist[-max_msgs:]
    total = 0
    result = []
    for item in reversed(hist):
        c = len(item.get("content", ""))
        if total + c > max_chars:
            break
        total += c
        result.append(item)
    return list(reversed(result))
    
def user_repr(username, first_name, user_id):
    parts = []
    if username:
        parts.append(f"@{username}")
    if first_name:
        parts.append(f"({first_name})")
    if parts:
        return " ".join(parts)
    return f"[{user_id}]"


def msg_repr(msg_obj, message_id):
    text = None
    if msg_obj is not None:
        text = getattr(msg_obj, "text", None) or getattr(msg_obj, "caption", None)
    if text:
        preview = text.replace("\n", " ").strip()
        if len(preview) > 30:
            preview = preview[:30] + "…"
        return f'msg={message_id} "{preview}"'
    return f"msg={message_id}"    
