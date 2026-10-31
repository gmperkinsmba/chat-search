import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_security as security


class RuntimeSecurityTests(unittest.TestCase):
    def test_directory_and_database_are_hardened_before_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runtime"
            root.mkdir(mode=0o777)
            os.chmod(root, 0o755)
            security.ensure_private_directory(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            database = root / "app.sqlite3"
            database.touch(mode=0o644)
            os.chmod(database, 0o644)
            security.prepare_database(database)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE sample (value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('private')")
            security.verify_database(database)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(database) + suffix)
                self.assertTrue(sidecar.exists())
                self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)
            connection.close()

    def test_symlink_directory_and_database_targets_are_never_chmodded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.mkdir(mode=0o755)
            os.chmod(outside, 0o755)
            link_dir = root / "runtime-link"
            link_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(security.StorageSecurityError):
                security.ensure_private_directory(link_dir)
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

            target = root / "outside.sqlite3"
            target.write_text("keep")
            os.chmod(target, 0o644)
            link_db = root / "runtime" / "app.sqlite3"
            link_db.parent.mkdir(mode=0o700)
            link_db.symlink_to(target)
            with self.assertRaises(security.StorageSecurityError):
                security.prepare_database(link_db)
            self.assertEqual(target.read_text(), "keep")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_existing_symlink_sidecars_are_rejected_without_touching_target(self):
        for suffix in ("-journal", "-wal", "-shm"):
            for check in (security.prepare_database, security.verify_database):
                with self.subTest(suffix=suffix, check=check.__name__), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    database = root / "app.sqlite3"
                    if check is security.verify_database:
                        security.prepare_database(database)
                    target = root / "outside-sidecar"
                    target.write_text("keep")
                    os.chmod(target, 0o644)
                    Path(str(database) + suffix).symlink_to(target)
                    with self.assertRaises(security.StorageSecurityError):
                        check(database)
                    self.assertEqual(target.read_text(), "keep")
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_hardlinked_database_is_rejected_without_chmodding_shared_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "outside.sqlite3"
            target.write_text("keep")
            os.chmod(target, 0o644)
            database = root / "app.sqlite3"
            os.link(target, database)
            with self.assertRaises(security.StorageSecurityError):
                security.prepare_database(database)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_permission_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runtime"
            with patch.object(security.os, "fchmod", side_effect=OSError("denied")):
                with self.assertRaises(security.StorageSecurityError):
                    security.ensure_private_directory(root)

    def test_nonowned_runtime_directory_is_rejected_before_chmod(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "runtime"
            root.mkdir(mode=0o755)
            os.chmod(root, 0o755)
            with patch.object(security.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(security.StorageSecurityError):
                    security.ensure_private_directory(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
