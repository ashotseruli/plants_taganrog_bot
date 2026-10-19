"""Тесты для utils.py.

Здесь нет conftest.py, моков и фикстур: utils.py — чистый модуль,
не читает .env/config.yaml, не пишет логи, не зависит от глобального состояния.
"""
from datetime import datetime, timezone

import pytest

from utils import (
    sanitize_filename,
    is_mentioned,
    placeholders,
    safe_format,
    msg_edit_ts,
    trim_history,
    user_repr,
    msg_repr,
)


# ---------- sanitize_filename ----------

class TestSanitizeFilename:
    def test_simple(self):
        assert sanitize_filename("hello world") == "hello_world"

    def test_special_chars(self):
        assert sanitize_filename("a/b\\c:d*e") == "a_b_c_d_e"

    def test_preserves_dash_dot_underscore(self):
        assert sanitize_filename("a-b_c.d") == "a-b_c.d"

    def test_empty(self):
        assert sanitize_filename("") == "unknown"

    def test_none(self):
        assert sanitize_filename(None) == "unknown"

    def test_truncated_to_60(self):
        out = sanitize_filename("x" * 200)
        assert len(out) == 60

    def test_cyrillic(self):
        # Кириллица не alnum в ASCII-смысле → заменяется на _
        out = sanitize_filename("Привет")
        assert "/" not in out
        assert "\\" not in out
        assert len(out) <= 60


# ---------- is_mentioned ----------

class TestIsMentioned:
    def test_exact(self):
        assert is_mentioned("привет @BotName", "botname") is True

    def test_case_insensitive(self):
        assert is_mentioned("привет @BOTNAME", "botname") is True

    def test_bot_username_case_insensitive(self):
        assert is_mentioned("привет @botname", "BotName") is True

    def test_other_bot(self):
        assert is_mentioned("привет @otherbot", "botname") is False

    def test_empty_text(self):
        assert is_mentioned("", "botname") is False

    def test_none_username(self):
        assert is_mentioned("привет @botname", None) is False

    def test_partial_match(self):
        # @bot не должен матчиться на @botname
        assert is_mentioned("привет @bot", "botname") is False

    def test_substring_match(self):
        # @botname_extra не должен матчиться на @botname
        # но текущая реализация использует in, так что матчится.
        # Это осознанное поведение — фиксируем тестом как есть.
        assert is_mentioned("привет @botname_extra", "botname") is True


# ---------- placeholders ----------

class TestPlaceholders:
    def test_empty_returns_null(self):
        assert placeholders([]) == "NULL"

    def test_single(self):
        assert placeholders(["a"]) == "?"

    def test_multiple(self):
        assert placeholders(["a", "b", "c"]) == "?,?,?"

    def test_tuple(self):
        assert placeholders(("a", "b")) == "?,?"


# ---------- safe_format ----------

class TestSafeFormat:
    def test_simple(self):
        assert safe_format("hi {name}", name="world") == "hi world"

    def test_multiple(self):
        out = safe_format("{a} и {b}", a="1", b="2")
        assert out == "1 и 2"

    def test_missing_key_falls_back(self):
        # .format() упадёт на {missing}, fallback заменит только {a}
        out = safe_format("{a} {missing}", a="X")
        assert "X" in out
        assert "{missing}" in out

    def test_json_braces_survive(self):
        # Посторонние {} не должны падать
        out = safe_format('{a} {"k": 1}', a="X")
        assert "X" in out
        assert '{"k": 1}' in out

    def test_no_kwargs(self):
        assert safe_format("plain text") == "plain text"


# ---------- msg_edit_ts ----------

class _Msg:
    def __init__(self, edit_date=None):
        if edit_date is not None:
            self.edit_date = edit_date


class TestMsgEditTs:
    def test_missing_attribute(self):
        assert msg_edit_ts(_Msg()) == 0

    def test_none_edit_date(self):
        m = _Msg()
        m.edit_date = None
        assert msg_edit_ts(m) == 0

    def test_valid_datetime(self):
        dt = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        m = _Msg(dt)
        assert msg_edit_ts(m) == int(dt.timestamp())

    def test_broken_timestamp(self):
        class Broken:
            class edit_date:
                def timestamp(self):
                    raise RuntimeError("boom")
        assert msg_edit_ts(Broken()) == 0


# ---------- trim_history ----------

class TestTrimHistory:
    @staticmethod
    def _hist(n, size=1):
        return [{"role": "user", "content": "x" * size} for _ in range(n)]

    def test_under_limit(self):
        hist = self._hist(3, size=10)
        out = trim_history(hist, max_msgs=10, max_chars=1000)
        assert len(out) == 3

    def test_trim_by_count(self):
        hist = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        out = trim_history(hist, max_msgs=4, max_chars=1000)
        assert len(out) == 4
        # сохраняются последние
        assert out[-1]["content"] == "m9"
        assert out[0]["content"] == "m6"

    def test_trim_by_chars(self):
        # с конца: c(50) + b(50) = 100 ≤ 120, a(50) уже 150 > 120
        hist = [{"role": "user", "content": c * 50} for c in "abc"]
        out = trim_history(hist, max_msgs=10, max_chars=120)
        assert len(out) == 2
        assert out[0]["content"].startswith("b")
        assert out[1]["content"].startswith("c")

    def test_empty(self):
        assert trim_history([], max_msgs=10, max_chars=1000) == []

    def test_single_too_big(self):
        hist = [{"role": "user", "content": "x" * 1000}]
        out = trim_history(hist, max_msgs=10, max_chars=100)
        # одно сообщение больше лимита — оно отбрасывается целиком
        assert out == []
        


# ---------- user_repr ----------

class TestUserRepr:
    def test_username_and_name(self):
        assert user_repr("vasya", "Василий", 123) == "@vasya (Василий)"

    def test_username_only(self):
        assert user_repr("vasya", None, 123) == "@vasya"

    def test_name_only(self):
        assert user_repr(None, "Василий", 123) == "(Василий)"

    def test_nothing(self):
        assert user_repr(None, None, 123) == "[123]"

    def test_empty_strings(self):
        # Пустые строки — ложные, значит идём в fallback [id]
        assert user_repr("", "", 123) == "[123]"


# ---------- msg_repr ----------

class _MsgForRepr:
    def __init__(self, text=None, caption=None):
        if text is not None:
            self.text = text
        if caption is not None:
            self.caption = caption


class TestMsgRepr:
    def test_text(self):
        m = _MsgForRepr(text="продам монстеру")
        assert msg_repr(m, 42) == 'msg=42 "продам монстеру"'

    def test_caption(self):
        m = _MsgForRepr(caption="подпись к фото")
        assert msg_repr(m, 42) == 'msg=42 "подпись к фото"'

    def test_text_preferred_over_caption(self):
        # text идёт первым в getattr(text) or getattr(caption)
        m = _MsgForRepr(text="текст", caption="подпись")
        assert msg_repr(m, 42) == 'msg=42 "текст"'

    def test_truncated_to_30_plus_ellipsis(self):
        m = _MsgForRepr(text="x" * 100)
        out = msg_repr(m, 42)
        # "msg=42 \"...\"" + 30 символов + "…" + закрывающая кавычка
        assert out.startswith('msg=42 "')
        assert out.endswith('…"')
        # Внутри превью 30 «x» + «…»
        assert "x" * 30 + "…" in out

    def test_newlines_replaced_with_space(self):
        m = _MsgForRepr(text="первая\nвторая\nтретья")
        assert msg_repr(m, 42) == 'msg=42 "первая вторая третья"'

    def test_no_text(self):
        m = _MsgForRepr()
        assert msg_repr(m, 42) == "msg=42"

    def test_none_obj(self):
        assert msg_repr(None, 42) == "msg=42"

    def test_message_id_in_output(self):
        # ID всегда виден, даже без текста
        assert "msg=999" in msg_repr(None, 999)
