# -*- coding: utf-8 -*-
"""Regression tests for staging WebUI follow-up features."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.config import Config
from src.storage import DatabaseManager


class WebUiFollowupFeatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.env_file = self.data_dir / ".env"
        self.env_file.write_text("ADMIN_AUTH_ENABLED=true\n", encoding="utf-8")
        os.environ["ENV_FILE"] = str(self.env_file)
        os.environ["DATABASE_PATH"] = str(self.data_dir / "test.db")
        os.environ["ADMIN_AUTH_ENABLED"] = "true"
        os.environ["DSA_REGISTRATION_ENABLED"] = "false"
        os.environ["DSA_REGISTRATION_INVITE_CODE"] = "invite-123"
        os.environ["HISTORY_RETENTION_DAYS"] = "14"
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager()

    def tearDown(self):
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for key in [
            "ENV_FILE",
            "DATABASE_PATH",
            "ADMIN_AUTH_ENABLED",
            "DSA_ADMIN_USERNAME",
            "DSA_ADMIN_PASSWORD",
            "DSA_REGISTRATION_ENABLED",
            "DSA_REGISTRATION_INVITE_CODE",
            "HISTORY_RETENTION_DAYS",
        ]:
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_configured_admin_user_is_seeded_and_updated(self):
        from src.auth import ensure_admin_user, get_user_by_username, verify_user_password

        os.environ["DSA_ADMIN_USERNAME"] = "tony"
        os.environ["DSA_ADMIN_PASSWORD"] = "firstpass1"
        ensure_admin_user()
        user = get_user_by_username("tony")
        self.assertIsNotNone(user)
        self.assertEqual(user.role, "admin")
        self.assertTrue(verify_user_password("tony", "firstpass1"))

        os.environ["DSA_ADMIN_PASSWORD"] = "secondpass2"
        ensure_admin_user()
        user = get_user_by_username("tony")
        self.assertEqual(user.role, "admin")
        self.assertEqual(user.status, "active")
        self.assertTrue(verify_user_password("tony", "secondpass2"))
        self.assertFalse(verify_user_password("tony", "firstpass1"))

    def test_registration_policy_blocks_when_disabled_and_requires_invite(self):
        from src.auth import create_registration_captcha
        from api.v1.endpoints import auth as auth_endpoint
        import asyncio

        challenge = create_registration_captcha()
        request = SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http"), cookies={}, client=SimpleNamespace(host="127.0.0.1"))
        body = auth_endpoint.RegisterRequest(
            username="alice",
            password="abc1234567890xyz",
            passwordConfirm="abc1234567890xyz",
            captchaToken=challenge["captchaToken"],
            captchaAnswer=str(challenge["answer"]),
            inviteCode="wrong",
        )
        response = asyncio.run(auth_endpoint.auth_register(request, body))
        self.assertEqual(response.status_code, 403)
        self.assertIn(b'registration_disabled', response.body)

        os.environ["DSA_REGISTRATION_ENABLED"] = "true"
        response = asyncio.run(auth_endpoint.auth_register(request, body))
        self.assertEqual(response.status_code, 403)
        self.assertIn(b'invalid_invite_code', response.body)

    def test_user_management_lists_and_updates_users_for_admin(self):
        from src.auth import create_user
        from api.v1.endpoints import auth as auth_endpoint
        import asyncio

        admin = create_user("tony", "adminpass1", role="admin")
        user = create_user("alice", "abc1234567890xyz", role="user")
        request = SimpleNamespace(state=SimpleNamespace(current_user={"id": admin.id, "username": "tony", "role": "admin"}))

        response = asyncio.run(auth_endpoint.auth_list_users(request))
        self.assertEqual(response["total"], 2)
        self.assertIn("alice", [item["username"] for item in response["items"]])
        self.assertNotIn("password_hash", response["items"][0])

        update = auth_endpoint.UserUpdateRequest(status="disabled")
        updated = asyncio.run(auth_endpoint.auth_update_user(user.id, request, update))
        self.assertEqual(updated["user"]["status"], "disabled")

    def test_history_response_contains_retention_policy(self):
        from api.v1.endpoints.history import get_history_list

        request = SimpleNamespace(state=SimpleNamespace(current_user={"id": None, "username": "tony", "role": "admin"}))
        result = get_history_list(request=request, stock_code=None, start_date=None, end_date=None, page=1, limit=20, user_id=None, all_users=True, db_manager=self.db)
        self.assertEqual(result.retention_days, 14)
        self.assertTrue(result.auto_cleanup_enabled)

    def test_sea_aliases_resolve_to_se(self):
        from src.data.stock_index_loader import _build_lookup_keys

        keys = {key.lower() for key in _build_lookup_keys("SE", "SE", ["sea", "sea limited", "shopee", "虾皮"])}
        self.assertIn("sea", keys)
        self.assertIn("shopee", keys)
        self.assertIn("虾皮", keys)

    def test_static_stock_index_uses_sea_limited_display_name_for_se(self):
        import json

        checked_paths = [
            Path(__file__).resolve().parents[1] / "apps/dsa-web/public/stocks.index.json",
            Path(__file__).resolve().parents[1] / "static/stocks.index.json",
        ]
        for index_path in checked_paths:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            rows = data.get("items", data) if isinstance(data, dict) else data
            se_rows = [
                row for row in rows
                if isinstance(row, list) and len(row) >= 3 and str(row[1]).upper() == "SE"
            ]
            self.assertTrue(se_rows, f"{index_path} should contain displayCode SE")
            self.assertEqual("Sea Limited", se_rows[0][2], f"{index_path} first SE display name")
            self.assertNotEqual("SEA 'A' SPN.ADR 1:1", se_rows[0][2], f"{index_path} must not expose stale vendor name")


if __name__ == "__main__":
    unittest.main()
