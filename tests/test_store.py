"""Tests for dupefinder.store."""

import os
import sqlite3
import unittest

from dupefinder.store import SCHEMA_VERSION, HashRow, Store
from tests.helpers import TempTreeCase


class SchemaTests(TempTreeCase):
    def test_fresh_db_has_expected_schema_and_wal_enabled(self) -> None:
        db_path = os.path.join(self.root, "hashes.db")
        store = Store(db_path)
        try:
            conn = sqlite3.connect(db_path)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertEqual(tables, {"file_hashes", "settings", "schema_meta"})
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            version = conn.execute("SELECT version FROM schema_meta").fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION)
            conn.close()
        finally:
            store.close()


class RoundTripTests(TempTreeCase):
    def test_stores_and_reads_back_a_digest(self) -> None:
        db_path = os.path.join(self.root, "hashes.db")
        store = Store(db_path)
        try:
            store.put_hash(
                HashRow(path="/a/b.txt", size=5, mtime_ns=100, partial=None, full="deadbeef")
            )
            store.flush()
            row = store.get_hash("/a/b.txt", size=5, mtime_ns=100)
            self.assertIsNotNone(row)
            self.assertEqual(row.full, "deadbeef")
        finally:
            store.close()


class StalenessTests(TempTreeCase):
    def test_same_size_different_mtime_is_treated_as_a_miss(self) -> None:
        # This is the case (path, size) alone would get wrong: content can
        # change while size stays identical, so mtime_ns must also match.
        db_path = os.path.join(self.root, "hashes.db")
        store = Store(db_path)
        try:
            store.put_hash(
                HashRow(path="/a/b.txt", size=5, mtime_ns=100, partial=None, full="oldhash")
            )
            store.flush()
            self.assertIsNone(store.get_hash("/a/b.txt", size=5, mtime_ns=200))
        finally:
            store.close()

    def test_mtime_change_alone_invalidates_the_entry(self) -> None:
        db_path = os.path.join(self.root, "hashes.db")
        store = Store(db_path)
        try:
            store.put_hash(
                HashRow(path="/a/b.txt", size=5, mtime_ns=100, partial="p", full="f")
            )
            store.flush()
            self.assertIsNone(store.get_hash("/a/b.txt", size=5, mtime_ns=999))
        finally:
            store.close()


class FlushOnCloseTests(TempTreeCase):
    def test_pending_writes_flush_on_close_and_a_reopened_store_sees_them(self) -> None:
        # A high threshold means nothing flushes automatically; only close()
        # persists it -- this is the resumability guarantee.
        db_path = os.path.join(self.root, "hashes.db")
        store = Store(db_path, flush_rows=200, flush_seconds=999)
        store.put_hash(
            HashRow(path="/a/b.txt", size=5, mtime_ns=100, partial=None, full="deadbeef")
        )
        store.close()

        reopened = Store(db_path)
        try:
            row = reopened.get_hash("/a/b.txt", size=5, mtime_ns=100)
            self.assertIsNotNone(row)
            self.assertEqual(row.full, "deadbeef")
        finally:
            reopened.close()


class SchemaVersionTests(TempTreeCase):
    def test_unknown_future_version_disables_the_cache_instead_of_raising(self) -> None:
        db_path = os.path.join(self.root, "hashes.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION + 1,))
        conn.execute(
            "CREATE TABLE file_hashes (path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
            "mtime_ns INTEGER NOT NULL, partial TEXT, sampled TEXT, full TEXT, "
            "last_seen INTEGER NOT NULL)"
        )
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        conn.close()

        store = Store(db_path)  # must not raise
        try:
            store.put_hash(HashRow(path="/x", size=1, mtime_ns=1, partial=None, full="h"))
            store.flush()
            self.assertIsNone(store.get_hash("/x", size=1, mtime_ns=1))
        finally:
            store.close()


class SettingsTests(TempTreeCase):
    def test_settings_round_trip_including_non_ascii_paths(self) -> None:
        db_path = os.path.join(self.root, "hashes.db")
        store = Store(db_path)
        try:
            value = {"a": "/mnt/d/Fotos año 2020", "b": "/home/user/café"}
            store.set_setting("last_paths", value)
            self.assertEqual(store.get_setting("last_paths"), value)
            self.assertIsNone(store.get_setting("missing_key"))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
