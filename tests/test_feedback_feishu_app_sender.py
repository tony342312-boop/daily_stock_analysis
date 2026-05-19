# -*- coding: utf-8 -*-
"""Unit tests for Feishu App API feedback notification sender."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.services import feedback_service
from src.services.feedback_service import FeedbackPayload


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload, ensure_ascii=False) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _config(**overrides):
    defaults = dict(
        feishu_app_id="cli_test_app",
        feishu_app_secret="secret-value",
        feishu_feedback_receive_id="ou_test_user",
        feishu_feedback_receive_id_type="open_id",
        feishu_feedback_webhook_keyword="",
        feishu_webhook_keyword="",
        feishu_feedback_webhook_url="",
        feishu_webhook_url="",
        webhook_verify_ssl=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FeishuAppFeedbackSenderTest(unittest.TestCase):
    def test_app_api_sender_gets_token_and_sends_interactive_message(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if url == feedback_service._FEISHU_TOKEN_URL:
                return _FakeResponse(payload={"code": 0, "tenant_access_token": "tenant-token"})
            if url == feedback_service._FEISHU_MESSAGE_URL:
                return _FakeResponse(payload={"code": 0, "data": {"message_id": "om_x"}})
            raise AssertionError(url)

        with patch("src.services.feedback_service.get_config", return_value=_config()), patch(
            "src.services.feedback_service.requests.post", side_effect=fake_post
        ):
            ok = feedback_service.send_feedback_to_feishu(FeedbackPayload(content="按钮无响应", category="bug"))

        self.assertTrue(ok)
        self.assertEqual(calls[0][1]["json"]["app_id"], "cli_test_app")
        self.assertEqual(calls[1][1]["params"], {"receive_id_type": "open_id"})
        self.assertEqual(calls[1][1]["json"]["receive_id"], "ou_test_user")
        self.assertEqual(calls[1][1]["json"]["msg_type"], "interactive")
        self.assertEqual(calls[1][1]["headers"]["Authorization"], "Bearer tenant-token")
        self.assertIn("按钮无响应", calls[1][1]["json"]["content"])

    def test_app_api_sender_returns_false_when_token_request_fails(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _FakeResponse(payload={"code": 10003, "msg": "invalid param"})

        with patch("src.services.feedback_service.get_config", return_value=_config()), patch(
            "src.services.feedback_service.requests.post", side_effect=fake_post
        ):
            ok = feedback_service.send_feedback_to_feishu(FeedbackPayload(content="建议新增导出"))

        self.assertFalse(ok)
        self.assertEqual(calls, [feedback_service._FEISHU_TOKEN_URL])

    def test_missing_receive_id_falls_back_to_webhook_path(self):
        with patch("src.services.feedback_service.get_config", return_value=_config(feishu_feedback_receive_id="")), patch(
            "src.services.feedback_service._send_feedback_via_feishu_webhook", return_value=False
        ) as webhook_mock:
            ok = feedback_service.send_feedback_to_feishu(FeedbackPayload(content="建议新增导出"))

        self.assertFalse(ok)
        webhook_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
