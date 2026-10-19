"""Тесты для db_* функций из db.py.

Используют фикстуру temp_db (conftest.py), которая подменяет db.DB_PATH.
"""
import time
import db

import pytest


# ---------- users ----------

class TestUpsertUser:
    def test_insert_new(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        row = db.db_find_user_by_username("alice")
        assert row is not None
        assert row[0] == 1
        assert row[1] == "Alice"

    def test_update_existing(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_upsert_user(1, "alice_new", "Alice2")
        row = db.db_find_user_by_username("alice_new")
        assert row is not None
        assert row[0] == 1

    def test_username_unique_case_insensitive(self, temp_db):
        db.db_upsert_user(1, "Alice", "A")
        db.db_upsert_user(2, "alice", "B")
        # у первого username должен был занулиться
        row = db.db_find_user_by_username("alice")
        assert row[0] == 2

    def test_find_case_insensitive(self, temp_db):
        db.db_upsert_user(1, "Alice", "A")
        assert db.db_find_user_by_username("ALICE")[0] == 1

    def test_find_missing(self, temp_db):
        assert db.db_find_user_by_username("nonexistent") is None


# ---------- messages ----------

class TestMessages:
    def test_save_and_get(self, temp_db):
        db.db_save_message(-100, 10, 1, 1000)
        assert db.db_get_message_author(-100, 10) == 1

    def test_get_missing(self, temp_db):
        assert db.db_get_message_author(-100, 999) is None

    def test_save_replaces_author(self, temp_db):
        db.db_save_message(-100, 10, 1, 1000)
        db.db_save_message(-100, 10, 2, 1001)
        assert db.db_get_message_author(-100, 10) == 2


# ---------- reactions ----------

class TestReactions:
    def test_add_positive(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_replace_reactions(-100, 10, 2, set(), {"👍"}, 1001)
        assert db.db_get_karma(1) == 1

    def test_add_negative(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_replace_reactions(-100, 10, 2, set(), {"👎"}, 1001)
        assert db.db_get_karma(1) == -1

    def test_remove_reaction(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_replace_reactions(-100, 10, 2, set(), {"👍"}, 1001)
        db.db_replace_reactions(-100, 10, 2, {"👍"}, set(), 1002)
        assert db.db_get_karma(1) == 0

    def test_delete_for_message(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_replace_reactions(-100, 10, 2, set(), {"👍", "👎"}, 1001)
        n = db.db_delete_reactions_for_message(-100, 10)
        assert n == 2
        assert db.db_get_karma(1) == 0

    def test_delete_older_than(self, temp_db):
        now = int(time.time())
        old = now - 100 * 24 * 3600
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, old)
        db.db_save_message(-100, 11, 1, now)
        db.db_replace_reactions(-100, 10, 2, set(), {"👍"}, old)
        db.db_replace_reactions(-100, 11, 2, set(), {"👍"}, now)
        n_reactions, n_messages = db.db_delete_reactions_older_than(30)
        assert n_reactions == 1
        assert n_messages == 1

    def test_purge_all(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_replace_reactions(-100, 10, 2, set(), {"👍"}, 1001)
        n_reactions, n_messages = db.db_delete_reactions_older_than(0)
        assert n_reactions == 1
        assert n_messages == 1


# ---------- karma ----------

class TestKarma:
    def test_positive(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_replace_reactions(-100, 10, 2, set(), {"👍"}, 1001)
        assert db.db_get_karma(1) == 1

    def test_negative(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_replace_reactions(-100, 10, 2, set(), {"👎"}, 1001)
        assert db.db_get_karma(1) == -1

    def test_mixed(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_replace_reactions(-100, 10, 2, set(), {"👍"}, 1001)
        db.db_replace_reactions(-100, 10, 3, set(), {"👍"}, 1002)
        db.db_replace_reactions(-100, 10, 4, set(), {"👎"}, 1003)
        assert db.db_get_karma(1) == 1

    def test_zero(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        assert db.db_get_karma(1) == 0

    def test_unknown_user(self, temp_db):
        assert db.db_get_karma(999) == 0

    def test_period_filter(self, temp_db):
        now = int(time.time())
        old = now - 100 * 24 * 3600
        db.db_upsert_user(1, "alice", "Alice")
        db.db_save_message(-100, 10, 1, old)
        db.db_save_message(-100, 11, 1, now)
        db.db_replace_reactions(-100, 10, 2, set(), {"👍"}, old)
        db.db_replace_reactions(-100, 11, 2, set(), {"👍"}, now)
        assert db.db_get_karma(1) == 2
        month_ago = now - 30 * 24 * 3600
        assert db.db_get_karma(1, since=month_ago) == 1


# ---------- rank ----------

class TestRank:
    def test_top_rank(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_upsert_user(2, "bob", "Bob")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_save_message(-100, 11, 2, 1001)
        db.db_replace_reactions(-100, 10, 3, set(), {"👍"}, 1010)
        db.db_replace_reactions(-100, 10, 4, set(), {"👍"}, 1011)
        db.db_replace_reactions(-100, 11, 3, set(), {"👍"}, 1012)
        assert db.db_get_rank(1) == 1
        assert db.db_get_rank(2) == 2

    def test_zero_karma(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        assert db.db_get_rank(1) == 1


# ---------- top ----------

class TestTop:
    def test_sorted(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        db.db_upsert_user(2, "bob", "Bob")
        db.db_save_message(-100, 10, 1, 1000)
        db.db_save_message(-100, 11, 2, 1001)
        db.db_replace_reactions(-100, 10, 3, set(), {"👍"}, 1010)
        db.db_replace_reactions(-100, 10, 4, set(), {"👍"}, 1011)
        db.db_replace_reactions(-100, 11, 3, set(), {"👎"}, 1012)
        top = db.db_get_top(5)
        assert len(top) == 2
        assert top[0][0] == 1
        assert top[0][3] == 2
        assert top[1][0] == 2
        assert top[1][3] == -1

    def test_limit(self, temp_db):
        for i in range(1, 8):
            db.db_upsert_user(i, f"u{i}", f"U{i}")
            db.db_save_message(-100, 10 + i, i, 1000 + i)
            db.db_replace_reactions(-100, 10 + i, 99, set(), {"👍"}, 2000 + i)
        top = db.db_get_top(5)
        assert len(top) == 5

    def test_empty(self, temp_db):
        assert db.db_get_top(5) == []
        
class TestGetUser:
    def test_existing(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        row = db.db_get_user(1)
        assert row is not None
        assert row == (1, "alice", "Alice")

    def test_missing(self, temp_db):
        assert db.db_get_user(999) is None

    def test_order_is_id_username_firstname(self, temp_db):
        db.db_upsert_user(1, "alice", "Alice")
        uid, username, first_name = db.db_get_user(1)
        assert uid == 1
        assert username == "alice"
        assert first_name == "Alice"        
