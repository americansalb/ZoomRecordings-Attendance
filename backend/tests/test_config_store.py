"""
Tests for where publish settings are stored.

The Postgres tests matter most: that database is SHARED with other services, so
these assert the store is incapable of touching anything but its own table.
They run against a fake connection, so no database is needed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import Any, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import config_store  # noqa: E402
from services.config_store import (  # noqa: E402
    CachingStore,
    FileConfigStore,
    PostgresConfigStore,
)


# --------------------------------------------------------------------------
# a fake psycopg2 connection that records every statement
# --------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, log: List[str], rows: List[Any]):
        self._log = log
        self._rows = rows
        self._last = None

    def execute(self, sql, params=None):
        self._log.append(" ".join(sql.split()))
        self._last = sql

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConnection:
    def __init__(self, log: List[str], rows: List[Any]):
        self._log = log
        self._rows = rows
        self.commits = 0

    def cursor(self):
        return FakeCursor(self._log, self._rows)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def store_with(rows=None):
    """A Postgres store wired to a fake connection, plus its statement log."""
    log: List[str] = []
    store = PostgresConfigStore("postgres://user:pw@host:5432/db")
    store._connect = lambda: FakeConnection(log, list(rows or []))   # type: ignore
    return store, log


class TestPostgresIsSafeInASharedDatabase(unittest.TestCase):
    """The database belongs to other services too. Stay in our lane."""

    FORBIDDEN = ["DROP", "TRUNCATE", "ALTER", "DELETE", "GRANT", "REVOKE"]

    def test_never_issues_a_destructive_statement(self):
        store, log = store_with(rows=[])
        store.read()
        store.write({"classes": {}})
        joined = " ".join(log).upper()
        for word in self.FORBIDDEN:
            self.assertNotIn(word, joined, f"{word} must never be issued")

    def test_only_ever_touches_its_own_table(self):
        store, log = store_with(rows=[])
        store.read()
        store.write({"classes": {}})
        for statement in log:
            self.assertIn(
                "publish_settings", statement,
                f"statement touches something else: {statement}",
            )

    def test_table_creation_is_if_not_exists(self):
        store, log = store_with(rows=[])
        store.read()
        create = next(s for s in log if "CREATE TABLE" in s.upper())
        self.assertIn("IF NOT EXISTS", create.upper())

    def test_write_is_an_upsert_not_a_wipe_and_insert(self):
        store, log = store_with(rows=[])
        store.write({"classes": {}})
        insert = next(s for s in log if s.upper().startswith("INSERT"))
        self.assertIn("ON CONFLICT", insert.upper())
        self.assertNotIn("DELETE", " ".join(log).upper())

    def test_table_is_created_once_per_process_not_per_call(self):
        store, log = store_with(rows=[None, None, None])
        store.read()
        store.read()
        store.read()
        creates = [s for s in log if "CREATE TABLE" in s.upper()]
        self.assertEqual(len(creates), 1, "should not re-run DDL on every read")


class TestPostgresBehaviour(unittest.TestCase):
    def test_render_style_url_is_normalised(self):
        s = PostgresConfigStore("postgres://u:p@h/db")
        self.assertTrue(s.url.startswith("postgresql://"))

    def test_already_normal_url_is_left_alone(self):
        s = PostgresConfigStore("postgresql://u:p@h/db")
        self.assertEqual(s.url, "postgresql://u:p@h/db")

    def test_reads_back_a_dict(self):
        store, _ = store_with(rows=[({"classroom_subject": "t@aalb.org"},)])
        self.assertEqual(store.read()["classroom_subject"], "t@aalb.org")

    def test_reads_back_json_text_too(self):
        store, _ = store_with(rows=[(json.dumps({"classroom_subject": "t@aalb.org"}),)])
        self.assertEqual(store.read()["classroom_subject"], "t@aalb.org")

    def test_empty_table_returns_none_not_an_error(self):
        store, _ = store_with(rows=[])
        self.assertIsNone(store.read())

    def test_a_broken_database_degrades_instead_of_raising(self):
        store = PostgresConfigStore("postgresql://u:p@h/db")

        def boom():
            raise OSError("connection refused")

        store._connect = boom                       # type: ignore
        self.assertIsNone(store.read())             # no exception
        self.assertFalse(store.write({"a": 1}))     # reports failure


class TestFileStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = FileConfigStore(os.path.join(self.dir, "settings.json"))

    def test_round_trip(self):
        self.assertTrue(self.store.write({"classes": {"139": {"code": "139"}}}))
        self.assertEqual(self.store.read()["classes"]["139"]["code"], "139")

    def test_missing_file_is_none(self):
        self.assertIsNone(self.store.read())

    def test_corrupt_file_is_none_not_a_crash(self):
        with open(self.store.path, "w") as f:
            f.write("{not json")
        self.assertIsNone(self.store.read())

    def test_reports_itself_as_not_durable(self):
        self.assertFalse(self.store.describe()["durable"])


class TestCaching(unittest.TestCase):
    class Counting(FileConfigStore):
        def __init__(self, path):
            super().__init__(path)
            self.reads = 0

        def read(self):
            self.reads += 1
            return super().read()

    def test_repeated_reads_hit_the_backend_once(self):
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        inner = self.Counting(path)
        inner.write({"a": 1})
        cached = CachingStore(inner)
        cached.read()
        cached.read()
        cached.read()
        self.assertEqual(inner.reads, 1)

    def test_a_write_updates_the_cache_immediately(self):
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        cached = CachingStore(self.Counting(path))
        cached.write({"a": 2})
        self.assertEqual(cached.read()["a"], 2)


class TestBackendSelection(unittest.TestCase):
    def setUp(self):
        config_store.reset_store()
        self._env = dict(os.environ)
        for key in ("DATABASE_URL", "REDIS_URL", "GOOGLE_CLIENT_EMAIL",
                    "PUBLISH_CONFIG_STORE", "GOOGLE_SERVICE_ACCOUNT_FILE"):
            os.environ.pop(key, None)
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = "/definitely/not/here.json"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        config_store.reset_store()

    def test_database_url_wins(self):
        os.environ["DATABASE_URL"] = "postgres://u:p@h/db"
        os.environ["REDIS_URL"] = "redis://localhost:6379"
        self.assertEqual(config_store.get_config_store().name, "postgres")

    def test_falls_back_to_file_with_nothing_configured(self):
        os.environ["PUBLISH_CONFIG_PATH"] = os.path.join(tempfile.mkdtemp(), "s.json")
        self.assertEqual(config_store.get_config_store().name, "file")

    def test_explicit_choice_is_respected(self):
        os.environ["DATABASE_URL"] = "postgres://u:p@h/db"
        os.environ["PUBLISH_CONFIG_STORE"] = "file"
        os.environ["PUBLISH_CONFIG_PATH"] = os.path.join(tempfile.mkdtemp(), "s.json")
        self.assertEqual(config_store.get_config_store().name, "file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
