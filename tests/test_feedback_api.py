# -*- coding: utf-8 -*-
"""Tests for user feedback submission API and Feishu notification formatting."""

import asyncio
from unittest.mock import patch

from api.v1.endpoints import feedback
from src.auth import reset_current_user_context, set_current_user_context


def test_submit_feedback_requires_non_empty_content():
    response = asyncio.run(feedback.submit_feedback(feedback.FeedbackRequest(content="   ")))

    assert response.status_code == 400
    assert b'"error":"invalid_feedback"' in response.body


def test_submit_feedback_sends_feishu_notification_with_user_context():
    token = set_current_user_context({"id": 7, "username": "alice", "role": "user"})
    try:
        with patch("api.v1.endpoints.feedback.send_feedback_to_feishu", return_value=True) as send_mock:
            response = asyncio.run(
                feedback.submit_feedback(
                    feedback.FeedbackRequest(
                        content="页面点击分析后一直转圈",
                        category="bug",
                        contact="alice@example.com",
                        pageUrl="https://stock.example.com/?stock=600519",
                    )
                )
            )
    finally:
        reset_current_user_context(token)

    assert response.status_code == 200
    assert b'"ok":true' in response.body
    feedback_payload = send_mock.call_args.args[0]
    assert feedback_payload.content == "页面点击分析后一直转圈"
    assert feedback_payload.category == "bug"
    assert feedback_payload.username == "alice"
    assert feedback_payload.user_id == 7
    assert feedback_payload.page_url == "https://stock.example.com/?stock=600519"


def test_submit_feedback_still_accepts_when_feishu_webhook_is_unconfigured():
    with patch("api.v1.endpoints.feedback.send_feedback_to_feishu", return_value=False):
        response = asyncio.run(feedback.submit_feedback(feedback.FeedbackRequest(content="建议增加导出按钮")))

    assert response.status_code == 202
    assert b'"notificationSent":false' in response.body
