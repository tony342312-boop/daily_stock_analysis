# -*- coding: utf-8 -*-
"""User feedback API endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from src.auth import get_current_user_context
except ImportError:  # Older deployments without request user context
    def get_current_user_context():
        return None
from src.services.feedback_service import FeedbackPayload, send_feedback_to_feishu

logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackRequest(BaseModel):
    content: str = Field(..., max_length=2000, description="用户反馈内容")
    category: str = Field("bug", max_length=32, description="反馈类型：bug/iteration/other")
    contact: Optional[str] = Field(None, max_length=200, description="可选联系方式")
    page_url: Optional[str] = Field(None, alias="pageUrl", max_length=500, description="提交反馈时所在页面 URL")


@router.post("")
async def submit_feedback(request: FeedbackRequest):
    content = (request.content or "").strip()
    if not content:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_feedback", "message": "反馈内容不能为空"},
        )

    category = (request.category or "bug").strip().lower()
    if category not in {"bug", "iteration", "other"}:
        category = "other"

    user = get_current_user_context() or {}
    payload = FeedbackPayload(
        content=content,
        category=category,
        contact=(request.contact or "").strip() or None,
        page_url=(request.page_url or "").strip() or None,
        username=user.get("username") if isinstance(user, dict) else None,
        user_id=user.get("id") if isinstance(user, dict) else None,
    )
    notification_sent = send_feedback_to_feishu(payload)
    status_code = 200 if notification_sent else 202
    return JSONResponse(
        status_code=status_code,
        content={"ok": True, "notificationSent": notification_sent},
    )
