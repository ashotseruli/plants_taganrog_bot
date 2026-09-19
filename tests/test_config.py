"""Тесты для app._validate_config.

Проверяет, что конфиг валидируется корректно — ловит как «плохие» конфиги,
так и «хорошие». Импорт app требует conftest.py: он подсовывает валидный
config.yaml и BOT_TOKEN до импорта.
"""
import copy

import pytest


def _base_cfg():
    """Минимальный валидный конфиг, от которого удобно отталкиваться."""
    return {
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


class TestValid:
    def test_passes(self):
        import app
        app._validate_config(_base_cfg())

    def test_user_cooldown_optional(self):
        import app
        cfg = _base_cfg()
        cfg["llm"]["user_cooldown"] = 1.5
        app._validate_config(cfg)

    def test_empty_negative_ok(self):
        import app
        cfg = _base_cfg()
        cfg["reactions"]["negative"] = []
        app._validate_config(cfg)

    def test_empty_positive_ok(self):
        import app
        cfg = _base_cfg()
        cfg["reactions"]["positive"] = []
        app._validate_config(cfg)


class TestRoot:
    def test_not_a_dict(self):
        import app
        with pytest.raises(ValueError, match="корень"):
            app._validate_config([])

    def test_missing_section(self):
        import app
        cfg = _base_cfg()
        del cfg["llm"]
        with pytest.raises(ValueError, match="отсутствует секция"):
            app._validate_config(cfg)


class TestPhrases:
    def test_empty(self):
        import app
        cfg = _base_cfg()
        cfg["phrases"] = []
        with pytest.raises(ValueError, match="phrases"):
            app._validate_config(cfg)

    def test_not_list(self):
        import app
        cfg = _base_cfg()
        cfg["phrases"] = "string"
        with pytest.raises(ValueError, match="phrases"):
            app._validate_config(cfg)

    def test_non_str_element(self):
        import app
        cfg = _base_cfg()
        cfg["phrases"] = ["ok", 42]
        with pytest.raises(ValueError, match="строками"):
            app._validate_config(cfg)


class TestReactions:
    def test_both_empty(self):
        import app
        cfg = _base_cfg()
        cfg["reactions"] = {"positive": [], "negative": []}
        with pytest.raises(ValueError, match="реакция"):
            app._validate_config(cfg)

    def test_not_dict(self):
        import app
        cfg = _base_cfg()
        cfg["reactions"] = []
        with pytest.raises(ValueError, match="reactions"):
            app._validate_config(cfg)


class TestPrompts:
    def test_missing_system(self):
        import app
        cfg = _base_cfg()
        del cfg["prompts"]["system"]
        with pytest.raises(ValueError, match="system"):
            app._validate_config(cfg)

    def test_empty_system(self):
        import app
        cfg = _base_cfg()
        cfg["prompts"]["system"] = "   "
        with pytest.raises(ValueError, match="system"):
            app._validate_config(cfg)

    def test_not_str(self):
        import app
        cfg = _base_cfg()
        cfg["prompts"]["system"] = 42
        with pytest.raises(ValueError, match="system"):
            app._validate_config(cfg)


class TestLlm:
    def test_missing_key(self):
        import app
        cfg = _base_cfg()
        del cfg["llm"]["temperature"]
        with pytest.raises(ValueError, match="temperature"):
            app._validate_config(cfg)

    def test_wrong_type(self):
        import app
        cfg = _base_cfg()
        cfg["llm"]["temperature"] = "hot"
        with pytest.raises(ValueError, match="temperature"):
            app._validate_config(cfg)

    def test_wrong_type_int_ok_for_float_key(self):
        # int принимается там, где ожидается число
        import app
        cfg = _base_cfg()
        cfg["llm"]["temperature"] = 1
        app._validate_config(cfg)

    def test_bad_user_cooldown(self):
        import app
        cfg = _base_cfg()
        cfg["llm"]["user_cooldown"] = "fast"
        with pytest.raises(ValueError, match="user_cooldown"):
            app._validate_config(cfg)


class TestNewMember:
    def test_missing_kick(self):
        import app
        cfg = _base_cfg()
        del cfg["new_member"]["kick_after_seconds"]
        with pytest.raises(ValueError, match="kick_after_seconds"):
            app._validate_config(cfg)

    def test_negative_kick(self):
        import app
        cfg = _base_cfg()
        cfg["new_member"]["kick_after_seconds"] = -1
        with pytest.raises(ValueError):
            app._validate_config(cfg)

    def test_zero_kick(self):
        import app
        cfg = _base_cfg()
        cfg["new_member"]["kick_after_seconds"] = 0
        with pytest.raises(ValueError, match="> 0"):
            app._validate_config(cfg)

    def test_negative_age(self):
        import app
        cfg = _base_cfg()
        cfg["new_member"]["max_message_age_seconds"] = -1
        with pytest.raises(ValueError):
            app._validate_config(cfg)
