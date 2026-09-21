"""Transport-слой для Ollama: HTTP-клиент + один метод post_chat.

Модуль не знает ничего про промпты, историю, конфиг. Только отправка
готового списка messages в /api/chat и распаковка ответа.

URL и модель читаются из env. Общий httpx-клиент переиспользует соединения.
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")

# Модель задаётся из app.py через set_model() — чтобы можно было менять
# через /reload без рестарта службы.
_model: str = ""

DEFAULT_HARD_TIMEOUT = 90

_http_client: "httpx.AsyncClient | None" = None
_hard_timeout: float = DEFAULT_HARD_TIMEOUT

def set_model(name: str) -> None:
    """Устанавливает имя модели Ollama. Вызывается из app.py при старте
    и в /reload."""
    global _model
    _model = name
    
def set_hard_timeout(seconds: float) -> None:
    """Устанавливает read-таймаут для запросов к Ollama.

    Вызывается из app.py при старте и в /reload. Если текущий клиент уже
    создан — он будет закрыт при следующем запросе и создан заново с новым
    таймаутом (это ок, операция редкая)."""
    global _hard_timeout
    _hard_timeout = float(seconds)


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0, read=_hard_timeout, write=10.0, pool=5.0
            )
        )
    return _http_client


async def close_http_client():
    global _http_client
    if _http_client is not None:
        try:
            await _http_client.aclose()
        except Exception as e:
            logger.warning(f"Не удалось закрыть httpx-клиент: {e}")
        _http_client = None


async def post_chat(messages: list, options: dict) -> str:
    """Отправляет готовый список messages в Ollama /api/chat.

    Возвращает content ответа ассистента, strip-нутый. Пустая строка, если
    модель ответила без content.
    """
    payload = {
        "model": _model,
        "messages": messages,
        "stream": False,
        "options": options,
    }
    client = await _get_http_client()
    r = await client.post(OLLAMA_URL, json=payload)
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "").strip()
