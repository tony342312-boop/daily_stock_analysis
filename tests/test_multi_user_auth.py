# -*- coding: utf-8 -*-
"""Multi-user auth, registration captcha, and history isolation tests."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.config import Config
from src.storage import DatabaseManager, AnalysisHistory


class MultiUserAuthStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        os.environ["DATABASE_PATH"] = str(self.data_dir / "test.db")
        os.environ["ADMIN_AUTH_ENABLED"] = "true"
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager()

    def tearDown(self):
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("ADMIN_AUTH_ENABLED", None)
        self.tmp.cleanup()

    def test_users_are_created_with_roles_and_password_rules(self):
        from src.auth import create_user, verify_user_password

        admin = create_user("tony", "87358289Heyyo", role="admin")
        user = create_user("alice", "abc1234567890xyz", role="user")

        self.assertEqual(admin.role, "admin")
        self.assertEqual(user.role, "user")
        self.assertTrue(verify_user_password("tony", "87358289Heyyo"))
        self.assertTrue(verify_user_password("alice", "abc1234567890xyz"))
        self.assertFalse(verify_user_password("alice", "1234567890123456"))
        with self.assertRaises(ValueError):
            create_user("numbers", "1234567890123456", role="user")
        with self.assertRaises(ValueError):
            create_user("toolong", "abc1234567890xyzz", role="user")

    def test_captcha_token_verification(self):
        from src.auth import create_registration_captcha, verify_registration_captcha

        challenge = create_registration_captcha()
        self.assertIn("question", challenge)
        self.assertIn("captchaToken", challenge)
        self.assertTrue(verify_registration_captcha(challenge["captchaToken"], str(challenge["answer"])))
        self.assertFalse(verify_registration_captcha(challenge["captchaToken"], "999"))

    def test_history_list_is_filtered_by_user_unless_admin(self):
        from src.auth import create_user

        alice = create_user("alice", "abc1234567890xyz", role="user")
        bob = create_user("bob", "def1234567890xyz", role="user")
        with self.db.get_session() as session:
            session.add_all([
                AnalysisHistory(query_id="q1", code="AAPL", name="Apple", user_id=alice.id, created_at=datetime.now()),
                AnalysisHistory(query_id="q2", code="NVDA", name="Nvidia", user_id=bob.id, created_at=datetime.now()),
            ])
            session.commit()

        records, total = self.db.get_analysis_history_paginated(user_id=alice.id, include_all_users=False)
        self.assertEqual(total, 1)
        self.assertEqual(records[0].query_id, "q1")

        records, total = self.db.get_analysis_history_paginated(user_id=alice.id, include_all_users=True)
        self.assertEqual(total, 2)

    def test_cleanup_deletes_records_older_than_retention(self):
        now = datetime.now()
        with self.db.get_session() as session:
            session.add_all([
                AnalysisHistory(query_id="old", code="AAPL", created_at=now - timedelta(days=20)),
                AnalysisHistory(query_id="new", code="NVDA", created_at=now - timedelta(days=3)),
            ])
            session.commit()

        deleted = self.db.delete_analysis_history_older_than(days=14)
        self.assertEqual(deleted, 1)
        records, total = self.db.get_analysis_history_paginated(include_all_users=True)
        self.assertEqual(total, 1)
        self.assertEqual(records[0].query_id, "new")


if __name__ == "__main__":
    unittest.main()
