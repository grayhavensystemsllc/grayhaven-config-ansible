"""Verified backup sets use invented data, real SQLite copies and private paths."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from tests.helpers import load_program

backup = load_program("fixture_mcp_backup", "roles/restic_backup/files/grayhaven-mcp-backup")


class MCPBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(mode=0o700)
        self.ledger = self.root / "sessions" / "builder.db"
        self.ledger.parent.mkdir()

    def ledger_database(self, *, rows=1):
        connection = sqlite3.connect(self.ledger)
        connection.execute("CREATE TABLE locations (kind TEXT, item_id TEXT, payload TEXT, PRIMARY KEY(kind,item_id))")
        connection.executemany("INSERT INTO locations VALUES (?,?,?)", [
            ("fixture", f"row-{index}", "opaque-fixture" * 300) for index in range(rows)
        ])
        connection.commit()
        return connection

    @staticmethod
    def producer(destination):
        destination.write_bytes(b"opaque-core-fixture")

    def create(self):
        return backup.create(self.artifacts, self.ledger, producer=self.producer)

    def test_complete_set_preserves_opaque_data_and_private_permissions(self):
        source = self.ledger_database()
        self.addCleanup(source.close)
        old = self.artifacts / "mcp-older.sqlite3"
        old.write_bytes(b"older-fixture")
        result = self.create()
        self.assertEqual(json.loads((result / "manifest.json").read_text()), {
            "format": 1, "databases": ["state.sqlite3", "hierarchy.sqlite3"],
        })
        self.assertEqual((result / "state.sqlite3").read_bytes(), b"opaque-core-fixture")
        with sqlite3.connect(result / "hierarchy.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT * FROM locations").fetchall(),
                             source.execute("SELECT * FROM locations").fetchall())
            backup.verify_ledger(connection)
        self.assertEqual(result.stat().st_mode & 0o777, 0o700)
        for path in result.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result.stat().st_uid, self.artifacts.stat().st_uid)
        copied = (result / "hierarchy.sqlite3").stat()
        self.assertEqual((copied.st_uid, copied.st_gid),
                         (self.ledger.stat().st_uid, self.ledger.stat().st_gid))
        self.assertEqual(old.read_bytes(), b"older-fixture")
        self.assertFalse(any(path.name.startswith(".") for path in self.artifacts.iterdir()))

    def test_ledger_owner_is_preserved_and_ownership_failure_keeps_the_previous_set(self):
        self.ledger_database().close()
        original_stat = Path.stat
        owners = []

        def source_owner(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == self.ledger:
                values = list(result)
                values[4:6] = [42631, 42641]
                return os.stat_result(values)
            return result

        def preserve_owner(descriptor, uid, gid):
            copied = os.fstat(descriptor)
            self.assertGreater(copied.st_size, 0)
            self.assertEqual(copied.st_mode & 0o777, 0o600)
            owners.append((uid, gid))

        with (mock.patch.object(Path, "stat", source_owner),
              mock.patch.object(backup.os, "fchown", side_effect=preserve_owner)):
            published = self.create()
        self.assertEqual(owners, [(42631, 42641)])
        with (mock.patch.object(backup.os, "fchown", side_effect=PermissionError("fixture ownership failure")),
              self.assertRaises(PermissionError)):
            self.create()
        self.assertEqual(list(self.artifacts.iterdir()), [published])
        with sqlite3.connect(published / "hierarchy.sqlite3") as connection:
            backup.verify_ledger(connection)

    def test_unused_ledger_can_be_absent_without_creating_its_directory(self):
        absent = self.root / "unused" / "ledger.db"
        result = backup.create(self.artifacts, absent, producer=self.producer)
        self.assertEqual(json.loads((result / "manifest.json").read_text())["databases"], ["state.sqlite3"])
        self.assertFalse(absent.parent.exists())

    def test_a_new_ledger_or_existing_publication_stops_without_overwriting(self):
        def appeared(_source, _destination):
            self.ledger_database().close()
            return False

        with mock.patch.object(backup, "snapshot_ledger", side_effect=appeared), self.assertRaises(backup.BackupError):
            self.create()
        self.assertTrue(self.ledger.exists())
        self.assertEqual(list(self.artifacts.iterdir()), [])
        original = backup.tempfile.mkdtemp
        preserved = []
        for redirect in (False, True):
            def collision(*args, **kwargs):
                pending = Path(original(*args, **kwargs))
                published = pending.parent / pending.name.removeprefix(".").removesuffix(".pending")
                if redirect:
                    published.symlink_to(self.root / "missing-publication")
                else:
                    published.mkdir()
                    (published / "preserve").write_bytes(b"fixture-material")
                preserved.append(published)
                return str(pending)

            with self.subTest(redirect=redirect), mock.patch.object(backup.tempfile, "mkdtemp", side_effect=collision), self.assertRaises(backup.BackupError):
                self.create()
            self.assertEqual(set(self.artifacts.iterdir()), set(preserved))
        self.assertEqual((preserved[0] / "preserve").read_bytes(), b"fixture-material")
        self.assertTrue(preserved[1].is_symlink())

    def test_producer_failure_missing_empty_link_or_extra_output_never_publishes(self):
        external = self.root / "preserve"
        external.write_bytes(b"preserve-fixture")

        def fail(destination):
            destination.write_bytes(b"incomplete-fixture")
            raise RuntimeError("Fixture exporter failed")

        for producer in (
            fail, lambda _path: None, lambda path: path.touch(),
            lambda path: path.symlink_to(external),
            lambda path: (self.producer(path), (path.parent / "unexpected").touch()),
        ):
            with self.subTest(producer=producer):
                with self.assertRaises(Exception):
                    backup.create(self.artifacts, self.ledger, producer=producer)
                self.assertEqual(list(self.artifacts.iterdir()), [])
                self.assertEqual(external.read_bytes(), b"preserve-fixture")

    def test_existing_invalid_or_unreadable_ledger_stops_publication(self):
        for content in (b"", b"not a database"):
            self.ledger.write_bytes(content)
            with self.subTest(size=len(content)), self.assertRaises(Exception):
                self.create()
            self.assertEqual(list(self.artifacts.iterdir()), [])
        self.ledger.unlink()
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("CREATE TABLE unrelated (id TEXT)")
        with self.assertRaises(backup.BackupError):
            self.create()
        self.assertEqual(list(self.artifacts.iterdir()), [])
        with mock.patch.object(backup.sqlite3, "connect", side_effect=PermissionError), self.assertRaises(PermissionError):
            self.create()
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_source_and_destination_redirects_and_existing_files_are_preserved(self):
        source = self.ledger_database()
        source.close()
        redirect = self.root / "redirect"
        redirect.symlink_to(self.ledger.parent, target_is_directory=True)
        file_link = self.root / "file-link"
        file_link.symlink_to(self.ledger)
        for ledger in (file_link, redirect / self.ledger.name, self.ledger.parent):
            with self.subTest(path=ledger.name), self.assertRaises(backup.BackupError):
                backup.create(self.artifacts, ledger, producer=self.producer)
            self.assertEqual(list(self.artifacts.iterdir()), [])
        target_link = self.root / "artifact-link"
        target_link.symlink_to(self.artifacts, target_is_directory=True)
        with self.assertRaises(backup.BackupError):
            backup.create(target_link, self.ledger, producer=self.producer)
        destination = self.root / "existing"
        destination.write_bytes(b"preserve-fixture")
        with self.assertRaises(FileExistsError):
            backup.snapshot_ledger(self.ledger, destination)
        self.assertEqual(destination.read_bytes(), b"preserve-fixture")
        for destination in (self.ledger, self.root / "copy-wal", redirect / "copy"):
            with self.subTest(destination=destination.name), self.assertRaises(backup.BackupError):
                backup.snapshot_ledger(self.ledger, destination)
        with self.assertRaises(backup.BackupError):
            backup.create(self.root / "missing", self.ledger, producer=self.producer)
        with self.assertRaises(backup.BackupError):
            backup.prune(Path("/"), 4)

    def test_snapshot_timeout_integrity_and_disk_failures_remove_partial_copies(self):
        connection = self.ledger_database()
        connection.close()
        destination = self.root / "snapshot"
        with self.assertRaises(backup.BackupError):
            backup.snapshot_ledger(self.ledger, destination, 0)
        with mock.patch.object(backup.time, "monotonic", side_effect=[0, 61]), self.assertRaises(Exception):
            backup.snapshot_ledger(self.ledger, destination)
        self.assertFalse(destination.exists())
        with mock.patch.object(backup, "verify_ledger", side_effect=[None, backup.BackupError("Fixture verification failed")]), self.assertRaises(backup.BackupError):
            backup.snapshot_ledger(self.ledger, destination)
        self.assertFalse(destination.exists())
        with mock.patch.object(backup.os, "fsync", side_effect=OSError), self.assertRaises(OSError):
            backup.snapshot_ledger(self.ledger, destination)
        self.assertFalse(destination.exists())
        connection = mock.Mock()
        connection.execute.return_value.fetchall.return_value = [("fixture damage",)]
        with self.assertRaises(backup.BackupError):
            backup.verify_ledger(connection)

    def test_first_reader_open_failure_removes_the_exclusively_created_destination(self):
        connection = self.ledger_database()
        connection.close()
        destination = self.root / "reader-failure"
        connect = sqlite3.connect

        def unavailable(_uri, **_options):
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.stat().st_size, 0)
            raise sqlite3.OperationalError("Fixture reader could not open")

        with mock.patch.object(backup.sqlite3, "connect", side_effect=unavailable) as opened:
            with self.assertRaises(sqlite3.OperationalError):
                backup.snapshot_ledger(self.ledger, destination)
        self.assertEqual(opened.call_count, 1)
        self.assertFalse(destination.exists())
        with connect(self.ledger) as source:
            self.assertEqual(source.execute("SELECT count(*) FROM locations").fetchone(), (1,))

    def test_committed_wal_and_a_concurrent_transaction_are_copied_consistently(self):
        source = self.ledger_database(rows=600)
        self.addCleanup(source.close)
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("UPDATE locations SET payload='opaque-wal-fixture' WHERE item_id='row-0'")
        source.commit()
        self.assertTrue(Path(str(self.ledger) + "-wal").exists())
        changed, trigger = threading.Event(), threading.Event()
        errors = []
        connect = sqlite3.connect

        def writer():
            try:
                if not trigger.wait(5):
                    raise RuntimeError("Fixture backup did not request a concurrent write")
                with connect(self.ledger) as connection:
                    connection.execute("UPDATE locations SET payload='opaque-concurrent-fixture' WHERE item_id IN ('row-1','row-2')")
            except Exception as error:
                errors.append(error)
            finally:
                changed.set()

        class Observed(sqlite3.Connection):
            def backup(self, target, **options):
                progress = options.pop("progress")

                def observe(status, remaining, total):
                    if remaining and not trigger.is_set():
                        trigger.set()
                        if not changed.wait(5):
                            raise RuntimeError("Fixture writer did not finish")
                    progress(status, remaining, total)

                return super().backup(target, progress=observe, **options)

        worker = threading.Thread(target=writer)
        worker.start()
        try:
            with mock.patch.object(backup.sqlite3, "connect", side_effect=lambda *args, **kwargs: connect(*args, factory=Observed, **kwargs)):
                result = self.create()
        finally:
            trigger.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertTrue(changed.is_set())
        with connect(result / "hierarchy.sqlite3") as connection:
            rows = connection.execute("SELECT item_id,payload FROM locations WHERE item_id IN ('row-0','row-1','row-2') ORDER BY item_id").fetchall()
            self.assertEqual(rows, [("row-0", "opaque-wal-fixture"), ("row-1", "opaque-concurrent-fixture"), ("row-2", "opaque-concurrent-fixture")])

    def test_fsync_owner_and_publication_failures_do_not_leave_pending_sets(self):
        for target in ("chown", "fsync"):
            with self.subTest(operation=target), mock.patch.object(backup.os, target, side_effect=OSError), self.assertRaises(OSError):
                self.create()
            self.assertEqual(list(self.artifacts.iterdir()), [])
        for target in ("sync_directory", "rename"):
            patcher = (mock.patch.object(backup, target, side_effect=OSError)
                       if target == "sync_directory" else mock.patch.object(Path, target, side_effect=OSError))
            with self.subTest(operation=target), patcher, self.assertRaises(OSError):
                self.create()
            self.assertEqual(list(self.artifacts.iterdir()), [])
        sync = backup.sync_directory
        with mock.patch.object(backup, "sync_directory", side_effect=lambda path: (_ for _ in ()).throw(OSError()) if path == self.artifacts else sync(path)), self.assertRaises(OSError):
            self.create()
        published = list(self.artifacts.iterdir())
        self.assertEqual(len(published), 1)
        backup.verify_bundle(published[0])

    def test_expiry_prunes_old_sets_and_legacy_files_but_preserves_other_material(self):
        old = self.create()
        recent = self.create()
        legacy = self.artifacts / "mcp-fixture.sqlite3"
        legacy.write_bytes(b"opaque-legacy")
        unrelated = self.artifacts / "unrelated"
        unrelated.mkdir()
        pending = self.artifacts / ".mcp-fixture.pending"
        pending.mkdir()
        redirect = self.artifacts / "mcp-redirect.sqlite3"
        redirect.symlink_to(self.ledger)
        now = 5_000_000
        boundary = now - 4 * 86400
        for path in (old, legacy, unrelated, pending):
            os.utime(path, (boundary, boundary))
        os.utime(recent, (boundary + 1, boundary + 1))
        backup.prune(self.artifacts, 3, now=lambda: now)
        self.assertFalse(old.exists())
        self.assertFalse(legacy.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(pending.exists())
        self.assertTrue(redirect.is_symlink())

    def test_bad_manifests_redirects_or_unexpected_children_prevent_pruning(self):
        result = self.create()
        manifest = result / "manifest.json"
        original = manifest.read_text()
        for value in ([], {}, {"format": True, "databases": []}, {"format": 2, "databases": []},
                      {"format": 1, "databases": "state.sqlite3"},
                      {"format": 1, "databases": [4]},
                      {"format": 1, "databases": ["state.sqlite3", "state.sqlite3"]},
                      {"format": 1, "databases": ["outside"]}):
            manifest.write_text(json.dumps(value))
            with self.subTest(manifest=value), self.assertRaises(backup.BackupError):
                backup.prune(self.artifacts, 1, now=lambda: result.stat().st_mtime + 3 * 86400)
            self.assertTrue(result.exists())
        manifest.write_text(original)
        unexpected = result / "preserve"
        unexpected.touch()
        with self.assertRaises(backup.BackupError):
            backup.verify_bundle(result)
        unexpected.unlink()
        core = result / "state.sqlite3"
        core.unlink()
        core.symlink_to(self.ledger)
        with self.assertRaises(backup.BackupError):
            backup.verify_bundle(result)

    def test_prune_errors_and_invalid_retention_are_reported(self):
        result = self.create()
        now = result.stat().st_mtime + 3 * 86400
        for days in (0, -1, True, "2"):
            with self.subTest(days=days), self.assertRaises(backup.BackupError):
                backup.prune(self.artifacts, days)
        with mock.patch.object(backup.shutil, "rmtree", side_effect=PermissionError), self.assertRaises(PermissionError):
            backup.prune(self.artifacts, 1, now=lambda: now)
        self.assertTrue(result.exists())
        legacy = self.artifacts / "mcp-old-fixture.sqlite3"
        legacy.write_bytes(b"older-fixture")
        os.utime(legacy, (0, 0))
        with mock.patch.object(Path, "unlink", side_effect=PermissionError), self.assertRaises(PermissionError):
            backup.prune(self.artifacts, 1)
        self.assertTrue(legacy.exists())
        with mock.patch.object(backup, "sync_directory", side_effect=OSError), self.assertRaises(OSError):
            backup.prune(self.artifacts, 1)
        self.assertTrue(result.exists())

    def test_cleanup_failure_leaves_only_an_unpublished_set(self):
        def fail(_destination):
            raise RuntimeError("Fixture exporter failed")

        with mock.patch.object(backup.shutil, "rmtree", side_effect=PermissionError), self.assertRaises(PermissionError):
            backup.create(self.artifacts, self.ledger, producer=fail)
        remaining = list(self.artifacts.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].name.startswith("."))
        self.assertTrue(remaining[0].name.endswith(".pending"))

    def test_cli_and_original_core_export_contract_use_one_verified_set(self):
        def provider(argv, **options):
            self.assertEqual(argv[:6], ["podman", "exec", "grayhaven-mcp", "python", "-m", "connector.backup"])
            self.assertTrue(options["check"])
            self.assertTrue(options["capture_output"])
            self.producer(Path(argv[-1]))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(backup.subprocess, "run", side_effect=provider) as run:
            self.assertEqual(backup.main(["create", str(self.artifacts), str(self.ledger)]), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(backup.main(["prune", str(self.artifacts), "3"]), 0)
        errors = io.StringIO()
        with mock.patch.object(backup, "create", side_effect=RuntimeError("fixture internal detail")), contextlib.redirect_stderr(errors):
            self.assertEqual(backup.main(["create", str(self.artifacts), str(self.ledger)]), 1)
        self.assertNotIn("fixture internal detail", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
