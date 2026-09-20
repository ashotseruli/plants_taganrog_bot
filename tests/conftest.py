"""Настройка окружения для тестов.

Здесь мы ДО импорта app.py:
- подсовываем фиктивные BOT_TOKEN и OWNER_ID
- создаём временный config.yaml в tmp-папке
- говорим app.py читать конфиг оттуда

Это позволяет импортировать app.py без реального .env и config.yaml
и без записи в боевую karma.db.
"""
import os
import tempfile
from pathlib import Path

import pytest
import yaml

# --- Окружение до импорта app ---
_TMP = Path(tempfile.mkdtemp(prefix="tgbot_test_"))

os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN"
os.environ["OWNER_ID"] = "999999"
os.environ["CONFIG_PATH"] = str(_TMP / "config.yaml")

# --- Минимальный валидный config.yaml ---
_TEST_CFG = {
    "llm": {
        "thinking_delay": 5,
        "hard_timeout": 90,
        "max_words": 25,
        "max_input_chars": 500,
        "num_ctx": 2048,
        "num_predict": 60,
        "temperature": 0.85,
        "top_p": 0.9,
        "repeat_penalty": 1.1,
        "history_max_messages": 6,
        "history_max_chars": 1500,
        "user_cooldown": 1.0,
    },
    "prompts": {"system": "test {max_words}"},
    "phrases": ["p1", "p2"],
    "placeholders": ["хм..."],
    "reactions": {"positive": ["👍"], "negative": ["👎"]},
    "new_member": {
        "kick_after_seconds": 60,
        "max_message_age_seconds": 300,
    },
}
(_TMP / "config.yaml").write_text(
    yaml.safe_dump(_TEST_CFG, allow_unicode=True),
    encoding="utf-8",
)


# --- Фикстуры ---

@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """Изолированная БД для одного теста."""
    import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_karma.db"))
    monkeypatch.setattr(db, "POSITIVE_REACTIONS", {"👍"})
    monkeypatch.setattr(db, "NEGATIVE_REACTIONS", {"👎"})
    db.init_db()
    return db
