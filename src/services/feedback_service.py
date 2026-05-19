# -*- coding: utf-8 -*-
"""User feedback notification service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from src.config import get_config

logger = logging.getLogger(__name__)

_FEISHU_CATEGORY_LABELS = {
    "bug": "Bug / 功能异常",
    "iteration": "迭代建议",
    "other": "其他",
}
_FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_FEISHU_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


@dataclass(frozen=True)
class FeedbackPayload:
    content: str
    category: str = "bug"
    contact: Optional[str] = None
    page_url: Optional[str] = None
    username: Optional[str] = None
    user_id: Optional[int] = None


def _build_security_fields(secret: str) -> dict:
    if not secret:
        return {}
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    sign = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    return {"timestamp": timestamp, "sign": sign}


def _truncate(value: Optional[str], limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_feedback_message(payload: FeedbackPayload, keyword: str = "") -> str:
    category = _FEISHU_CATEGORY_LABELS.get(payload.category, payload.category or "未分类")
    user = payload.username or "匿名/未登录"
    if payload.user_id is not None:
        user = f"{user} (ID: {payload.user_id})"
    submitted_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "**用户反馈 / Bug 上报**",
        f"- 类型：{category}",
        f"- 用户：{user}",
        f"- 联系方式：{_truncate(payload.contact, 120) or '未填写'}",
        f"- 页面：{_truncate(payload.page_url, 300) or '未提供'}",
        f"- 时间：{submitted_at}",
        "",
        "**反馈内容**",
        _truncate(payload.content, 1800),
        "",
        "请先判断是否可复现/是否合理；合理问题进入待办汇总后再安排修复。",
    ]
    content = "\n".join(lines)
    keyword = (keyword or "").strip()
    return f"{keyword}\n{content}" if keyword else content


def _build_interactive_card(payload: FeedbackPayload, keyword: str = "") -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "DSA 用户反馈"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": _format_feedback_message(payload, keyword=keyword),
                },
            }
        ],
    }


def _get_feishu_tenant_access_token(app_id: str, app_secret: str, *, timeout: float = 10) -> Optional[str]:
    try:
        response = requests.post(
            _FEISHU_TOKEN_URL,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=timeout,
        )
    except Exception as exc:  # pragma: no cover - defensive network boundary
        logger.error("反馈飞书 App API 获取 tenant_access_token 异常: %s", exc, exc_info=True)
        return None

    if response.status_code != 200:
        logger.error("反馈飞书 App API 获取 token 失败: HTTP %s %s", response.status_code, response.text[:300])
        return None

    try:
        result = response.json()
    except ValueError:
        logger.error("反馈飞书 App API 获取 token 返回非 JSON: %s", response.text[:300])
        return None

    if result.get("code") != 0:
        safe_result = {k: v for k, v in result.items() if k not in {"tenant_access_token", "app_access_token"}}
        logger.error("反馈飞书 App API 获取 token 返回错误: %s", safe_result)
        return None

    token = (result.get("tenant_access_token") or "").strip()
    if not token:
        logger.error("反馈飞书 App API 获取 token 成功但返回 token 为空")
        return None
    return token


def _send_feedback_via_feishu_app_api(payload: FeedbackPayload) -> bool:
    config = get_config()
    app_id = (getattr(config, "feishu_app_id", None) or "").strip()
    app_secret = (getattr(config, "feishu_app_secret", None) or "").strip()
    receive_id = (getattr(config, "feishu_feedback_receive_id", None) or "").strip()
    receive_id_type = (getattr(config, "feishu_feedback_receive_id_type", None) or "open_id").strip() or "open_id"
    keyword = (
        getattr(config, "feishu_feedback_webhook_keyword", None)
        or getattr(config, "feishu_webhook_keyword", None)
        or ""
    ).strip()

    if not receive_id:
        logger.warning("反馈飞书 App API receive_id 未配置，跳过一对一推送")
        return False
    if not app_id or not app_secret:
        logger.warning("反馈飞书 App API App ID/Secret 未配置，跳过一对一推送")
        return False

    token = _get_feishu_tenant_access_token(app_id, app_secret)
    if not token:
        return False

    params = {"receive_id_type": receive_id_type}
    body = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(_build_interactive_card(payload, keyword=keyword), ensure_ascii=False),
    }
    try:
        response = requests.post(
            _FEISHU_MESSAGE_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json=body,
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover - defensive network boundary
        logger.error("反馈飞书 App API 一对一推送异常: %s", exc, exc_info=True)
        return False

    if response.status_code != 200:
        logger.error("反馈飞书 App API 一对一推送失败: HTTP %s %s", response.status_code, response.text[:500])
        return False

    try:
        result = response.json()
    except ValueError:
        logger.error("反馈飞书 App API 一对一推送返回非 JSON: %s", response.text[:500])
        return False

    if result.get("code") == 0:
        logger.info("反馈飞书 App API 一对一推送成功: receive_id_type=%s", receive_id_type)
        return True

    logger.error("反馈飞书 App API 一对一推送返回错误: %s", result)
    return False


def _send_feedback_via_feishu_webhook(payload: FeedbackPayload) -> bool:
    config = get_config()
    webhook_url = (
        getattr(config, "feishu_feedback_webhook_url", None)
        or getattr(config, "feishu_webhook_url", None)
        or ""
    ).strip()
    if not webhook_url:
        logger.warning("反馈飞书 Webhook 未配置，已接收反馈但不推送")
        return False

    secret = (
        getattr(config, "feishu_feedback_webhook_secret", None)
        or getattr(config, "feishu_webhook_secret", None)
        or ""
    ).strip()
    keyword = (
        getattr(config, "feishu_feedback_webhook_keyword", None)
        or getattr(config, "feishu_webhook_keyword", None)
        or ""
    ).strip()
    verify_ssl = bool(getattr(config, "webhook_verify_ssl", True))
    request_payload = {"msg_type": "interactive", "card": _build_interactive_card(payload, keyword=keyword)}
    request_payload.update(_build_security_fields(secret))

    try:
        response = requests.post(webhook_url, json=request_payload, timeout=10, verify=verify_ssl)
        if response.status_code != 200:
            logger.error("反馈飞书推送失败: HTTP %s %s", response.status_code, response.text[:500])
            return False
        result = response.json()
        code = result.get("code") if "code" in result else result.get("StatusCode")
        if code == 0:
            logger.info("反馈飞书推送成功")
            return True
        logger.error("反馈飞书返回错误: %s", result)
        return False
    except Exception as exc:  # pragma: no cover - defensive network boundary
        logger.error("反馈飞书推送异常: %s", exc, exc_info=True)
        return False


def send_feedback_to_feishu(payload: FeedbackPayload) -> bool:
    """Send feedback notification via Feishu App API or webhook.

    The App API one-to-one path is preferred when FEISHU_FEEDBACK_RECEIVE_ID is
    configured. Notification failures are logged and never reject the user's
    feedback submission.
    """
    config = get_config()
    receive_id = (getattr(config, "feishu_feedback_receive_id", None) or "").strip()
    if receive_id:
        return _send_feedback_via_feishu_app_api(payload)
    return _send_feedback_via_feishu_webhook(payload)
