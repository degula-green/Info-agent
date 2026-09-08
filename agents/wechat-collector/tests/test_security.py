import io
import tempfile
import unittest
from pathlib import Path

from app.security import LocalDatabaseError, content_hash, validate_database_path


class SecurityTests(unittest.TestCase):
    def test_database_path_validation_returns_fingerprint_only(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            account = root / "wxid_a_1234"
            (account / "db_storage").mkdir(parents=True)
            resolved_root, selected, fingerprint = validate_database_path(str(root), "wxid_a")
            self.assertEqual(resolved_root, root.resolve())
            self.assertEqual(selected, account.name)
            self.assertEqual(len(fingerprint), 64)
            self.assertNotIn(str(root), fingerprint)

    def test_database_path_requires_unique_matching_account(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("wxid_a_1234", "wxid_b_5678"):
                (root / name / "db_storage").mkdir(parents=True)
            with self.assertRaises(LocalDatabaseError):
                validate_database_path(str(root), "wxid_missing")
            with self.assertRaises(LocalDatabaseError):
                validate_database_path("relative/path", "wxid_a")

    def test_content_hash_streams_chunks(self):
        digest, size = content_hash(io.BytesIO(b"abc" * 100))
        self.assertEqual(size, 300)
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
