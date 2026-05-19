# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 通知层
===================================

职责：
1. 汇总分析结果生成日报
2. 支持 Markdown 格式输出
3. 多渠道推送（自动识别）：
   - 企业微信 Webhook
   - 飞书 Webhook
   - Telegram Bot
   - 邮件 SMTP
   - Pushover（手机/桌面推送）
"""
import logging
import re
import urllib.parse
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from enum import Enum

from src.config import get_config
from src.analyzer import AnalysisResult
from src.enums import ReportType
from src.report_language import (
    get_localized_stock_name,
    get_report_labels,
    get_signal_level,
    localize_chip_health,
    localize_operation_advice,
    localize_trend_prediction,
    normalize_report_language,
)
from bot.models import BotMessage
from src.utils.data_processing import normalize_model_used
from src.notification_sender import (
    AstrbotSender,
    CustomWebhookSender,
    DiscordSender,
    EmailSender,
    FeishuSender,
    PushoverSender,
    PushplusSender,
    Serverchan3Sender,
    SlackSender,
    TelegramSender,
    WechatSender,
    WECHAT_IMAGE_MAX_BYTES
)

logger = logging.getLogger(__name__)


class NotificationChannel(Enum):
    """通知渠道类型"""
    WECHAT = "wechat"      # 企业微信
    FEISHU = "feishu"      # 飞书
    TELEGRAM = "telegram"  # Telegram
    EMAIL = "email"        # 邮件
    PUSHOVER = "pushover"  # Pushover（手机/桌面推送）
    PUSHPLUS = "pushplus"  # PushPlus（国内推送服务）
    SERVERCHAN3 = "serverchan3"  # Server酱3（手机APP推送服务）
    CUSTOM = "custom"      # 自定义 Webhook
    DISCORD = "discord"    # Discord 机器人 (Bot)
    SLACK = "slack"        # Slack
    ASTRBOT = "astrbot"
    UNKNOWN = "unknown"    # 未知


class ChannelDetector:
    """
    渠道检测器 - 简化版
    
    根据配置直接判断渠道类型（不再需要 URL 解析）
    """
    
    @staticmethod
    def get_channel_name(channel: NotificationChannel) -> str:
        """获取渠道中文名称"""
        names = {
            NotificationChannel.WECHAT: "企业微信",
            NotificationChannel.FEISHU: "飞书",
            NotificationChannel.TELEGRAM: "Telegram",
            NotificationChannel.EMAIL: "邮件",
            NotificationChannel.PUSHOVER: "Pushover",
            NotificationChannel.PUSHPLUS: "PushPlus",
            NotificationChannel.SERVERCHAN3: "Server酱3",
            NotificationChannel.CUSTOM: "自定义Webhook",
            NotificationChannel.DISCORD: "Discord机器人",
            NotificationChannel.SLACK: "Slack",
            NotificationChannel.ASTRBOT: "ASTRBOT机器人",
            NotificationChannel.UNKNOWN: "未知渠道",
        }
        return names.get(channel, "未知渠道")


class NotificationService(
    AstrbotSender,
    CustomWebhookSender,
    DiscordSender,
    EmailSender,
    FeishuSender,
    PushoverSender,
    PushplusSender,
    Serverchan3Sender,
    SlackSender,
    TelegramSender,
    WechatSender
):
    """
    通知服务
    
    职责：
    1. 生成 Markdown 格式的分析日报
    2. 向所有已配置的渠道推送消息（多渠道并发）
    3. 支持本地保存日报
    
    支持的渠道：
    - 企业微信 Webhook
    - 飞书 Webhook
    - Telegram Bot
    - 邮件 SMTP
    - Pushover（手机/桌面推送）
    
    注意：所有已配置的渠道都会收到推送
    """
    
    def __init__(self, source_message: Optional[BotMessage] = None):
        """
        初始化通知服务
        
        检测所有已配置的渠道，推送时会向所有渠道发送
        """
        config = get_config()
        self._source_message = source_message
        self._context_channels: List[str] = []

        # Markdown 转图片（Issue #289）
        self._markdown_to_image_channels = set(
            getattr(config, 'markdown_to_image_channels', []) or []
        )
        self._markdown_to_image_max_chars = getattr(
            config, 'markdown_to_image_max_chars', 15000
        )

        # 仅分析结果摘要（Issue #262）：true 时只推送汇总，不含个股详情
        self._report_summary_only = getattr(config, 'report_summary_only', False)
        self._history_compare_cache: Dict[Tuple[int, Tuple[Tuple[str, str], ...]], Dict[str, List[Dict[str, Any]]]] = {}

        # 初始化各渠道
        AstrbotSender.__init__(self, config)
        CustomWebhookSender.__init__(self, config)
        DiscordSender.__init__(self, config)
        EmailSender.__init__(self, config)
        FeishuSender.__init__(self, config)
        PushoverSender.__init__(self, config)
        PushplusSender.__init__(self, config)
        Serverchan3Sender.__init__(self, config)
        SlackSender.__init__(self, config)
        TelegramSender.__init__(self, config)
        WechatSender.__init__(self, config)

        # 检测所有已配置的渠道
        self._available_channels = self._detect_all_channels()
        if self._has_context_channel():
            self._context_channels.append("钉钉会话")

        if not self._available_channels and not self._context_channels:
            logger.warning("未配置有效的通知渠道，将不发送推送通知")
        else:
            channel_names = [ChannelDetector.get_channel_name(ch) for ch in self._available_channels]
            channel_names.extend(self._context_channels)
            logger.info(f"已配置 {len(channel_names)} 个通知渠道：{', '.join(channel_names)}")

    def _normalize_report_type(self, report_type: Any) -> ReportType:
        """Normalize string/enum input into ReportType."""
        if isinstance(report_type, ReportType):
            return report_type
        return ReportType.from_str(report_type)

    def _get_report_language(self, payload: Optional[Any] = None) -> str:
        """Resolve report language from result payload or global config."""
        if isinstance(payload, list):
            for item in payload:
                language = getattr(item, "report_language", None)
                if language:
                    return normalize_report_language(language)
        elif payload is not None:
            language = getattr(payload, "report_language", None)
            if language:
                return normalize_report_language(language)

        return normalize_report_language(getattr(get_config(), "report_language", "zh"))

    def _get_labels(self, payload: Optional[Any] = None) -> Dict[str, str]:
        return get_report_labels(self._get_report_language(payload))

    def _get_display_name(self, result: AnalysisResult, language: Optional[str] = None) -> str:
        report_language = normalize_report_language(language or self._get_report_language(result))
        return self._escape_md(
            get_localized_stock_name(result.name, result.code, report_language)
        )

    def _get_history_compare_context(self, results: List[AnalysisResult]) -> Dict[str, Any]:
        """Fetch and cache history comparison data for markdown rendering."""
        config = get_config()
        history_compare_n = getattr(config, 'report_history_compare_n', 0)
        if history_compare_n <= 0 or not results:
            return {"history_by_code": {}}

        cache_key = (
            history_compare_n,
            tuple(sorted((r.code, getattr(r, 'query_id', '') or '') for r in results)),
        )
        if cache_key in self._history_compare_cache:
            return {"history_by_code": self._history_compare_cache[cache_key]}

        try:
            from src.services.history_comparison_service import get_signal_changes_batch

            exclude_ids = {
                r.code: r.query_id
                for r in results
                if getattr(r, 'query_id', None)
            }
            codes = list(dict.fromkeys(r.code for r in results))
            history_by_code = get_signal_changes_batch(
                codes,
                limit=history_compare_n,
                exclude_query_ids=exclude_ids,
            )
        except Exception as e:
            logger.debug("History comparison skipped: %s", e)
            history_by_code = {}

        self._history_compare_cache[cache_key] = history_by_code
        return {"history_by_code": history_by_code}

    def generate_aggregate_report(
        self,
        results: List[AnalysisResult],
        report_type: Any,
        report_date: Optional[str] = None,
    ) -> str:
        """Generate the aggregate report content used by merge/save/push paths."""
        normalized_type = self._normalize_report_type(report_type)
        if normalized_type == ReportType.BRIEF:
            return self.generate_brief_report(results, report_date=report_date)
        return self.generate_dashboard_report(results, report_date=report_date)

    def _collect_models_used(self, results: List[AnalysisResult]) -> List[str]:
        models: List[str] = []
        for result in results:
            model = normalize_model_used(getattr(result, "model_used", None))
            if model:
                models.append(model)
        return list(dict.fromkeys(models))
    
    def _detect_all_channels(self) -> List[NotificationChannel]:
        """
        检测所有已配置的渠道
        
        Returns:
            已配置的渠道列表
        """
        channels = []
        
        # 企业微信
        if self._wechat_url:
            channels.append(NotificationChannel.WECHAT)
        
        # 飞书
        if self._feishu_url:
            channels.append(NotificationChannel.FEISHU)
        
        # Telegram
        if self._is_telegram_configured():
            channels.append(NotificationChannel.TELEGRAM)
        
        # 邮件
        if self._is_email_configured():
            channels.append(NotificationChannel.EMAIL)
        
        # Pushover
        if self._is_pushover_configured():
            channels.append(NotificationChannel.PUSHOVER)

        # PushPlus
        if self._pushplus_token:
            channels.append(NotificationChannel.PUSHPLUS)

       # Server酱3
        if self._serverchan3_sendkey:
            channels.append(NotificationChannel.SERVERCHAN3)
       
        # 自定义 Webhook
        if self._custom_webhook_urls:
            channels.append(NotificationChannel.CUSTOM)
        
        # Discord
        if self._is_discord_configured():
            channels.append(NotificationChannel.DISCORD)
        # Slack
        if self._is_slack_configured():
            channels.append(NotificationChannel.SLACK)
        # AstrBot
        if self._is_astrbot_configured():
            channels.append(NotificationChannel.ASTRBOT)
        return channels

    def is_available(self) -> bool:
        """检查通知服务是否可用（至少有一个渠道或上下文渠道）"""
        return len(self._available_channels) > 0 or self._has_context_channel()
    
    def get_available_channels(self) -> List[NotificationChannel]:
        """获取所有已配置的渠道"""
        return self._available_channels
    
    def get_channel_names(self) -> str:
        """获取所有已配置渠道的名称"""
        names = [ChannelDetector.get_channel_name(ch) for ch in self._available_channels]
        if self._has_context_channel():
            names.append("钉钉会话")
        return ', '.join(names)

    # ===== Context channel =====
    def _has_context_channel(self) -> bool:
        """判断是否存在基于消息上下文的临时渠道（如钉钉会话、飞书会话）"""
        return (
            self._extract_dingtalk_session_webhook() is not None
            or self._extract_feishu_reply_info() is not None
        )

    def _extract_dingtalk_session_webhook(self) -> Optional[str]:
        """从来源消息中提取钉钉会话 Webhook（用于 Stream 模式回复）"""
        if not isinstance(self._source_message, BotMessage):
            return None
        raw_data = getattr(self._source_message, "raw_data", {}) or {}
        if not isinstance(raw_data, dict):
            return None
        session_webhook = (
            raw_data.get("_session_webhook")
            or raw_data.get("sessionWebhook")
            or raw_data.get("session_webhook")
            or raw_data.get("session_webhook_url")
        )
        if not session_webhook and isinstance(raw_data.get("headers"), dict):
            session_webhook = raw_data["headers"].get("sessionWebhook")
        return session_webhook

    def _extract_feishu_reply_info(self) -> Optional[Dict[str, str]]:
        """
        从来源消息中提取飞书回复信息（用于 Stream 模式回复）
        
        Returns:
            包含 chat_id 的字典，或 None
        """
        if not isinstance(self._source_message, BotMessage):
            return None
        if getattr(self._source_message, "platform", "") != "feishu":
            return None
        chat_id = getattr(self._source_message, "chat_id", "")
        if not chat_id:
            return None
        return {"chat_id": chat_id}

    def send_to_context(self, content: str) -> bool:
        """
        向基于消息上下文的渠道发送消息（例如钉钉 Stream 会话）
        
        Args:
            content: Markdown 格式内容
        """
        return self._send_via_source_context(content)
    
    def _send_via_source_context(self, content: str) -> bool:
        """
        使用消息上下文（如钉钉/飞书会话）发送一份报告
        
        主要用于从机器人 Stream 模式触发的任务，确保结果能回到触发的会话。
        """
        success = False
        
        # 尝试钉钉会话
        session_webhook = self._extract_dingtalk_session_webhook()
        if session_webhook:
            try:
                if self._send_dingtalk_chunked(session_webhook, content, max_bytes=20000):
                    logger.info("已通过钉钉会话（Stream）推送报告")
                    success = True
                else:
                    logger.error("钉钉会话（Stream）推送失败")
            except Exception as e:
                logger.error(f"钉钉会话（Stream）推送异常: {e}")

        # 尝试飞书会话
        feishu_info = self._extract_feishu_reply_info()
        if feishu_info:
            try:
                if self._send_feishu_stream_reply(feishu_info["chat_id"], content):
                    logger.info("已通过飞书会话（Stream）推送报告")
                    success = True
                else:
                    logger.error("飞书会话（Stream）推送失败")
            except Exception as e:
                logger.error(f"飞书会话（Stream）推送异常: {e}")

        return success

    def _send_feishu_stream_reply(self, chat_id: str, content: str) -> bool:
        """
        通过飞书 Stream 模式发送消息到指定会话
        
        Args:
            chat_id: 飞书会话 ID
            content: 消息内容
            
        Returns:
            是否发送成功
        """
        try:
            from bot.platforms.feishu_stream import FeishuReplyClient, FEISHU_SDK_AVAILABLE
            if not FEISHU_SDK_AVAILABLE:
                logger.warning("飞书 SDK 不可用，无法发送 Stream 回复")
                return False
            
            from src.config import get_config
            config = get_config()
            
            app_id = getattr(config, 'feishu_app_id', None)
            app_secret = getattr(config, 'feishu_app_secret', None)
            
            if not app_id or not app_secret:
                logger.warning("飞书 APP_ID 或 APP_SECRET 未配置")
                return False
            
            # 创建回复客户端
            reply_client = FeishuReplyClient(app_id, app_secret)
            
            # 飞书文本消息有长度限制，需要分批发送
            max_bytes = getattr(config, 'feishu_max_bytes', 20000)
            content_bytes = len(content.encode('utf-8'))
            
            if content_bytes > max_bytes:
                return self._send_feishu_stream_chunked(reply_client, chat_id, content, max_bytes)
            
            return reply_client.send_to_chat(chat_id, content)
            
        except ImportError as e:
            logger.error(f"导入飞书 Stream 模块失败: {e}")
            return False
        except Exception as e:
            logger.error(f"飞书 Stream 回复异常: {e}")
            return False

    def _send_feishu_stream_chunked(
        self, 
        reply_client, 
        chat_id: str, 
        content: str, 
        max_bytes: int
    ) -> bool:
        """
        分批发送长消息到飞书（Stream 模式）
        
        Args:
            reply_client: FeishuReplyClient 实例
            chat_id: 飞书会话 ID
            content: 完整消息内容
            max_bytes: 单条消息最大字节数
            
        Returns:
            是否全部发送成功
        """
        import time
        
        def get_bytes(s: str) -> int:
            return len(s.encode('utf-8'))
        
        # 按段落或分隔线分割
        if "\n---\n" in content:
            sections = content.split("\n---\n")
            separator = "\n---\n"
        elif "\n### " in content:
            parts = content.split("\n### ")
            sections = [parts[0]] + [f"### {p}" for p in parts[1:]]
            separator = "\n"
        else:
            # 按行分割
            sections = content.split("\n")
            separator = "\n"
        
        chunks = []
        current_chunk = []
        current_bytes = 0
        separator_bytes = get_bytes(separator)
        
        for section in sections:
            section_bytes = get_bytes(section) + separator_bytes
            
            if current_bytes + section_bytes > max_bytes:
                if current_chunk:
                    chunks.append(separator.join(current_chunk))
                current_chunk = [section]
                current_bytes = section_bytes
            else:
                current_chunk.append(section)
                current_bytes += section_bytes
        
        if current_chunk:
            chunks.append(separator.join(current_chunk))
        
        # 发送每个分块
        success = True
        for i, chunk in enumerate(chunks):
            if i > 0:
                time.sleep(0.5)  # 避免请求过快
            
            if not reply_client.send_to_chat(chat_id, chunk):
                success = False
                logger.error(f"飞书 Stream 分块 {i+1}/{len(chunks)} 发送失败")
        
        return success
        
    def generate_daily_report(
        self,
        results: List[AnalysisResult],
        report_date: Optional[str] = None
    ) -> str:
        """
        生成 Markdown 格式的日报（详细版）

        Args:
            results: 分析结果列表
            report_date: 报告日期（默认今天）

        Returns:
            Markdown 格式的日报内容
        """
        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')
        report_language = self._get_report_language(results)
        labels = get_report_labels(report_language)

        # 标题
        report_lines = [
            f"# 📅 {report_date} {labels['report_title']}",
            "",
            f"> {labels['analyzed_prefix']} **{len(results)}** {labels['stock_unit']} | "
            f"{labels['generated_at_label']}：{datetime.now().strftime('%H:%M:%S')}",
            "",
            "---",
            "",
        ]
        
        # 按评分排序（高分在前）
        sorted_results = sorted(
            results, 
            key=lambda x: x.sentiment_score, 
            reverse=True
        )
        
        # 统计信息 - 使用 decision_type 字段准确统计
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))
        avg_score = sum(r.sentiment_score for r in results) / len(results) if results else 0
        
        report_lines.extend([
            f"## 📊 {labels['summary_heading']}",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 🟢 {labels['buy_label']} | **{buy_count}** {labels['stock_unit_compact']} |",
            f"| 🟡 {labels['watch_label']} | **{hold_count}** {labels['stock_unit_compact']} |",
            f"| 🔴 {labels['sell_label']} | **{sell_count}** {labels['stock_unit_compact']} |",
            f"| 📈 {labels['avg_score_label']} | **{avg_score:.1f}** |",
            "",
            "---",
            "",
        ])
        
        # Issue #262: summary_only 时仅输出摘要，跳过个股详情
        if self._report_summary_only:
            report_lines.extend([f"## 📊 {labels['summary_heading']}", ""])
            for r in sorted_results:
                _, emoji, _ = self._get_signal_level(r)
                report_lines.append(
                    f"{emoji} **{self._get_display_name(r, report_language)}({r.code})**: "
                    f"{localize_operation_advice(r.operation_advice, report_language)} | "
                    f"{labels['score_label']} {r.sentiment_score} | "
                    f"{localize_trend_prediction(r.trend_prediction, report_language)}"
                )
        else:
            report_lines.extend([f"## 📈 {labels['report_title']}", ""])
            # 逐个股票的详细分析
            for result in sorted_results:
                _, emoji, _ = self._get_signal_level(result)
                confidence_stars = result.get_confidence_stars() if hasattr(result, 'get_confidence_stars') else '⭐⭐'
                
                report_lines.extend([
                    f"### {emoji} {self._get_display_name(result, report_language)} ({result.code})",
                    "",
                    f"**{labels['action_advice_label']}：{localize_operation_advice(result.operation_advice, report_language)}** | "
                    f"**{labels['score_label']}：{result.sentiment_score}** | "
                    f"**{labels['trend_label']}：{localize_trend_prediction(result.trend_prediction, report_language)}** | "
                    f"**Confidence：{confidence_stars}**",
                    "",
                ])

                self._append_market_snapshot(report_lines, result)
                self._append_technical_indicator_snapshot(report_lines, result)
                
                # 核心看点
                if hasattr(result, 'key_points') and result.key_points:
                    report_lines.extend([
                        f"**🎯 核心看点**：{result.key_points}",
                        "",
                    ])
                
                # 买入/卖出理由
                if hasattr(result, 'buy_reason') and result.buy_reason:
                    report_lines.extend([
                        f"**💡 操作理由**：{result.buy_reason}",
                        "",
                    ])
                
                # 走势分析
                if hasattr(result, 'trend_analysis') and result.trend_analysis:
                    report_lines.extend([
                        "#### 📉 走势分析",
                        f"{result.trend_analysis}",
                        "",
                    ])
                
                # 短期/中期展望
                outlook_lines = []
                if hasattr(result, 'short_term_outlook') and result.short_term_outlook:
                    outlook_lines.append(f"- **短期（1-3日）**：{result.short_term_outlook}")
                if hasattr(result, 'medium_term_outlook') and result.medium_term_outlook:
                    outlook_lines.append(f"- **中期（1-2周）**：{result.medium_term_outlook}")
                if outlook_lines:
                    report_lines.extend([
                        "#### 🔮 市场展望",
                        *outlook_lines,
                        "",
                    ])
                
                # 技术面分析
                tech_lines = []
                if result.technical_analysis:
                    tech_lines.append(f"**综合**：{result.technical_analysis}")
                if hasattr(result, 'ma_analysis') and result.ma_analysis:
                    tech_lines.append(f"**均线**：{result.ma_analysis}")
                if hasattr(result, 'volume_analysis') and result.volume_analysis:
                    tech_lines.append(f"**量能**：{result.volume_analysis}")
                if hasattr(result, 'pattern_analysis') and result.pattern_analysis:
                    tech_lines.append(f"**形态**：{result.pattern_analysis}")
                if tech_lines:
                    report_lines.extend([
                        "#### 📊 技术面分析",
                        *tech_lines,
                        "",
                    ])
                
                # 基本面分析
                fund_lines = []
                if hasattr(result, 'fundamental_analysis') and result.fundamental_analysis:
                    fund_lines.append(result.fundamental_analysis)
                if hasattr(result, 'sector_position') and result.sector_position:
                    fund_lines.append(f"**板块地位**：{result.sector_position}")
                if hasattr(result, 'company_highlights') and result.company_highlights:
                    fund_lines.append(f"**公司亮点**：{result.company_highlights}")
                if fund_lines:
                    report_lines.extend([
                        "#### 🏢 基本面分析",
                        *fund_lines,
                        "",
                    ])
                
                # 消息面/情绪面
                news_lines = []
                if result.news_summary:
                    news_lines.append(f"**新闻摘要**：{result.news_summary}")
                if hasattr(result, 'market_sentiment') and result.market_sentiment:
                    news_lines.append(f"**市场情绪**：{result.market_sentiment}")
                if hasattr(result, 'hot_topics') and result.hot_topics:
                    news_lines.append(f"**相关热点**：{result.hot_topics}")
                if news_lines:
                    report_lines.extend([
                        "#### 📰 消息面/情绪面",
                        *news_lines,
                        "",
                    ])
                
                # 综合分析
                if result.analysis_summary:
                    report_lines.extend([
                        "#### 📝 综合分析",
                        result.analysis_summary,
                        "",
                    ])
                
                # 风险提示
                if hasattr(result, 'risk_warning') and result.risk_warning:
                    report_lines.extend([
                        f"⚠️ **风险提示**：{result.risk_warning}",
                        "",
                    ])
                
                # 数据来源说明
                if hasattr(result, 'search_performed') and result.search_performed:
                    report_lines.append("*🔍 已执行联网搜索*")
                if hasattr(result, 'data_sources') and result.data_sources:
                    report_lines.append(f"*📋 数据来源：{result.data_sources}*")
                
                # 错误信息（如果有）
                if not result.success and result.error_message:
                    report_lines.extend([
                        "",
                        f"❌ **分析异常**：{result.error_message[:100]}",
                    ])
                
                report_lines.extend([
                    "",
                    "---",
                    "",
                ])
        
        # 底部信息（去除免责声明）
        report_lines.extend([
            "",
            f"*{labels['generated_at_label']}：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        ])
        
        return "\n".join(report_lines)
    
    @staticmethod
    def _escape_md(name: str) -> str:
        """Escape markdown special characters in stock names (e.g. *ST → \\*ST)."""
        return name.replace('*', r'\*') if name else name

    @staticmethod
    def _clean_sniper_value(value: Any) -> str:
        """Normalize sniper point values and remove redundant label prefixes."""
        if value is None:
            return 'N/A'
        if isinstance(value, (int, float)):
            return str(value)
        if not isinstance(value, str):
            return str(value)
        if not value or value == 'N/A':
            return value
        prefixes = ['理想买入点：', '次优买入点：', '止损位：', '目标位：',
                     '理想买入点:', '次优买入点:', '止损位:', '目标位:',
                     'Ideal Entry:', 'Secondary Entry:', 'Stop Loss:', 'Target:']
        for prefix in prefixes:
            if value.startswith(prefix):
                return value[len(prefix):]
        return value

    def _get_signal_level(self, result: AnalysisResult) -> tuple:
        """Get localized signal level and color based on operation advice."""
        return get_signal_level(
            result.operation_advice,
            result.sentiment_score,
            self._get_report_language(result),
        )
    
    def generate_dashboard_report(
        self,
        results: List[AnalysisResult],
        report_date: Optional[str] = None
    ) -> str:
        """
        生成决策仪表盘格式的日报（详细版）

        格式：市场概览 + 重要信息 + 核心结论 + 数据透视 + 作战计划

        Args:
            results: 分析结果列表
            report_date: 报告日期（默认今天）

        Returns:
            Markdown 格式的决策仪表盘日报
        """
        config = get_config()
        report_language = self._get_report_language(results)
        labels = get_report_labels(report_language)
        reason_label = "Rationale" if report_language == "en" else "操作理由"
        risk_warning_label = "Risk Warning" if report_language == "en" else "风险提示"
        technical_heading = "Technicals" if report_language == "en" else "技术面"
        ma_label = "Moving Averages" if report_language == "en" else "均线"
        volume_analysis_label = "Volume" if report_language == "en" else "量能"
        news_heading = "News Flow" if report_language == "en" else "消息面"
        if getattr(config, 'report_renderer_enabled', False) and results:
            from src.services.report_renderer import render
            out = render(
                platform='markdown',
                results=results,
                report_date=report_date,
                summary_only=self._report_summary_only,
                extra_context={
                    **self._get_history_compare_context(results),
                    "report_language": report_language,
                },
            )
            if out:
                return out

        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')

        # 按评分排序（高分在前）
        sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)

        # 统计信息 - 使用 decision_type 字段准确统计
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))

        report_lines = [
            f"# 🎯 {report_date} {labels['dashboard_title']}",
            "",
            f"> {labels['analyzed_prefix']} **{len(results)}** {labels['stock_unit']} | "
            f"🟢{labels['buy_label']}:{buy_count} 🟡{labels['watch_label']}:{hold_count} 🔴{labels['sell_label']}:{sell_count}",
            "",
        ]

        # === 新增：分析结果摘要 (Issue #112) ===
        if results:
            report_lines.extend([
                f"## 📊 {labels['summary_heading']}",
                "",
            ])
            for r in sorted_results:
                _, signal_emoji, _ = self._get_signal_level(r)
                display_name = self._get_display_name(r, report_language)
                report_lines.append(
                    f"{signal_emoji} **{display_name}({r.code})**: "
                    f"{localize_operation_advice(r.operation_advice, report_language)} | "
                    f"{labels['score_label']} {r.sentiment_score} | "
                    f"{localize_trend_prediction(r.trend_prediction, report_language)}"
                )
            report_lines.extend([
                "",
                "---",
                "",
            ])

        # 逐个股票的决策仪表盘（Issue #262: summary_only 时跳过详情）
        if not self._report_summary_only:
            for result in sorted_results:
                signal_text, signal_emoji, signal_tag = self._get_signal_level(result)
                dashboard = result.dashboard if hasattr(result, 'dashboard') and result.dashboard else {}
                
                # 股票名称（优先使用 dashboard 或 result 中的名称，转义 *ST 等特殊字符）
                stock_name = self._get_display_name(result, report_language)
                
                report_lines.extend([
                    f"## {signal_emoji} {stock_name} ({result.code})",
                    "",
                ])
                
                # ========== 舆情与基本面概览（放在最前面）==========
                intel = dashboard.get('intelligence', {}) if dashboard else {}
                if intel:
                    report_lines.extend([
                        f"### 📰 {labels['info_heading']}",
                        "",
                    ])
                    # 舆情情绪总结
                    if intel.get('sentiment_summary'):
                        report_lines.append(f"**💭 {labels['sentiment_summary_label']}**: {intel['sentiment_summary']}")
                    # 业绩预期
                    if intel.get('earnings_outlook'):
                        report_lines.append(f"**📊 {labels['earnings_outlook_label']}**: {intel['earnings_outlook']}")
                    # 风险警报（醒目显示）
                    risk_alerts = intel.get('risk_alerts', [])
                    if risk_alerts:
                        report_lines.append("")
                        report_lines.append(f"**🚨 {labels['risk_alerts_label']}**:")
                        for alert in risk_alerts:
                            if self._should_skip_display_item(alert):
                                continue
                            report_lines.append(f"- {alert}")
                    # 利好催化
                    catalysts = intel.get('positive_catalysts', [])
                    if catalysts:
                        report_lines.append("")
                        report_lines.append(f"**✨ {labels['positive_catalysts_label']}**:")
                        for cat in catalysts:
                            report_lines.append(f"- {cat}")
                    # 最新消息
                    if intel.get('latest_news'):
                        report_lines.append("")
                        report_lines.append(f"**📢 {labels['latest_news_label']}**: {intel['latest_news']}")
                    report_lines.append("")

                self._append_news_context_snapshot(report_lines, result)
                self._append_filing_references(report_lines, result)
                self._append_fundamental_snapshot(report_lines, result)
                self._append_financial_statement_analysis(report_lines, result)
                self._append_dividend_snapshot(report_lines, result)
                self._append_peer_valuation_snapshot(report_lines, result)
                self._append_macro_snapshot(report_lines, result)
                self._append_integrated_research_framework(report_lines, result)
                
                # ========== 核心结论 ==========
                core = dashboard.get('core_conclusion', {}) if dashboard else {}
                one_sentence = core.get('one_sentence', result.analysis_summary)
                time_sense = core.get('time_sensitivity', labels['default_time_sensitivity'])
                pos_advice = core.get('position_advice', {})
                
                report_lines.extend([
                    f"### 📌 {labels['core_conclusion_heading']}",
                    "",
                    f"**{signal_emoji} {signal_text}** | {localize_trend_prediction(result.trend_prediction, report_language)}",
                    "",
                    f"> **{labels['one_sentence_label']}**: {one_sentence}",
                    "",
                    f"⏰ **{labels['time_sensitivity_label']}**: {time_sense}",
                    "",
                ])
                # 持仓分类建议
                if pos_advice:
                    report_lines.extend([
                        f"| {labels['position_status_label']} | {labels['action_advice_label']} |",
                        "|---------|---------|",
                        f"| 🆕 **{labels['no_position_label']}** | {pos_advice.get('no_position', localize_operation_advice(result.operation_advice, report_language))} |",
                        f"| 💼 **{labels['has_position_label']}** | {pos_advice.get('has_position', labels['continue_holding'])} |",
                        "",
                    ])

                self._append_market_snapshot(report_lines, result)
                self._append_technical_indicator_snapshot(report_lines, result)
                
                # ========== 数据透视 ==========
                data_persp = dashboard.get('data_perspective', {}) if dashboard else {}
                if data_persp:
                    trend_data = data_persp.get('trend_status', {})
                    price_data = data_persp.get('price_position', {})
                    vol_data = data_persp.get('volume_analysis', {})
                    chip_data = data_persp.get('chip_structure', {})
                    
                    report_lines.extend([
                        f"### 📊 {labels['data_perspective_heading']}",
                        "",
                    ])
                    # 趋势状态
                    if trend_data:
                        is_bullish = (
                            f"✅ {labels['yes_label']}"
                            if trend_data.get('is_bullish', False)
                            else f"❌ {labels['no_label']}"
                        )
                        report_lines.extend([
                            f"**{labels['ma_alignment_label']}**: {trend_data.get('ma_alignment', 'N/A')} | "
                            f"{labels['bullish_alignment_label']}: {is_bullish} | "
                            f"{labels['trend_strength_label']}: {trend_data.get('trend_score', 'N/A')}/100",
                            "",
                        ])
                    # 价格位置
                    if price_data:
                        bias_status = price_data.get('bias_status', 'N/A')
                        report_lines.extend([
                            f"| {labels['price_metrics_label']} | {labels['current_price_label']} |",
                            "|---------|------|",
                            f"| {labels['current_price_label']} | {price_data.get('current_price', 'N/A')} |",
                            f"| {labels['ma5_label']} | {price_data.get('ma5', 'N/A')} |",
                            f"| {labels['ma10_label']} | {price_data.get('ma10', 'N/A')} |",
                            f"| {labels['ma20_label']} | {price_data.get('ma20', 'N/A')} |",
                            f"| {labels['bias_ma5_label']} | {price_data.get('bias_ma5', 'N/A')}% {bias_status} |",
                            f"| {labels['support_level_label']} | {price_data.get('support_level', 'N/A')} |",
                            f"| {labels['resistance_level_label']} | {price_data.get('resistance_level', 'N/A')} |",
                            "",
                        ])
                    # 量能分析
                    if vol_data:
                        report_lines.extend([
                            f"**{labels['volume_label']}**: {labels['volume_ratio_label']} {vol_data.get('volume_ratio', 'N/A')} ({vol_data.get('volume_status', '')}) | "
                            f"{labels['turnover_rate_label']} {vol_data.get('turnover_rate', 'N/A')}%",
                            f"💡 *{vol_data.get('volume_meaning', '')}*",
                            "",
                        ])
                    # 筹码结构
                    if chip_data:
                        chip_health = localize_chip_health(chip_data.get('chip_health', 'N/A'), report_language)
                        report_lines.extend([
                            f"**{labels['chip_label']}**: {chip_data.get('profit_ratio', 'N/A')} | {chip_data.get('avg_cost', 'N/A')} | "
                            f"{chip_data.get('concentration', 'N/A')} {chip_health}",
                            "",
                        ])
                
                # ========== 作战计划 ==========
                battle = dashboard.get('battle_plan', {}) if dashboard else {}
                if battle:
                    report_lines.extend([
                        f"### 🎯 {labels['battle_plan_heading']}",
                        "",
                    ])
                    # 狙击点位
                    sniper = battle.get('sniper_points', {})
                    if sniper:
                        report_lines.extend([
                            f"**📍 {labels['action_points_heading']}**",
                            "",
                            f"| {labels['action_points_heading']} | {labels['current_price_label']} |",
                            "|---------|------|",
                            f"| 🎯 {labels['ideal_buy_label']} | {self._clean_sniper_value(sniper.get('ideal_buy', 'N/A'))} |",
                            f"| 🔵 {labels['secondary_buy_label']} | {self._clean_sniper_value(sniper.get('secondary_buy', 'N/A'))} |",
                            f"| 🛑 {labels['stop_loss_label']} | {self._clean_sniper_value(sniper.get('stop_loss', 'N/A'))} |",
                            f"| 🎊 {labels['take_profit_label']} | {self._clean_sniper_value(sniper.get('take_profit', 'N/A'))} |",
                            "",
                        ])
                    # 仓位策略
                    position = battle.get('position_strategy', {})
                    if position:
                        report_lines.extend([
                            f"**💰 {labels['suggested_position_label']}**: {position.get('suggested_position', 'N/A')}",
                            f"- {labels['entry_plan_label']}: {position.get('entry_plan', 'N/A')}",
                            f"- {labels['risk_control_label']}: {position.get('risk_control', 'N/A')}",
                            "",
                        ])
                    # 检查清单
                    checklist = battle.get('action_checklist', []) if battle else []
                    if checklist:
                        report_lines.extend([
                            f"**✅ {labels['checklist_heading']}**",
                            "",
                        ])
                        for item in checklist:
                            if self._should_skip_display_item(item):
                                continue
                            report_lines.append(f"- {item}")
                        report_lines.append("")
                
                # 如果没有 dashboard，显示传统格式
                if not dashboard:
                    # 操作理由
                    if result.buy_reason:
                        report_lines.extend([
                            f"**💡 {reason_label}**: {result.buy_reason}",
                            "",
                        ])
                    # 风险提示
                    if result.risk_warning:
                        report_lines.extend([
                            f"**⚠️ {risk_warning_label}**: {result.risk_warning}",
                            "",
                        ])
                    # 技术面分析
                    if result.ma_analysis or result.volume_analysis:
                        report_lines.extend([
                            f"### 📊 {technical_heading}",
                            "",
                        ])
                        if result.ma_analysis:
                            report_lines.append(f"**{ma_label}**: {result.ma_analysis}")
                        if result.volume_analysis:
                            report_lines.append(f"**{volume_analysis_label}**: {result.volume_analysis}")
                        report_lines.append("")
                    # 消息面
                    if result.news_summary:
                        report_lines.extend([
                            f"### 📰 {news_heading}",
                            f"{result.news_summary}",
                            "",
                        ])
                
                report_lines.extend([
                    "---",
                    "",
                ])
        
        # 底部（去除免责声明）
        report_lines.extend([
            "",
            f"*{labels['generated_at_label']}：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        ])
        
        return "\n".join(report_lines)
    
    def generate_wechat_dashboard(self, results: List[AnalysisResult]) -> str:
        """
        生成企业微信决策仪表盘精简版（控制在4000字符内）
        
        只保留核心结论和狙击点位
        
        Args:
            results: 分析结果列表
            
        Returns:
            精简版决策仪表盘
        """
        config = get_config()
        report_language = self._get_report_language(results)
        labels = get_report_labels(report_language)
        if getattr(config, 'report_renderer_enabled', False) and results:
            from src.services.report_renderer import render
            out = render(
                platform='wechat',
                results=results,
                report_date=datetime.now().strftime('%Y-%m-%d'),
                summary_only=self._report_summary_only,
                extra_context={"report_language": report_language},
            )
            if out:
                return out

        report_date = datetime.now().strftime('%Y-%m-%d')
        
        # 按评分排序
        sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)
        
        # 统计 - 使用 decision_type 字段准确统计
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))
        
        lines = [
            f"## 🎯 {report_date} {labels['dashboard_title']}",
            "",
            f"> {len(results)} {labels['stock_unit']} | "
            f"🟢{labels['buy_label']}:{buy_count} 🟡{labels['watch_label']}:{hold_count} 🔴{labels['sell_label']}:{sell_count}",
            "",
        ]
        
        # Issue #262: summary_only 时仅输出摘要列表
        if self._report_summary_only:
            lines.append(f"**📊 {labels['summary_heading']}**")
            lines.append("")
            for r in sorted_results:
                _, signal_emoji, _ = self._get_signal_level(r)
                stock_name = self._get_display_name(r, report_language)
                lines.append(
                    f"{signal_emoji} **{stock_name}({r.code})**: "
                    f"{localize_operation_advice(r.operation_advice, report_language)} | "
                    f"{labels['score_label']} {r.sentiment_score} | "
                    f"{localize_trend_prediction(r.trend_prediction, report_language)}"
                )
        else:
            for result in sorted_results:
                signal_text, signal_emoji, _ = self._get_signal_level(result)
                dashboard = result.dashboard if hasattr(result, 'dashboard') and result.dashboard else {}
                core = dashboard.get('core_conclusion', {}) if dashboard else {}
                battle = dashboard.get('battle_plan', {}) if dashboard else {}
                intel = dashboard.get('intelligence', {}) if dashboard else {}
                
                # 股票名称
                stock_name = self._get_display_name(result, report_language)
                
                # 标题行：信号等级 + 股票名称
                lines.append(f"### {signal_emoji} **{signal_text}** | {stock_name}({result.code})")
                lines.append("")
                
                # 核心决策（一句话）
                one_sentence = core.get('one_sentence', result.analysis_summary) if core else result.analysis_summary
                if one_sentence:
                    lines.append(f"📌 **{one_sentence[:80]}**")
                    lines.append("")
                
                # 重要信息区（舆情+基本面）
                info_lines = []
                
                # 业绩预期
                if intel.get('earnings_outlook'):
                    outlook = str(intel['earnings_outlook'])[:60]
                    info_lines.append(f"📊 {labels['earnings_outlook_label']}: {outlook}")
                if intel.get('sentiment_summary'):
                    sentiment = str(intel['sentiment_summary'])[:50]
                    info_lines.append(f"💭 {labels['sentiment_summary_label']}: {sentiment}")
                if info_lines:
                    lines.extend(info_lines)
                    lines.append("")
                
                # 风险警报（最重要，醒目显示）
                risks = intel.get('risk_alerts', []) if intel else []
                if risks:
                    lines.append(f"🚨 **{labels['risk_alerts_label']}**:")
                    for risk in risks[:2]:  # 最多显示2条
                        risk_str = str(risk)
                        risk_text = risk_str[:50] + "..." if len(risk_str) > 50 else risk_str
                        lines.append(f"   • {risk_text}")
                    lines.append("")
                
                # 利好催化
                catalysts = intel.get('positive_catalysts', []) if intel else []
                if catalysts:
                    lines.append(f"✨ **{labels['positive_catalysts_label']}**:")
                    for cat in catalysts[:2]:  # 最多显示2条
                        cat_str = str(cat)
                        cat_text = cat_str[:50] + "..." if len(cat_str) > 50 else cat_str
                        lines.append(f"   • {cat_text}")
                    lines.append("")
                
                # 狙击点位
                sniper = battle.get('sniper_points', {}) if battle else {}
                if sniper:
                    ideal_buy = str(sniper.get('ideal_buy', ''))
                    stop_loss = str(sniper.get('stop_loss', ''))
                    take_profit = str(sniper.get('take_profit', ''))
                    points = []
                    if ideal_buy:
                        points.append(f"🎯{labels['ideal_buy_label']}:{ideal_buy[:15]}")
                    if stop_loss:
                        points.append(f"🛑{labels['stop_loss_label']}:{stop_loss[:15]}")
                    if take_profit:
                        points.append(f"🎊{labels['take_profit_label']}:{take_profit[:15]}")
                    if points:
                        lines.append(" | ".join(points))
                        lines.append("")
                
                # 持仓建议
                pos_advice = core.get('position_advice', {}) if core else {}
                if pos_advice:
                    no_pos = str(pos_advice.get('no_position', ''))
                    has_pos = str(pos_advice.get('has_position', ''))
                    if no_pos:
                        lines.append(f"🆕 {labels['no_position_label']}: {no_pos[:50]}")
                    if has_pos:
                        lines.append(f"💼 {labels['has_position_label']}: {has_pos[:50]}")
                    lines.append("")
                
                # 检查清单简化版
                checklist = battle.get('action_checklist', []) if battle else []
                if checklist:
                    # 只显示不通过的项目
                    failed_checks = [str(c) for c in checklist if str(c).startswith('❌') or str(c).startswith('⚠️')]
                    if failed_checks:
                        lines.append(f"**{labels['failed_checks_heading']}**:")
                        for check in failed_checks[:3]:
                            lines.append(f"   {check[:40]}")
                        lines.append("")
                
                lines.append("---")
                lines.append("")
        
        # 底部
        lines.append(f"*{labels['report_time_label']}: {datetime.now().strftime('%H:%M')}*")
        models = self._collect_models_used(results)
        if models:
            lines.append(f"*{labels['analysis_model_label']}: {', '.join(models)}*")

        content = "\n".join(lines)

        return content

    def generate_wechat_summary(self, results: List[AnalysisResult]) -> str:
        """
        生成企业微信精简版日报（控制在4000字符内）

        Args:
            results: 分析结果列表

        Returns:
            精简版 Markdown 内容
        """
        report_date = datetime.now().strftime('%Y-%m-%d')
        report_language = self._get_report_language(results)
        labels = get_report_labels(report_language)

        # 按评分排序
        sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)

        # 统计 - 使用 decision_type 字段准确统计
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))
        avg_score = sum(r.sentiment_score for r in results) / len(results) if results else 0

        lines = [
            f"## 📅 {report_date} {labels['report_title']}",
            "",
            f"> {labels['analyzed_prefix']} **{len(results)}** {labels['stock_unit_compact']} | "
            f"🟢{labels['buy_label']}:{buy_count} 🟡{labels['watch_label']}:{hold_count} 🔴{labels['sell_label']}:{sell_count} | "
            f"{labels['avg_score_label']}:{avg_score:.0f}",
            "",
        ]
        
        # 每只股票精简信息（控制长度）
        for result in sorted_results:
            _, emoji, _ = self._get_signal_level(result)
            
            # 核心信息行
            lines.append(f"### {emoji} {self._get_display_name(result, report_language)}({result.code})")
            lines.append(
                f"**{localize_operation_advice(result.operation_advice, report_language)}** | "
                f"{labels['score_label']}:{result.sentiment_score} | "
                f"{localize_trend_prediction(result.trend_prediction, report_language)}"
            )
            
            # 操作理由（截断）
            if hasattr(result, 'buy_reason') and result.buy_reason:
                reason = result.buy_reason[:80] + "..." if len(result.buy_reason) > 80 else result.buy_reason
                lines.append(f"💡 {reason}")
            
            # 核心看点
            if hasattr(result, 'key_points') and result.key_points:
                points = result.key_points[:60] + "..." if len(result.key_points) > 60 else result.key_points
                lines.append(f"🎯 {points}")
            
            # 风险提示（截断）
            if hasattr(result, 'risk_warning') and result.risk_warning:
                risk = result.risk_warning[:50] + "..." if len(result.risk_warning) > 50 else result.risk_warning
                lines.append(f"⚠️ {risk}")
            
            lines.append("")
        
        # 底部（模型行在 --- 之前，Issue #528）
        models = self._collect_models_used(results)
        if models:
            lines.append(f"*{labels['analysis_model_label']}: {', '.join(models)}*")
        lines.extend([
            "---",
            f"*{labels['not_investment_advice']}*",
            f"*{labels['details_report_hint']} reports/report_{report_date.replace('-', '')}.md*"
        ])

        content = "\n".join(lines)

        return content

    def generate_brief_report(
        self,
        results: List[AnalysisResult],
        report_date: Optional[str] = None,
    ) -> str:
        """
        Generate brief report (3-5 sentences per stock) for mobile/push.

        Args:
            results: Analysis results list (use [result] for single stock).
            report_date: Report date (default: today).

        Returns:
            Brief markdown content.
        """
        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')
        report_language = self._get_report_language(results)
        labels = get_report_labels(report_language)
        config = get_config()
        if getattr(config, 'report_renderer_enabled', False) and results:
            from src.services.report_renderer import render
            out = render(
                platform='brief',
                results=results,
                report_date=report_date,
                summary_only=False,
                extra_context={"report_language": report_language},
            )
            if out:
                return out
        # Fallback: brief summary from dashboard report
        if not results:
            return f"# {report_date} {labels['brief_title']}\n\n{labels['no_results']}"
        sorted_results = sorted(results, key=lambda x: x.sentiment_score, reverse=True)
        buy_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'buy')
        sell_count = sum(1 for r in results if getattr(r, 'decision_type', '') == 'sell')
        hold_count = sum(1 for r in results if getattr(r, 'decision_type', '') in ('hold', ''))
        lines = [
            f"# {report_date} {labels['brief_title']}",
            "",
            f"> {len(results)} {labels['stock_unit_compact']} | 🟢{buy_count} 🟡{hold_count} 🔴{sell_count}",
            "",
        ]
        for r in sorted_results:
            _, emoji, _ = self._get_signal_level(r)
            name = self._get_display_name(r, report_language)
            dash = r.dashboard or {}
            core = dash.get('core_conclusion', {}) or {}
            one = (core.get('one_sentence') or r.analysis_summary or '')[:60]
            lines.append(
                f"**{name}({r.code})** {emoji} "
                f"{localize_operation_advice(r.operation_advice, report_language)} | "
                f"{labels['score_label']} {r.sentiment_score} | {one}"
            )
        lines.append("")
        lines.append(f"*{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
        return "\n".join(lines)

    def generate_single_stock_report(self, result: AnalysisResult) -> str:
        """
        生成单只股票的分析报告（用于单股推送模式 #55）
        
        格式精简但信息完整，适合每分析完一只股票立即推送
        
        Args:
            result: 单只股票的分析结果
            
        Returns:
            Markdown 格式的单股报告
        """
        report_date = datetime.now().strftime('%Y-%m-%d %H:%M')
        report_language = self._get_report_language(result)
        labels = get_report_labels(report_language)
        signal_text, signal_emoji, _ = self._get_signal_level(result)
        dashboard = result.dashboard if hasattr(result, 'dashboard') and result.dashboard else {}
        core = dashboard.get('core_conclusion', {}) if dashboard else {}
        battle = dashboard.get('battle_plan', {}) if dashboard else {}
        intel = dashboard.get('intelligence', {}) if dashboard else {}
        
        # 股票名称（转义 *ST 等特殊字符）
        stock_name = self._get_display_name(result, report_language)
        
        lines = [
            f"## {signal_emoji} {stock_name} ({result.code})",
            "",
            f"> {report_date} | {labels['score_label']}: **{result.sentiment_score}** | {localize_trend_prediction(result.trend_prediction, report_language)}",
            "",
        ]

        self._append_market_snapshot(lines, result)
        self._append_technical_indicator_snapshot(lines, result)
        
        # 核心决策（一句话）
        one_sentence = core.get('one_sentence', result.analysis_summary) if core else result.analysis_summary
        if one_sentence:
            lines.extend([
                f"### 📌 {labels['core_conclusion_heading']}",
                "",
                f"**{signal_text}**: {one_sentence}",
                "",
            ])
        
        # 重要信息（舆情+基本面）
        info_added = False
        if intel:
            if intel.get('earnings_outlook'):
                if not info_added:
                    lines.append(f"### 📰 {labels['info_heading']}")
                    lines.append("")
                    info_added = True
                lines.append(f"📊 **{labels['earnings_outlook_label']}**: {str(intel['earnings_outlook'])[:100]}")
            
            if intel.get('sentiment_summary'):
                if not info_added:
                    lines.append(f"### 📰 {labels['info_heading']}")
                    lines.append("")
                    info_added = True
                lines.append(f"💭 **{labels['sentiment_summary_label']}**: {str(intel['sentiment_summary'])[:80]}")
            
            # 风险警报
            risks = intel.get('risk_alerts', [])
            if risks:
                if not info_added:
                    lines.append(f"### 📰 {labels['info_heading']}")
                    lines.append("")
                    info_added = True
                lines.append("")
                lines.append(f"🚨 **{labels['risk_alerts_label']}**:")
                for risk in risks[:3]:
                    lines.append(f"- {str(risk)[:60]}")
            
            # 利好催化
            catalysts = intel.get('positive_catalysts', [])
            if catalysts:
                lines.append("")
                lines.append(f"✨ **{labels['positive_catalysts_label']}**:")
                for cat in catalysts[:3]:
                    lines.append(f"- {str(cat)[:60]}")
        
        if info_added:
            lines.append("")
        
        # 狙击点位
        sniper = battle.get('sniper_points', {}) if battle else {}
        if sniper:
            lines.extend([
                f"### 🎯 {labels['action_points_heading']}",
                "",
                f"| {labels['ideal_buy_label']} | {labels['stop_loss_label']} | {labels['take_profit_label']} |",
                "|------|------|------|",
            ])
            ideal_buy = sniper.get('ideal_buy', '-')
            stop_loss = sniper.get('stop_loss', '-')
            take_profit = sniper.get('take_profit', '-')
            lines.append(f"| {ideal_buy} | {stop_loss} | {take_profit} |")
            lines.append("")
        
        # 持仓建议
        pos_advice = core.get('position_advice', {}) if core else {}
        if pos_advice:
            lines.extend([
                f"### 💼 {labels['position_advice_heading']}",
                "",
                f"- 🆕 **{labels['no_position_label']}**: {pos_advice.get('no_position', localize_operation_advice(result.operation_advice, report_language))}",
                f"- 💼 **{labels['has_position_label']}**: {pos_advice.get('has_position', labels['continue_holding'])}",
                "",
            ])
        
        lines.append("---")
        model_used = normalize_model_used(getattr(result, "model_used", None))
        if model_used:
            lines.append(f"*{labels['analysis_model_label']}: {model_used}*")
        lines.append(f"*{labels['not_investment_advice']}*")

        return "\n".join(lines)

    # Display name mapping for realtime data sources
    _SOURCE_DISPLAY_NAMES = {
        "tencent": {"zh": "腾讯财经", "en": "Tencent Finance"},
        "akshare_em": {"zh": "东方财富", "en": "Eastmoney"},
        "akshare_sina": {"zh": "新浪财经", "en": "Sina Finance"},
        "akshare_qq": {"zh": "腾讯财经", "en": "Tencent Finance"},
        "efinance": {"zh": "东方财富(efinance)", "en": "Eastmoney (efinance)"},
        "tushare": {"zh": "Tushare Pro", "en": "Tushare Pro"},
        "sina": {"zh": "新浪财经", "en": "Sina Finance"},
        "stooq": {"zh": "Stooq", "en": "Stooq"},
        "longbridge": {"zh": "长桥", "en": "Longbridge"},
        "fallback": {"zh": "降级兜底", "en": "Fallback"},
    }

    def _get_source_display_name(self, source: Any, language: Optional[str]) -> str:
        raw_source = str(source or "N/A")
        mapping = self._SOURCE_DISPLAY_NAMES.get(raw_source)
        if not mapping:
            return raw_source
        return mapping[normalize_report_language(language)]

    def _append_market_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'market_snapshot', None)
        if not snapshot:
            return

        report_language = self._get_report_language(result)
        labels = get_report_labels(report_language)

        lines.extend([
            f"### 📈 {labels['market_snapshot_heading']}",
            "",
            f"| {labels['close_label']} | {labels['prev_close_label']} | {labels['open_label']} | {labels['high_label']} | {labels['low_label']} | {labels['change_pct_label']} | {labels['change_amount_label']} | {labels['amplitude_label']} | {labels['volume_label']} | {labels['amount_label']} |",
            "|------|------|------|------|------|-------|-------|------|--------|--------|",
            f"| {snapshot.get('close', 'N/A')} | {snapshot.get('prev_close', 'N/A')} | "
            f"{snapshot.get('open', 'N/A')} | {snapshot.get('high', 'N/A')} | "
            f"{snapshot.get('low', 'N/A')} | {snapshot.get('pct_chg', 'N/A')} | "
            f"{snapshot.get('change_amount', 'N/A')} | {snapshot.get('amplitude', 'N/A')} | "
            f"{snapshot.get('volume', 'N/A')} | {snapshot.get('amount', 'N/A')} |",
        ])

        if "price" in snapshot:
            display_source = self._get_source_display_name(snapshot.get('source', 'N/A'), report_language)
            lines.extend([
                "",
                f"| {labels['current_price_label']} | {labels['volume_ratio_label']} | {labels['turnover_rate_label']} | {labels['source_label']} |",
                "|-------|------|--------|----------|",
                f"| {snapshot.get('price', 'N/A')} | {snapshot.get('volume_ratio', 'N/A')} | "
                f"{snapshot.get('turnover_rate', 'N/A')} | {display_source} |",
            ])

        lines.append("")

    def _append_technical_indicator_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'technical_indicator_snapshot', None)
        if not snapshot:
            return

        def _num(key: str, digits: int = 2) -> str:
            value = snapshot.get(key)
            if value is None or value == "":
                return "N/A"
            try:
                return f"{float(value):.{digits}f}"
            except (TypeError, ValueError):
                return str(value)

        rows = [
            ("EMA10", _num("ema10"), "短线趋势与动量"),
            ("MA50", _num("ma50"), "中期趋势参考"),
            ("MA200", _num("ma200"), "长期趋势/牛熊分界参考"),
            ("Bollinger 中轨", _num("boll_mid"), "20日均值"),
            ("Bollinger 上轨", _num("boll_upper"), "价格波动上沿"),
            ("Bollinger 下轨", _num("boll_lower"), "价格波动下沿"),
            ("ATR14", _num("atr14"), "近14日真实波幅"),
            ("VWMA20", _num("vwma20"), "20日成交量加权均价"),
            ("MFI14", _num("mfi14"), "资金流指标，0-100"),
        ]

        lines.extend([
            "### 📐 扩展技术指标",
            "",
            "| 指标 | 数值 | 说明 |",
            "|------|------|------|",
        ])
        for label, value, note in rows:
            lines.append(f"| {label} | {value} | {note} |")

        source = snapshot.get("source") or "StockTrendAnalyzer"
        lines.extend([
            "",
            f"> 来源：{source}。这些指标用于补充趋势、波动率和资金流观察。",
            "",
        ])

    def _append_news_context_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        text = getattr(result, 'news_context_snapshot', None)
        if not text:
            return
        text = str(text).strip()
        if not text:
            return

        sections = self._parse_news_context_sections(text)
        if sections:
            lines.extend([
                "### 🗞️ 搜索情报摘要",
                "",
                "> 这里展示每类搜索源命中的核心标题；长摘要和网页正文不再直接塞进报告，避免干扰阅读。",
                "",
                "| 模块 | 来源 | 核心命中 |",
                "|------|------|----------|",
            ])
            for section in sections[:8]:
                section_title = str(section.get("title") or "")
                if self._should_skip_display_item(section_title):
                    continue
                items = section.get("items") or []
                if not items:
                    continue
                item_text = "<br>".join(
                    f"{idx + 1}. {self._sanitize_table_text(item)}"
                    for idx, item in enumerate(items[:3])
                )
                lines.append(
                    f"| {self._sanitize_table_text(section.get('title'))} | "
                    f"{self._sanitize_table_text(section.get('source'))} | {item_text} |"
                )
            lines.append("")
            return

        max_chars = 1200
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars].rstrip() + "\n... (已截断)"
        text = self._clean_news_context_text(text).replace("```", "'''")

        lines.extend([
            "### 🗞️ 搜索情报摘要",
            "",
            "```text",
            text,
            "```",
            "",
        ])

    @classmethod
    def _parse_news_context_sections(cls, text: str) -> List[Dict[str, Any]]:
        sections: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("【") and line.endswith("】"):
                continue
            header_match = re.match(r"^[^\w\s]?\s*(.+?)\s*\(来源:\s*([^)]+)\):$", line)
            if header_match:
                current = {
                    "title": header_match.group(1).strip(),
                    "source": header_match.group(2).strip(),
                    "items": [],
                }
                sections.append(current)
                continue
            item_match = re.match(r"^\d+\.\s+(.+)$", line)
            if not item_match:
                item_match = re.match(r"^\s*\d+\.\s+(.+)$", raw_line)
            if current is not None and item_match:
                item = cls._clean_news_context_text(item_match.group(1))
                if item:
                    current["items"].append(item)
        return [section for section in sections if section.get("items")]

    @staticmethod
    def _clean_news_context_text(text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = cleaned.replace("&nbsp;", " ")
        cleaned = cleaned.replace("[...]", "")
        cleaned = cleaned.replace("#", "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @classmethod
    def _sanitize_table_text(cls, value: Any, limit: int = 140) -> str:
        text = cls._clean_news_context_text(str(value or "N/A")).replace("|", "\\|")
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    @staticmethod
    def _should_skip_display_item(value: Any) -> bool:
        text = str(value or "").lower()
        skip_tokens = (
            "sec form 144",
            "form 144",
            "sec 内部人",
            "内部人交易",
            "内部人申报",
            "insider transaction",
            "insider trading",
            "insider filing",
        )
        return any(token in text for token in skip_tokens)

    def _append_filing_references(self, lines: List[str], result: AnalysisResult) -> None:
        refs = getattr(result, 'filing_references', None)
        if not refs:
            return

        lines.extend([
            "### 📄 财报原文链接",
            "",
            "| 类型 | 报告期 | 提交日期 | 链接 |",
            "|------|--------|----------|------|",
        ])
        for ref in refs[:5]:
            if not isinstance(ref, dict):
                continue
            links = self._build_sec_link_list(ref)
            if not links:
                continue
            form = ref.get("form") or "SEC filing"
            report_date = ref.get("report_date") or "N/A"
            filing_date = ref.get("filing_date") or "N/A"
            lines.append(f"| {form} | {report_date} | {filing_date} | {' / '.join(links)} |")
        lines.extend([
            "",
            "> 链接优先使用 SEC Archives 原文直链；“详情”是 SEC filing package 页面，可作为备用入口。",
            "",
        ])

    def _append_insider_activity_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'insider_activity_snapshot', None)
        if not snapshot:
            return
        filings = snapshot.get("recent_filings")
        if not isinstance(filings, list) or not filings:
            return

        lines.extend([
            "### 🧾 SEC 内部人申报",
            "",
            "| 类型 | 报告期 | 提交日期 | 链接 |",
            "|------|--------|----------|------|",
        ])
        for filing in filings[:6]:
            if not isinstance(filing, dict):
                continue
            links = self._build_sec_link_list(filing)
            if not links:
                continue
            lines.append(
                f"| {filing.get('form', 'N/A')} | {filing.get('report_date', 'N/A')} | "
                f"{filing.get('filing_date', 'N/A')} | {' / '.join(links)} |"
            )
        lines.extend([
            "",
            "> Form 4 通常用于内部人持股变动披露；买卖方向需进入 SEC 原文查看交易代码与数量。",
            "",
        ])

    @classmethod
    def _build_sec_link_list(cls, filing: Dict[str, Any]) -> List[str]:
        candidates = [
            ("PDF", filing.get("pdf_url")),
            ("SEC 原文", filing.get("document_url") or filing.get("sec_url") or filing.get("url")),
            ("详情", filing.get("filing_detail_url")),
        ]
        links: List[str] = []
        seen = set()
        for label, url in candidates:
            normalized = cls._normalize_sec_archive_url(url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            links.append(f"[{label}]({normalized})")
        return links

    @staticmethod
    def _normalize_sec_archive_url(url: Any) -> str:
        """Convert fragile SEC ixviewer URLs to direct Archives document URLs."""
        text = str(url or "").strip()
        if not text:
            return ""
        if "/ixviewer/doc/action" not in text or "doc=" not in text:
            return text
        parsed = urllib.parse.urlparse(text)
        doc = urllib.parse.parse_qs(parsed.query).get("doc", [""])[0]
        doc = urllib.parse.unquote(doc)
        if doc.startswith("/Archives/"):
            return f"https://www.sec.gov{doc}"
        return text

    def _append_fundamental_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'fundamental_snapshot', None)
        if not snapshot:
            return

        def _value(key: str) -> str:
            value = snapshot.get(key)
            if value is None or value == "":
                return "N/A"
            return str(value)

        source = _value("source")
        lines.extend([
            "### 📑 财报摘要",
            "",
            f"**最近报告**: {_value('form')}，报告期 {_value('report_date')}，提交日期 {_value('filing_date')}",
            "",
            "| 指标 | 数值 | 口径/说明 |",
            "|------|------|-----------|",
            f"| 营业收入 | {_value('revenue')} | {_value('revenue_period')} |",
            f"| 归母净利润 | {_value('net_profit_parent')} | {_value('net_profit_parent_period')} |",
            f"| 经营现金流 | {_value('operating_cash_flow')} | {_value('operating_cash_flow_period')} |",
            f"| ROE | {_value('roe')} | {_value('roe_note')} |",
            f"| 总资产 | {_value('assets')} | {source} |",
            f"| 总负债 | {_value('liabilities')} | {source} / 资产-权益推算 |",
            f"| 股东权益 | {_value('shareholders_equity')} | {source} |",
            f"| 摊薄 EPS | {_value('eps_diluted')} | {source} |",
            f"| 现金及可变现证券 | {_value('liquid_assets')} | 现金等价物 + 流动/非流动可交易证券 |",
            f"| 有息债务 | {_value('interest_bearing_debt')} | 商业票据 + 长期债务当期/非当期部分 |",
            f"| 净现金/净债务 | {_value('net_cash')} | 现金及可变现证券 - 有息债务 |",
            "",
        ])
        self._append_financial_trend_tables(lines, snapshot)

    def _append_financial_trend_tables(self, lines: List[str], snapshot: Dict[str, Any]) -> None:
        quarterly = snapshot.get("quarterly_trend")
        if isinstance(quarterly, list) and quarterly:
            has_derived_row = any(isinstance(row, dict) and row.get("derived") for row in quarterly)
            revenue_values = [row.get("revenue_value") for row in reversed(quarterly) if isinstance(row, dict)]
            profit_values = [row.get("net_profit_parent_value") for row in reversed(quarterly) if isinstance(row, dict)]
            margin_values = [row.get("net_margin_pct") for row in reversed(quarterly) if isinstance(row, dict)]
            fcf_values = [row.get("free_cash_flow_value") for row in reversed(quarterly) if isinstance(row, dict)]

            lines.extend([
                "#### 最近季度财务趋势",
                "",
                "> 趋势图为迷你图，左旧右新；较前值表示相对前一披露季度/期间的变化，YoY 表示相对上一年同季。"
                + ("带 * 的期间为累计值拆分或年报减 YTD 推算。" if has_derived_row else ""),
                "",
                "| 指标 | 趋势图 | 最新值 |",
                "|------|--------|--------|",
                f"| 收入 | {self._sparkline(revenue_values)} | {self._display_trend_latest(quarterly, 'revenue')} |",
                f"| 净利润 | {self._sparkline(profit_values)} | {self._display_trend_latest(quarterly, 'net_profit_parent')} |",
                f"| 净利率 | {self._sparkline(margin_values)} | {self._display_trend_latest_pct(quarterly, 'net_margin_pct')} |",
                f"| 自由现金流 | {self._sparkline(fcf_values)} | {self._display_trend_latest(quarterly, 'free_cash_flow')} |",
                "",
                "| 期间 | 收入 | 较前值 | YoY | 净利润 | 较前值 | YoY | 净利率 | FCF | EPS |",
                "|------|------|--------|-----|--------|--------|-----|--------|-----|-----|",
            ])
            for row in quarterly[:5]:
                if not isinstance(row, dict):
                    continue
                period = str(row.get('period', 'N/A'))
                if row.get("derived"):
                    period = f"{period}*"
                lines.append(
                    f"| {period} | {row.get('revenue', 'N/A')} | "
                    f"{self._format_change_pct(row.get('revenue_value_change_pct'))} | "
                    f"{self._format_change_pct(row.get('revenue_value_yoy_pct'))} | "
                    f"{row.get('net_profit_parent', 'N/A')} | "
                    f"{self._format_change_pct(row.get('net_profit_parent_value_change_pct'))} | "
                    f"{self._format_change_pct(row.get('net_profit_parent_value_yoy_pct'))} | "
                    f"{self._fmt_pct(self._to_float(row.get('net_margin_pct')))} | "
                    f"{row.get('free_cash_flow', 'N/A')} | {row.get('eps_diluted', 'N/A')} |"
                )
            lines.append("")

        annual = snapshot.get("annual_trend")
        if isinstance(annual, list) and annual:
            lines.extend([
                "#### 最近年度财务趋势",
                "",
                "| 年度 | 收入 | 较前值 | 净利润 | 较前值 | 净利率 | FCF |",
                "|------|------|--------|--------|--------|--------|-----|",
            ])
            for row in annual[:4]:
                if not isinstance(row, dict):
                    continue
                lines.append(
                    f"| {row.get('period', 'N/A')} | {row.get('revenue', 'N/A')} | "
                    f"{self._format_change_pct(row.get('revenue_value_change_pct'))} | "
                    f"{row.get('net_profit_parent', 'N/A')} | "
                    f"{self._format_change_pct(row.get('net_profit_parent_value_change_pct'))} | "
                    f"{self._fmt_pct(self._to_float(row.get('net_margin_pct')))} | "
                    f"{row.get('free_cash_flow', 'N/A')} |"
                )
            lines.append("")

    def _append_financial_statement_analysis(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'fundamental_snapshot', None)
        if not snapshot:
            return

        dividend = getattr(result, 'dividend_snapshot', None) or {}
        peer = getattr(result, 'peer_valuation_snapshot', None) or {}
        insider = getattr(result, 'insider_activity_snapshot', None) or {}

        revenue = self._financial_number(snapshot, "revenue_value", "revenue")
        net_income = self._financial_number(snapshot, "net_profit_parent_value", "net_profit_parent")
        ocf = self._financial_number(snapshot, "operating_cash_flow_value", "operating_cash_flow")
        capex = self._financial_number(snapshot, "capital_expenditure_value", "capital_expenditure")
        fcf = self._financial_number(snapshot, "free_cash_flow_value", "free_cash_flow")
        assets = self._financial_number(snapshot, "assets_value", "assets")
        liabilities = self._financial_number(snapshot, "liabilities_value", "liabilities")
        equity = self._financial_number(snapshot, "shareholders_equity_value", "shareholders_equity")
        liquid_assets = self._financial_number(snapshot, "liquid_assets_value", "liquid_assets")
        interest_bearing_debt = self._financial_number(
            snapshot,
            "interest_bearing_debt_value",
            "interest_bearing_debt",
        )
        net_cash = self._financial_number(snapshot, "net_cash_value", "net_cash")
        if liabilities is None and assets is not None and equity is not None:
            liabilities = assets - equity

        net_margin = self._financial_pct(snapshot, "net_margin_pct", net_income, revenue)
        ocf_to_ni = self._financial_pct(
            snapshot,
            "operating_cash_flow_to_net_income_pct",
            ocf,
            net_income,
        )
        fcf_to_ni = self._financial_pct(
            snapshot,
            "free_cash_flow_to_net_income_pct",
            fcf,
            net_income,
        )
        debt_to_assets = self._financial_pct(snapshot, "debt_to_assets_pct", liabilities, assets)
        interest_bearing_debt_to_assets = self._financial_pct(
            snapshot,
            "interest_bearing_debt_to_assets_pct",
            interest_bearing_debt,
            assets,
        )
        liquid_assets_to_debt = self._financial_pct(
            snapshot,
            "liquid_assets_to_interest_bearing_debt_pct",
            liquid_assets,
            interest_bearing_debt,
        )
        equity_ratio = self._financial_pct(snapshot, "equity_ratio_pct", equity, assets)
        roe = self._parse_percent(snapshot.get("roe"))

        green_flags: List[str] = []
        yellow_flags: List[str] = []

        if net_margin is not None:
            if net_margin >= 20:
                green_flags.append(f"净利率约 {net_margin:.2f}%，盈利能力强")
            elif net_margin < 8:
                yellow_flags.append(f"净利率约 {net_margin:.2f}%，利润率偏低")
        if roe is not None:
            if roe >= 15:
                green_flags.append(f"ROE 约 {roe:.2f}%，资本回报水平较高")
            elif roe < 10:
                yellow_flags.append(f"ROE 约 {roe:.2f}%，资本效率一般")
        if ocf_to_ni is not None:
            if ocf_to_ni >= 100:
                green_flags.append(f"经营现金流/净利润约 {ocf_to_ni:.2f}%，利润现金含量强")
            elif ocf_to_ni < 80:
                yellow_flags.append(f"经营现金流/净利润约 {ocf_to_ni:.2f}%，需解释现金转化不足")
        if fcf_to_ni is not None:
            if fcf_to_ni >= 80:
                green_flags.append(f"自由现金流/净利润约 {fcf_to_ni:.2f}%，可分配现金流质量较好")
            elif fcf_to_ni < 50:
                yellow_flags.append(f"自由现金流/净利润约 {fcf_to_ni:.2f}%，资本开支后现金留存偏弱")
        elif capex is None:
            yellow_flags.append("资本开支数据缺失，暂不能完整判断自由现金流质量")
        if debt_to_assets is not None:
            if debt_to_assets > 70:
                yellow_flags.append(f"总负债率约 {debt_to_assets:.2f}%，但这不是有息债务率，需拆分经营性负债和债务")
            elif debt_to_assets < 50:
                green_flags.append(f"资产负债率约 {debt_to_assets:.2f}%，资产负债表较保守")
        if interest_bearing_debt_to_assets is not None:
            if interest_bearing_debt_to_assets < 30:
                green_flags.append(f"有息债务/资产约 {interest_bearing_debt_to_assets:.2f}%，债务压力相对可控")
            elif interest_bearing_debt_to_assets > 50:
                yellow_flags.append(f"有息债务/资产约 {interest_bearing_debt_to_assets:.2f}%，需重点跟踪偿债压力")
        if net_cash is not None and net_cash > 0:
            green_flags.append(f"现金及可变现证券扣除有息债务后仍为净现金 {self._format_money_value(net_cash)}")

        if isinstance(dividend, dict) and dividend.get("ttm_event_count") not in (None, "", "N/A", 0):
            green_flags.append(f"近12个月有 {dividend.get('ttm_event_count')} 次现金分红，股东回报有持续性")

        peer_summary = peer.get("summary") if isinstance(peer, dict) else None
        if isinstance(peer_summary, dict):
            pe_vs = peer_summary.get("pe_ratio_vs_peer_median_pct")
            pb_vs = peer_summary.get("pb_ratio_vs_peer_median_pct")
            try:
                if pe_vs is not None and float(pe_vs) > 50:
                    yellow_flags.append(f"PE 相对 peer 中位数溢价约 {float(pe_vs):.2f}%，估值已较充分")
            except (TypeError, ValueError):
                pass
            try:
                if pb_vs is not None and float(pb_vs) > 100:
                    yellow_flags.append(f"PB 相对 peer 中位数溢价约 {float(pb_vs):.2f}%，需用品牌/生态/回购解释")
            except (TypeError, ValueError):
                pass

        if not green_flags:
            green_flags.append("暂无足够数据形成明确绿灯信号")
        if not yellow_flags:
            yellow_flags.append("暂未发现财报层面的明显红旗，仍需跟踪后续季度变化")

        revenue_text = self._display_money(snapshot, 'revenue')
        net_income_text = self._display_money(snapshot, 'net_profit_parent')
        ocf_text = self._display_money(snapshot, 'operating_cash_flow')
        fcf_text = self._format_money_value(fcf) if fcf is not None else self._display_money(snapshot, 'free_cash_flow')
        assets_text = self._display_money(snapshot, 'assets')
        liabilities_text = self._format_money_value(liabilities) if liabilities is not None else self._display_money(snapshot, 'liabilities')
        liquid_assets_text = self._format_money_value(liquid_assets) if liquid_assets is not None else self._display_money(snapshot, 'liquid_assets')
        interest_debt_text = self._format_money_value(interest_bearing_debt) if interest_bearing_debt is not None else self._display_money(snapshot, 'interest_bearing_debt')
        net_cash_text = self._format_money_value(net_cash) if net_cash is not None else self._display_money(snapshot, 'net_cash')
        dividend_yield = dividend.get('ttm_dividend_yield_pct', 'N/A')
        if isinstance(dividend_yield, (int, float)):
            dividend_yield = f"{float(dividend_yield):.4f}%"

        lines.extend([
            "### 🧮 财报分析",
            "",
            "> 方法论：结合科技股财报深挖的收入/盈利/现金流/资本配置模块、价值投资四维评分（ROE、债务安全、自由现金流质量、护城河）以及 SEC 对三张表和 MD&A 的阅读框架。",
            "",
            "| 分析维度 | 关键数据 | 解读 |",
            "|----------|----------|------|",
            f"| 收入与盈利质量 | 收入 {revenue_text}；净利润 {net_income_text}；净利率 {self._fmt_pct(net_margin)} | {self._profitability_comment(net_margin)} |",
            f"| 现金流质量 | 经营现金流 {ocf_text}；OCF/净利润 {self._fmt_pct(ocf_to_ni)}；自由现金流 {fcf_text}；FCF/净利润 {self._fmt_pct(fcf_to_ni)} | {self._cash_quality_comment(ocf_to_ni, fcf_to_ni)} |",
            f"| 资产负债表 | 总资产 {assets_text}；总负债 {liabilities_text}；总负债率 {self._fmt_pct(debt_to_assets)}；有息债务 {interest_debt_text}；有息债务/资产 {self._fmt_pct(interest_bearing_debt_to_assets)}；现金及可变现证券 {liquid_assets_text}；净现金/净债务 {net_cash_text} | {self._balance_sheet_comment(debt_to_assets, equity_ratio, interest_bearing_debt_to_assets, liquid_assets_to_debt, net_cash)} |",
            f"| 资本效率 | ROE {snapshot.get('roe') or 'N/A'}；摊薄 EPS {snapshot.get('eps_diluted') or 'N/A'} | {self._roe_comment(roe)} |",
            f"| 股东回报 | TTM 分红 {dividend.get('ttm_cash_dividend_per_share', 'N/A')}；股息率 {dividend_yield}；事件数 {dividend.get('ttm_event_count', 'N/A')} | 分红用于确认现金回报，但对成长/回购型公司不应单独作为估值锚。 |",
            "",
            "**绿灯信号**:",
        ])
        for item in green_flags[:6]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("**红旗/黄旗**:")
        for item in yellow_flags[:5]:
            lines.append(f"- {item}")
        lines.append("")

    def _append_integrated_research_framework(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'fundamental_snapshot', None) or {}
        peer = getattr(result, 'peer_valuation_snapshot', None) or {}
        macro = getattr(result, 'macro_snapshot', None) or {}
        technical = getattr(result, 'technical_indicator_snapshot', None) or {}
        if not any(isinstance(item, dict) and item for item in (snapshot, peer, macro, technical)):
            return

        framework = self._build_integrated_framework(snapshot, peer, macro, technical, result)
        lines.extend([
            "### 🧭 三框架综合诊断",
            "",
            "> 聚合方法：科技股财报深挖 + 美国价值投资四维 + 美国市场情绪/风险预算。这里先给研究框架和硬数据判断，不能替代个性化投资建议。",
            "",
            "#### 关键力量",
            "",
            "| 关键力量 | 当前证据 | 判断 | 后续观察 |",
            "|----------|----------|------|----------|",
        ])
        for row in framework["key_forces"]:
            lines.append(
                f"| {row['force']} | {row['evidence']} | {row['judgement']} | {row['watch']} |"
            )

        lines.extend([
            "",
            "#### 价值投资四维评分",
            "",
            "| 维度 | 数据 | 得分 | 解读 |",
            "|------|------|------|------|",
        ])
        for row in framework["value_scores"]:
            lines.append(
                f"| {row['dimension']} | {row['data']} | {row['score']} | {row['comment']} |"
            )
        lines.extend([
            "",
            f"**价值投资评级**: {framework['value_rating']}（{framework['value_total']} / 12）",
            "",
            "#### 市场情绪与风险预算",
            "",
            "| 模块 | 当前信号 | 风险含义 |",
            "|------|----------|----------|",
        ])
        for row in framework["risk_budget_rows"]:
            lines.append(f"| {row['module']} | {row['signal']} | {row['meaning']} |")
        lines.extend([
            "",
            f"**风险预算倾向**: {framework['risk_budget']}。{framework['risk_budget_reason']}",
            "",
        ])

    @staticmethod
    def _display_money(snapshot: Dict[str, Any], key: str) -> str:
        value = snapshot.get(key)
        if value not in (None, ""):
            return str(value)
        value = snapshot.get(f"{key}_value")
        if value is None:
            return "N/A"
        return NotificationService._format_money_value(value)

    @staticmethod
    def _format_money_value(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "N/A"
        sign = "-" if number < 0 else ""
        number = abs(number)
        if number >= 1_000_000_000:
            return f"{sign}${number / 1_000_000_000:.2f}B"
        if number >= 1_000_000:
            return f"{sign}${number / 1_000_000:.2f}M"
        return f"{sign}${number:,.0f}"

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_change_pct(value: Any) -> str:
        number = NotificationService._to_float(value)
        if number is None:
            return "N/A"
        arrow = "▲" if number > 0 else "▼" if number < 0 else "→"
        return f"{arrow} {number:+.2f}%"

    @staticmethod
    def _sparkline(values: List[Any]) -> str:
        numbers = [NotificationService._to_float(value) for value in values]
        clean = [value for value in numbers if value is not None]
        if len(clean) < 2:
            return "N/A"
        low = min(clean)
        high = max(clean)
        chars = "▁▂▃▄▅▆▇█"
        if high == low:
            return chars[len(chars) // 2] * len(clean)
        points = []
        for value in clean:
            idx = int(round((value - low) / (high - low) * (len(chars) - 1)))
            points.append(chars[max(0, min(idx, len(chars) - 1))])
        return "".join(points)

    @staticmethod
    def _display_trend_latest(rows: List[Dict[str, Any]], key: str) -> str:
        if not rows:
            return "N/A"
        value = rows[0].get(key)
        if value not in (None, ""):
            return str(value)
        raw = rows[0].get(f"{key}_value")
        return NotificationService._format_money_value(raw) if raw is not None else "N/A"

    @staticmethod
    def _display_trend_latest_pct(rows: List[Dict[str, Any]], key: str) -> str:
        if not rows:
            return "N/A"
        return NotificationService._fmt_pct(NotificationService._to_float(rows[0].get(key)))

    def _build_integrated_framework(
        self,
        snapshot: Dict[str, Any],
        peer: Dict[str, Any],
        macro: Dict[str, Any],
        technical: Dict[str, Any],
        result: AnalysisResult,
    ) -> Dict[str, Any]:
        latest_trend = None
        quarterly = snapshot.get("quarterly_trend")
        if isinstance(quarterly, list) and quarterly:
            latest_trend = quarterly[0] if isinstance(quarterly[0], dict) else None

        revenue_change = latest_trend.get("revenue_value_change_pct") if latest_trend else None
        profit_change = latest_trend.get("net_profit_parent_value_change_pct") if latest_trend else None
        net_margin = self._financial_pct(
            snapshot,
            "net_margin_pct",
            self._financial_number(snapshot, "net_profit_parent_value", "net_profit_parent"),
            self._financial_number(snapshot, "revenue_value", "revenue"),
        )
        roe = self._parse_percent(snapshot.get("roe"))
        debt_to_assets = self._financial_pct(
            snapshot,
            "debt_to_assets_pct",
            self._financial_number(snapshot, "liabilities_value", "liabilities"),
            self._financial_number(snapshot, "assets_value", "assets"),
        )
        interest_debt_to_assets = self._financial_pct(
            snapshot,
            "interest_bearing_debt_to_assets_pct",
            self._financial_number(snapshot, "interest_bearing_debt_value", "interest_bearing_debt"),
            self._financial_number(snapshot, "assets_value", "assets"),
        )
        liquid_assets_to_debt = self._financial_pct(
            snapshot,
            "liquid_assets_to_interest_bearing_debt_pct",
            self._financial_number(snapshot, "liquid_assets_value", "liquid_assets"),
            self._financial_number(snapshot, "interest_bearing_debt_value", "interest_bearing_debt"),
        )
        net_cash = self._financial_number(snapshot, "net_cash_value", "net_cash")
        ocf_to_ni = self._financial_pct(
            snapshot,
            "operating_cash_flow_to_net_income_pct",
            self._financial_number(snapshot, "operating_cash_flow_value", "operating_cash_flow"),
            self._financial_number(snapshot, "net_profit_parent_value", "net_profit_parent"),
        )
        fcf_to_ni = self._financial_pct(
            snapshot,
            "free_cash_flow_to_net_income_pct",
            self._financial_number(snapshot, "free_cash_flow_value", "free_cash_flow"),
            self._financial_number(snapshot, "net_profit_parent_value", "net_profit_parent"),
        )

        peer_summary = peer.get("summary") if isinstance(peer, dict) else {}
        pe_vs = self._to_float((peer_summary or {}).get("pe_ratio_vs_peer_median_pct"))
        pb_vs = self._to_float((peer_summary or {}).get("pb_ratio_vs_peer_median_pct"))

        key_forces = [
            {
                "force": "收入与利润动能",
                "evidence": (
                    f"收入较前值 {self._format_change_pct(revenue_change)}；"
                    f"净利润较前值 {self._format_change_pct(profit_change)}；"
                    f"净利率 {self._fmt_pct(net_margin)}"
                ),
                "judgement": self._growth_force_comment(revenue_change, profit_change, net_margin),
                "watch": "下一季收入增速、净利率和管理层指引是否同向改善",
            },
            {
                "force": "利润现金含量",
                "evidence": f"OCF/净利润 {self._fmt_pct(ocf_to_ni)}；FCF/净利润 {self._fmt_pct(fcf_to_ni)}",
                "judgement": self._cash_quality_comment(ocf_to_ni, fcf_to_ni),
                "watch": "经营现金流、CapEx、库存/应收变化和回购资金来源",
            },
            {
                "force": "估值隐含预期",
                "evidence": f"PE 相对 peer {self._fmt_pct(pe_vs)}；PB 相对 peer {self._fmt_pct(pb_vs)}",
                "judgement": self._valuation_force_comment(pe_vs, pb_vs),
                "watch": "增长兑现速度是否足以支撑相对估值溢价",
            },
        ]

        value_scores = [
            self._score_value_dimension(
                "ROE 可持续性",
                f"ROE {snapshot.get('roe') or 'N/A'}",
                self._score_roe(roe),
                self._roe_comment(roe),
            ),
            self._score_value_dimension(
                "债务安全性",
                f"有息债务/资产 {self._fmt_pct(interest_debt_to_assets)}；总负债/资产 {self._fmt_pct(debt_to_assets)}；净现金/净债务 {self._format_money_value(net_cash) if net_cash is not None else 'N/A'}",
                self._score_debt_safety(interest_debt_to_assets if interest_debt_to_assets is not None else debt_to_assets),
                self._balance_sheet_comment(
                    debt_to_assets,
                    None,
                    interest_debt_to_assets,
                    liquid_assets_to_debt,
                    net_cash,
                ),
            ),
            self._score_value_dimension(
                "自由现金流质量",
                f"FCF/净利润 {self._fmt_pct(fcf_to_ni)}；OCF/净利润 {self._fmt_pct(ocf_to_ni)}",
                self._score_cash_quality(fcf_to_ni, ocf_to_ni),
                self._cash_quality_comment(ocf_to_ni, fcf_to_ni),
            ),
            self._score_value_dimension(
                "经济护城河初判",
                f"净利率 {self._fmt_pct(net_margin)}；ROE {self._fmt_pct(roe)}；相对估值溢价 {self._fmt_pct(pe_vs)}",
                self._score_moat_proxy(net_margin, roe, pe_vs),
                "这是财务代理指标，仍需结合品牌、网络效应、转换成本和竞争格局验证。",
            ),
        ]
        value_total = sum(row["_score_raw"] for row in value_scores)
        value_rating = self._value_rating(value_total)
        for row in value_scores:
            row.pop("_score_raw", None)

        risk_budget_rows, risk_points = self._risk_budget_rows(macro, technical, peer_summary)
        if risk_points >= 3:
            risk_budget = "降低/控制"
            risk_reason = "估值、利率或技术热度中已有多项脆弱性信号，适合小仓或等待更好赔率。"
        elif risk_points <= 1:
            risk_budget = "可维持/适度提高"
            risk_reason = "宏观和技术风险信号不拥挤，但仍要以个股基本面和止损为主。"
        else:
            risk_budget = "维持"
            risk_reason = "机会和风险较均衡，更适合分批和条件触发式操作。"

        return {
            "key_forces": key_forces,
            "value_scores": value_scores,
            "value_total": value_total,
            "value_rating": value_rating,
            "risk_budget_rows": risk_budget_rows,
            "risk_budget": risk_budget,
            "risk_budget_reason": risk_reason,
        }

    @staticmethod
    def _score_value_dimension(dimension: str, data: str, score: int, comment: str) -> Dict[str, Any]:
        return {
            "dimension": dimension,
            "data": data,
            "score": f"{score}/3",
            "comment": comment,
            "_score_raw": score,
        }

    @staticmethod
    def _score_roe(roe: Optional[float]) -> int:
        if roe is None:
            return 0
        if roe > 20:
            return 3
        if roe > 15:
            return 2
        if roe >= 10:
            return 1
        return 0

    @staticmethod
    def _score_debt_safety(debt_to_assets: Optional[float]) -> int:
        if debt_to_assets is None:
            return 0
        if debt_to_assets < 30:
            return 3
        if debt_to_assets < 50:
            return 2
        if debt_to_assets <= 70:
            return 1
        return 0

    @staticmethod
    def _score_cash_quality(fcf_to_ni: Optional[float], ocf_to_ni: Optional[float]) -> int:
        primary = fcf_to_ni if fcf_to_ni is not None else ocf_to_ni
        if primary is None:
            return 0
        if primary >= 100:
            return 3
        if primary >= 80:
            return 2
        if primary >= 50:
            return 1
        return 0

    @staticmethod
    def _score_moat_proxy(net_margin: Optional[float], roe: Optional[float], pe_vs: Optional[float]) -> int:
        score = 0
        if net_margin is not None and net_margin >= 20:
            score += 1
        if roe is not None and roe >= 20:
            score += 1
        if pe_vs is not None and pe_vs > 20:
            score += 1
        return min(score, 3)

    @staticmethod
    def _value_rating(total: int) -> str:
        if total >= 10:
            return "A 级，高质量长期候选"
        if total >= 7:
            return "B 级，质量较好但需看估值和买点"
        if total >= 4:
            return "C 级，存在明显短板"
        return "D 级，价值投资框架下需谨慎"

    @staticmethod
    def _growth_force_comment(
        revenue_change: Optional[float],
        profit_change: Optional[float],
        net_margin: Optional[float],
    ) -> str:
        if revenue_change is None and profit_change is None:
            return "趋势数据不足，先看最新财报绝对质量。"
        if (revenue_change or 0) > 0 and (profit_change or 0) > 0:
            return "收入和利润同向改善，财报动能较好。"
        if (revenue_change or 0) > 0 and (profit_change or 0) < 0:
            return "收入增长但利润承压，需要检查成本、费用或一次性项目。"
        if net_margin is not None and net_margin >= 20:
            return "增速一般但利润率仍高，偏成熟现金牛特征。"
        return "增长动能偏弱，需要等待收入或利润重新加速。"

    @staticmethod
    def _valuation_force_comment(pe_vs: Optional[float], pb_vs: Optional[float]) -> str:
        if pe_vs is None and pb_vs is None:
            return "估值对比数据不足。"
        if (pe_vs is not None and pe_vs > 50) or (pb_vs is not None and pb_vs > 100):
            return "相对估值溢价较高，市场已经给了较强预期。"
        if pe_vs is not None and pe_vs < -20:
            return "相对 peer 有折价，需判断是机会还是基本面折价。"
        return "相对估值未到极端区间，需结合增长质量判断。"

    def _risk_budget_rows(
        self,
        macro: Dict[str, Any],
        technical: Dict[str, Any],
        peer_summary: Dict[str, Any],
    ) -> Tuple[List[Dict[str, str]], int]:
        risk_points = 0
        ten_yield = self._macro_indicator_value(macro, ("10Y Treasury Yield",))
        cpi = self._macro_indicator_value(macro, ("CPI YoY", "CPI"))
        mfi = self._to_float(technical.get("mfi14") or technical.get("MFI14"))
        pe_vs = self._to_float((peer_summary or {}).get("pe_ratio_vs_peer_median_pct"))

        rows: List[Dict[str, str]] = []
        if ten_yield is not None:
            high = ten_yield >= 4.25
            risk_points += 1 if high else 0
            rows.append({
                "module": "利率/折现率",
                "signal": f"10Y 美债 {ten_yield:.2f}%",
                "meaning": "高估值资产折现率压力偏大" if high else "利率压力相对可控",
            })
        if cpi is not None:
            high = cpi >= 3.0
            risk_points += 1 if high else 0
            rows.append({
                "module": "通胀",
                "signal": f"CPI YoY {cpi:.2f}%",
                "meaning": "通胀仍偏粘性，降息预期容易反复" if high else "通胀压力相对缓和",
            })
        if pe_vs is not None:
            high = pe_vs > 50
            risk_points += 1 if high else 0
            rows.append({
                "module": "估值拥挤",
                "signal": f"PE 相对 peer {pe_vs:+.2f}%",
                "meaning": "估值已反映较多乐观预期" if high else "相对估值没有显著过热",
            })
        if mfi is not None:
            high = mfi >= 80
            warm = mfi >= 70
            risk_points += 1 if high else 0
            rows.append({
                "module": "技术热度",
                "signal": f"MFI14 {mfi:.2f}",
                "meaning": "资金流过热，追高风险较大" if high else "资金流偏热，适合等回踩" if warm else "资金流未明显过热",
            })
        if not rows:
            rows.append({
                "module": "数据质量",
                "signal": "宏观/技术风险数据不足",
                "meaning": "暂以个股财报和价格纪律为主",
            })
        return rows, risk_points

    @staticmethod
    def _macro_indicator_value(macro: Dict[str, Any], labels: Tuple[str, ...]) -> Optional[float]:
        indicators = macro.get("indicators") if isinstance(macro, dict) else None
        if not isinstance(indicators, list):
            return None
        for item in indicators:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("series_id") or "")
            if any(token in label for token in labels):
                return NotificationService._to_float(item.get("value"))
        return None

    @classmethod
    def _financial_number(cls, snapshot: Dict[str, Any], value_key: str, display_key: str) -> Optional[float]:
        value = snapshot.get(value_key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
        return cls._parse_money(snapshot.get(display_key))

    @staticmethod
    def _parse_money(value: Any) -> Optional[float]:
        text = str(value or "").strip()
        if not text or text.upper() in {"N/A", "NA", "NONE"}:
            return None
        multiplier = 1.0
        upper = text.upper()
        if "B" in upper:
            multiplier = 1_000_000_000.0
        elif "M" in upper:
            multiplier = 1_000_000.0
        negative = text.startswith("-") or ("(" in text and ")" in text)
        cleaned = (
            text.replace("$", "")
            .replace(",", "")
            .replace("B", "")
            .replace("b", "")
            .replace("M", "")
            .replace("m", "")
            .replace("(", "")
            .replace(")", "")
            .strip()
        )
        try:
            number = float(cleaned) * multiplier
        except (TypeError, ValueError):
            return None
        return -number if negative else number

    @staticmethod
    def _parse_percent(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).replace("%", "").strip()
        if not text or text.upper() in {"N/A", "NA", "NONE"}:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _financial_pct(
        cls,
        snapshot: Dict[str, Any],
        pct_key: str,
        numerator: Optional[float],
        denominator: Optional[float],
    ) -> Optional[float]:
        explicit = cls._parse_percent(snapshot.get(pct_key))
        if explicit is not None:
            return explicit
        if numerator is None or denominator in (None, 0):
            return None
        try:
            return round(float(numerator) / float(denominator) * 100.0, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def _fmt_pct(value: Optional[float]) -> str:
        return "N/A" if value is None else f"{float(value):.2f}%"

    @staticmethod
    def _profitability_comment(net_margin: Optional[float]) -> str:
        if net_margin is None:
            return "利润率数据不足，暂不能判断收入转化为利润的效率。"
        if net_margin >= 20:
            return "净利率处于高水平，说明品牌、生态或规模效率较强。"
        if net_margin >= 10:
            return "净利率尚可，但需要与同业和历史趋势比较。"
        return "净利率偏低，需关注成本、价格竞争或一次性费用压力。"

    @staticmethod
    def _cash_quality_comment(ocf_to_ni: Optional[float], fcf_to_ni: Optional[float]) -> str:
        if ocf_to_ni is None and fcf_to_ni is None:
            return "现金流数据不足，无法判断利润含金量。"
        if ocf_to_ni is not None and ocf_to_ni >= 100 and (fcf_to_ni is None or fcf_to_ni >= 80):
            return "利润向现金转化较强，是财报质量的核心正面信号。"
        if ocf_to_ni is not None and ocf_to_ni < 80:
            return "经营现金流低于净利润，需要检查应收、库存、递延收入或一次性项目。"
        if fcf_to_ni is not None and fcf_to_ni < 50:
            return "资本开支后自由现金流偏弱，需要判断投资是否能形成未来增长。"
        return "现金流质量总体可接受，但仍需结合连续季度趋势确认。"

    @staticmethod
    def _balance_sheet_comment(
        debt_to_assets: Optional[float],
        equity_ratio: Optional[float],
        interest_bearing_debt_to_assets: Optional[float] = None,
        liquid_assets_to_debt: Optional[float] = None,
        net_cash: Optional[float] = None,
    ) -> str:
        if debt_to_assets is None and equity_ratio is None:
            return "资产负债表数据不足，暂不能判断杠杆安全性。"
        if net_cash is not None and net_cash > 0 and (
            interest_bearing_debt_to_assets is None or interest_bearing_debt_to_assets < 30
        ):
            return "总负债率较高，但有息债务率不高且净现金为正，债务安全性不能按总负债率简单判差。"
        if liquid_assets_to_debt is not None and liquid_assets_to_debt >= 100:
            return "可变现资产能覆盖有息债务，需关注的是回购导致的低权益基数，而非短期偿债压力。"
        if debt_to_assets is not None and debt_to_assets > 70:
            return "总负债率偏高，回购型公司需特别区分经营性负债、有息债务和低权益基数效应。"
        if debt_to_assets is not None and debt_to_assets < 50:
            return "资产负债表相对保守，抗冲击能力较好。"
        return "杠杆处于中等区间，需结合现金流和利率环境观察。"

    @staticmethod
    def _roe_comment(roe: Optional[float]) -> str:
        if roe is None:
            return "ROE 数据不足，不能判断资本效率。"
        if roe >= 20:
            return "ROE 较高，但若公司长期回购导致权益偏低，需要同步看利润增长和现金流。"
        if roe >= 15:
            return "ROE 良好，资本使用效率较强。"
        if roe >= 10:
            return "ROE 一般，需观察是否具备持续改善空间。"
        return "ROE 偏低，价值投资视角需要谨慎。"

    def _append_macro_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'macro_snapshot', None)
        if not snapshot:
            return
        indicators = snapshot.get("indicators")
        if not isinstance(indicators, list) or not indicators:
            return

        lines.extend([
            "### 🌐 FRED 宏观指标",
            "",
            "| 指标 | 数值 | 日期 | 说明 |",
            "|------|------|------|------|",
        ])
        for item in indicators:
            if not isinstance(item, dict):
                continue
            label = item.get("label") or item.get("series_id") or "N/A"
            value = item.get("value")
            unit = item.get("unit") or ""
            date = item.get("date") or "N/A"
            note = item.get("note") or ""
            value_text = "N/A" if value is None or value == "" else f"{value}{unit}"
            lines.append(f"| {label} | {value_text} | {date} | {note} |")
        lines.append("")

    def _append_dividend_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'dividend_snapshot', None)
        if not snapshot:
            return

        def _value(key: str) -> str:
            value = snapshot.get(key)
            if value is None or value == "":
                return "N/A"
            if key == "ttm_dividend_yield_pct" and isinstance(value, (int, float)):
                return f"{float(value):.4f}%"
            return str(value)

        lines.extend([
            "### 💵 分红指标",
            "",
            "| 指标 | 数值 | 来源/说明 |",
            "|------|------|-----------|",
            f"| 近12个月每股现金分红 | {_value('ttm_cash_dividend_per_share')} | 仅现金分红、税前口径 |",
            f"| TTM 股息率 | {_value('ttm_dividend_yield_pct')} | 近12个月每股现金分红 / 当前价格 |",
            f"| TTM 分红事件数 | {_value('ttm_event_count')} | {_value('source')} |",
            f"| 最近分红事实 | {_value('latest_dividend_fact')} | {_value('source')} |",
            "",
        ])

        events = snapshot.get("events")
        if isinstance(events, list) and events:
            lines.extend([
                "| 除息/事件日期 | 每股现金分红 |",
                "|--------------|--------------|",
            ])
            for event in events[:4]:
                if not isinstance(event, dict):
                    continue
                lines.append(
                    f"| {event.get('event_date', 'N/A')} | {event.get('cash_dividend_per_share', 'N/A')} |"
                )
            lines.append("")

    def _append_peer_valuation_snapshot(self, lines: List[str], result: AnalysisResult) -> None:
        snapshot = getattr(result, 'peer_valuation_snapshot', None)
        if not snapshot:
            return
        rows = snapshot.get("rows")
        if not isinstance(rows, list) or not rows:
            return

        def _num(value: Any, digits: int = 2) -> str:
            if value is None or value == "":
                return "N/A"
            try:
                return f"{float(value):.{digits}f}"
            except (TypeError, ValueError):
                return str(value)

        lines.extend([
            "### 🧭 同类型估值对比",
            "",
        ])
        basis = snapshot.get("comparison_basis")
        if basis:
            lines.extend([f"> 对比口径：{basis}", ""])
        lines.extend([
            "| 公司 | 当前价 | PE | PB | 市值 |",
            "|------|--------|----|----|------|",
        ])
        for row in rows[:6]:
            if not isinstance(row, dict):
                continue
            label = f"{row.get('symbol', 'N/A')} {row.get('name', '')}".strip()
            if row.get("is_target"):
                label = f"**{label}**"
            lines.append(
                f"| {label} | {_num(row.get('price'))} | {_num(row.get('pe_ratio'))} | "
                f"{_num(row.get('pb_ratio'))} | {row.get('market_cap_text') or 'N/A'} |"
            )

        summary = snapshot.get("summary")
        if isinstance(summary, dict) and summary:
            pe_vs = summary.get("pe_ratio_vs_peer_median_pct")
            pb_vs = summary.get("pb_ratio_vs_peer_median_pct")
            pe_vs_text = "N/A" if pe_vs is None else f"{float(pe_vs):.2f}%"
            pb_vs_text = "N/A" if pb_vs is None else f"{float(pb_vs):.2f}%"
            lines.extend([
                "",
                "| 对比项 | 数值 |",
                "|--------|------|",
                f"| Peer PE 中位数 | {_num(summary.get('peer_median_pe_ratio'))} |",
                f"| Peer PB 中位数 | {_num(summary.get('peer_median_pb_ratio'))} |",
                f"| 标的 PE 相对中位数 | {pe_vs_text} |",
                f"| 标的 PB 相对中位数 | {pb_vs_text} |",
            ])
        source = snapshot.get("source") or snapshot.get("provider") or "realtime_quote"
        lines.extend([
            "",
            f"> 来源：{source}。该表用于相对估值参考，不能替代增长率、利润质量和业务结构判断。",
            "",
        ])

    def _should_use_image_for_channel(
        self, channel: NotificationChannel, image_bytes: Optional[bytes]
    ) -> bool:
        """
        Decide whether to send as image for the given channel (Issue #289).

        Fallback rules (send as Markdown text instead of image):
        - image_bytes is None: conversion failed / imgkit not installed / content over max_chars
        - WeChat: image exceeds ~2MB limit
        """
        if channel.value not in self._markdown_to_image_channels or image_bytes is None:
            return False
        if channel == NotificationChannel.WECHAT and len(image_bytes) > WECHAT_IMAGE_MAX_BYTES:
            logger.warning(
                "企业微信图片超限 (%d bytes)，回退为 Markdown 文本发送",
                len(image_bytes),
            )
            return False
        return True

    def send(
        self,
        content: str,
        email_stock_codes: Optional[List[str]] = None,
        email_send_to_all: bool = False
    ) -> bool:
        """
        统一发送接口 - 向所有已配置的渠道发送

        遍历所有已配置的渠道，逐一发送消息

        Fallback rules (Markdown-to-image, Issue #289):
        - When image_bytes is None (conversion failed / imgkit not installed /
          content over max_chars): all channels configured for image will send
          as Markdown text instead.
        - When WeChat image exceeds ~2MB: that channel falls back to Markdown text.

        Args:
            content: 消息内容（Markdown 格式）
            email_stock_codes: 股票代码列表（可选，用于邮件渠道路由到对应分组邮箱，Issue #268）
            email_send_to_all: 邮件是否发往所有配置邮箱（用于大盘复盘等无股票归属的内容）

        Returns:
            是否至少有一个渠道发送成功
        """
        context_success = self.send_to_context(content)

        if not self._available_channels:
            if context_success:
                logger.info("已通过消息上下文渠道完成推送（无其他通知渠道）")
                return True
            logger.warning("通知服务不可用，跳过推送")
            return False

        # Markdown to image (Issue #289): convert once if any channel needs it.
        # Per-channel decision via _should_use_image_for_channel (see send() docstring for fallback rules).
        image_bytes = None
        channels_needing_image = {
            ch for ch in self._available_channels
            if ch.value in self._markdown_to_image_channels
        }
        if channels_needing_image:
            from src.md2img import markdown_to_image
            image_bytes = markdown_to_image(
                content, max_chars=self._markdown_to_image_max_chars
            )
            if image_bytes:
                logger.info("Markdown 已转换为图片，将向 %s 发送图片",
                            [ch.value for ch in channels_needing_image])
            elif channels_needing_image:
                try:
                    from src.config import get_config
                    engine = getattr(get_config(), "md2img_engine", "wkhtmltoimage")
                except Exception:
                    engine = "wkhtmltoimage"
                hint = (
                    "npm i -g markdown-to-file" if engine == "markdown-to-file"
                    else "wkhtmltopdf (apt install wkhtmltopdf / brew install wkhtmltopdf)"
                )
                logger.warning(
                    "Markdown 转图片失败，将回退为文本发送。请检查 MARKDOWN_TO_IMAGE_CHANNELS 配置并安装 %s",
                    hint,
                )

        channel_names = self.get_channel_names()
        logger.info(f"正在向 {len(self._available_channels)} 个渠道发送通知：{channel_names}")

        success_count = 0
        fail_count = 0

        for channel in self._available_channels:
            channel_name = ChannelDetector.get_channel_name(channel)
            use_image = self._should_use_image_for_channel(channel, image_bytes)
            try:
                if channel == NotificationChannel.WECHAT:
                    if use_image:
                        result = self._send_wechat_image(image_bytes)
                    else:
                        result = self.send_to_wechat(content)
                elif channel == NotificationChannel.FEISHU:
                    result = self.send_to_feishu(content)
                elif channel == NotificationChannel.TELEGRAM:
                    if use_image:
                        result = self._send_telegram_photo(image_bytes)
                    else:
                        result = self.send_to_telegram(content)
                elif channel == NotificationChannel.EMAIL:
                    receivers = None
                    if email_send_to_all and self._stock_email_groups:
                        receivers = self.get_all_email_receivers()
                    elif email_stock_codes and self._stock_email_groups:
                        receivers = self.get_receivers_for_stocks(email_stock_codes)
                    if use_image:
                        result = self._send_email_with_inline_image(
                            image_bytes, receivers=receivers
                        )
                    else:
                        result = self.send_to_email(content, receivers=receivers)
                elif channel == NotificationChannel.PUSHOVER:
                    result = self.send_to_pushover(content)
                elif channel == NotificationChannel.PUSHPLUS:
                    result = self.send_to_pushplus(content)
                elif channel == NotificationChannel.SERVERCHAN3:
                    result = self.send_to_serverchan3(content)
                elif channel == NotificationChannel.CUSTOM:
                    if use_image:
                        result = self._send_custom_webhook_image(
                            image_bytes, fallback_content=content
                        )
                    else:
                        result = self.send_to_custom(content)
                elif channel == NotificationChannel.DISCORD:
                    result = self.send_to_discord(content)
                elif channel == NotificationChannel.SLACK:
                    if use_image:
                        result = self._send_slack_image(
                            image_bytes, fallback_content=content
                        )
                    else:
                        result = self.send_to_slack(content)
                elif channel == NotificationChannel.ASTRBOT:
                    result = self.send_to_astrbot(content)
                else:
                    logger.warning(f"不支持的通知渠道: {channel}")
                    result = False

                if result:
                    success_count += 1
                else:
                    fail_count += 1

            except Exception as e:
                logger.error(f"{channel_name} 发送失败: {e}")
                fail_count += 1

        logger.info(f"通知发送完成：成功 {success_count} 个，失败 {fail_count} 个")
        return success_count > 0 or context_success
   
    def save_report_to_file(
        self, 
        content: str, 
        filename: Optional[str] = None
    ) -> str:
        """
        保存日报到本地文件
        
        Args:
            content: 日报内容
            filename: 文件名（可选，默认按日期生成）
            
        Returns:
            保存的文件路径
        """
        from pathlib import Path
        
        if filename is None:
            date_str = datetime.now().strftime('%Y%m%d')
            filename = f"report_{date_str}.md"
        
        # 确保 reports 目录存在（使用项目根目录下的 reports）
        reports_dir = Path(__file__).parent.parent / 'reports'
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        filepath = reports_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        logger.info(f"日报已保存到: {filepath}")
        return str(filepath)


class NotificationBuilder:
    """
    通知消息构建器
    
    提供便捷的消息构建方法
    """
    
    @staticmethod
    def build_simple_alert(
        title: str,
        content: str,
        alert_type: str = "info"
    ) -> str:
        """
        构建简单的提醒消息
        
        Args:
            title: 标题
            content: 内容
            alert_type: 类型（info, warning, error, success）
        """
        emoji_map = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "❌",
            "success": "✅",
        }
        emoji = emoji_map.get(alert_type, "📢")
        
        return f"{emoji} **{title}**\n\n{content}"
    
    @staticmethod
    def build_stock_summary(results: List[AnalysisResult]) -> str:
        """
        构建股票摘要（简短版）
        
        适用于快速通知
        """
        report_language = normalize_report_language(
            next((getattr(result, "report_language", None) for result in results if getattr(result, "report_language", None)), None)
        )
        labels = get_report_labels(report_language)
        lines = [f"📊 **{labels['summary_heading']}**", ""]
        
        for r in sorted(results, key=lambda x: x.sentiment_score, reverse=True):
            _, emoji, _ = get_signal_level(r.operation_advice, r.sentiment_score, report_language)
            name = get_localized_stock_name(r.name, r.code, report_language)
            lines.append(
                f"{emoji} {name}({r.code}): {localize_operation_advice(r.operation_advice, report_language)} | "
                f"{labels['score_label']} {r.sentiment_score}"
            )
        
        return "\n".join(lines)


# 便捷函数
def get_notification_service() -> NotificationService:
    """获取通知服务实例"""
    return NotificationService()


def send_daily_report(results: List[AnalysisResult]) -> bool:
    """
    发送每日报告的快捷方式
    
    自动识别渠道并推送
    """
    service = get_notification_service()
    
    # 生成报告
    report = service.generate_daily_report(results)
    
    # 保存到本地
    service.save_report_to_file(report)
    
    # 推送到配置的渠道（自动识别）
    return service.send(report)


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    # 模拟分析结果
    test_results = [
        AnalysisResult(
            code='600519',
            name='贵州茅台',
            sentiment_score=75,
            trend_prediction='看多',
            analysis_summary='技术面强势，消息面利好',
            operation_advice='买入',
            technical_analysis='放量突破 MA20，MACD 金叉',
            news_summary='公司发布分红公告，业绩超预期',
        ),
        AnalysisResult(
            code='000001',
            name='平安银行',
            sentiment_score=45,
            trend_prediction='震荡',
            analysis_summary='横盘整理，等待方向',
            operation_advice='持有',
            technical_analysis='均线粘合，成交量萎缩',
            news_summary='近期无重大消息',
        ),
        AnalysisResult(
            code='300750',
            name='宁德时代',
            sentiment_score=35,
            trend_prediction='看空',
            analysis_summary='技术面走弱，注意风险',
            operation_advice='卖出',
            technical_analysis='跌破 MA10 支撑，量能不足',
            news_summary='行业竞争加剧，毛利率承压',
        ),
    ]
    
    service = NotificationService()
    
    # 显示检测到的渠道
    print("=== 通知渠道检测 ===")
    print(f"当前渠道: {service.get_channel_names()}")
    print(f"渠道列表: {service.get_available_channels()}")
    print(f"服务可用: {service.is_available()}")
    
    # 生成日报
    print("\n=== 生成日报测试 ===")
    report = service.generate_daily_report(test_results)
    print(report)
    
    # 保存到文件
    print("\n=== 保存日报 ===")
    filepath = service.save_report_to_file(report)
    print(f"保存成功: {filepath}")
    
    # 推送测试
    if service.is_available():
        print(f"\n=== 推送测试（{service.get_channel_names()}）===")
        success = service.send(report)
        print(f"推送结果: {'成功' if success else '失败'}")
    else:
        print("\n通知渠道未配置，跳过推送测试")
