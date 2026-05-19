# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - AI分析层
===================================

职责：
1. 封装 LLM 调用逻辑（通过 LiteLLM 统一调用 Gemini/Anthropic/OpenAI 等）
2. 结合技术面和消息面生成分析报告
3. 解析 LLM 响应为结构化 AnalysisResult
"""

import json
import logging
import math
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple, Callable

import litellm
from json_repair import repair_json
from litellm import Router

from src.agent.llm_adapter import get_thinking_extra_body
from src.agent.skills.defaults import CORE_TRADING_SKILL_POLICY_ZH
from src.config import (
    Config,
    extra_litellm_params,
    get_api_keys_for_model,
    get_config,
    get_configured_llm_models,
    normalize_litellm_temperature,
    resolve_news_window_days,
)
from src.storage import persist_llm_usage
from src.data.stock_mapping import STOCK_NAME_MAP
from src.report_language import (
    get_signal_level,
    get_no_data_text,
    get_placeholder_text,
    get_unknown_text,
    infer_decision_type_from_advice,
    localize_chip_health,
    localize_confidence_level,
    normalize_report_language,
)
from src.schemas.report_schema import AnalysisReportSchema
from src.market_context import get_market_role, get_market_guidelines

logger = logging.getLogger(__name__)

SCORECARD_DIMENSIONS: Tuple[Tuple[str, str, int], ...] = (
    ("technical", "技术面", 25),
    ("fundamental", "基本面", 25),
    ("valuation", "估值", 20),
    ("news_sentiment", "新闻/情绪", 15),
    ("macro_risk", "宏观/风险", 15),
)

_SCORECARD_EN_LABELS: Dict[str, str] = {
    "technical": "Technicals",
    "fundamental": "Fundamentals",
    "valuation": "Valuation",
    "news_sentiment": "News/Sentiment",
    "macro_risk": "Macro/Risk",
}

_SCORECARD_TEXT_HINTS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "technical": (
        ("多头", "突破", "放量", "缩量企稳", "趋势向上", "金叉", "站上", "bullish", "breakout", "uptrend"),
        ("空头", "跌破", "破位", "放量下跌", "超买", "乖离", "overbought", "breakdown", "bearish"),
    ),
    "fundamental": (
        ("增长", "改善", "强劲", "盈利", "现金流", "ROE", "利润率", "上调", "growth", "profit", "cash flow"),
        ("下滑", "亏损", "承压", "恶化", "放缓", "缺失", "衰退", "loss", "decline", "pressure"),
    ),
    "valuation": (
        ("低估", "合理", "折价", "便宜", "安全边际", "undervalued", "discount", "reasonable"),
        ("高估", "偏贵", "估值过高", "溢价", "透支", "expensive", "premium", "overvalued"),
    ),
    "news_sentiment": (
        ("利好", "催化", "上调", "买入评级", "订单", "positive", "upgrade", "catalyst"),
        ("利空", "减持", "诉讼", "调查", "下调", "负面", "negative", "downgrade", "lawsuit"),
    ),
    "macro_risk": (
        ("顺风", "降息", "流动性改善", "需求复苏", "tailwind", "rate cut", "demand recovery"),
        ("逆风", "利率压力", "通胀", "衰退", "监管", "出口管制", "headwind", "inflation", "recession"),
    ),
}


class _LiteLLMStreamError(RuntimeError):
    """Internal error wrapper that records whether any text was streamed."""

    def __init__(self, message: str, *, partial_received: bool = False):
        super().__init__(message)
        self.partial_received = partial_received


def check_content_integrity(result: "AnalysisResult") -> Tuple[bool, List[str]]:
    """
    Check mandatory fields for report content integrity.
    Returns (pass, missing_fields). Module-level for use by pipeline (agent weak mode).
    """
    missing: List[str] = []
    if result.sentiment_score is None:
        missing.append("sentiment_score")
    advice = result.operation_advice
    if not advice or not isinstance(advice, str) or not advice.strip():
        missing.append("operation_advice")
    summary = result.analysis_summary
    if not summary or not isinstance(summary, str) or not summary.strip():
        missing.append("analysis_summary")
    dash = result.dashboard if isinstance(result.dashboard, dict) else {}
    core = dash.get("core_conclusion")
    core = core if isinstance(core, dict) else {}
    if not (core.get("one_sentence") or "").strip():
        missing.append("dashboard.core_conclusion.one_sentence")
    intel = dash.get("intelligence")
    intel = intel if isinstance(intel, dict) else None
    if intel is None or "risk_alerts" not in intel:
        missing.append("dashboard.intelligence.risk_alerts")
    if result.decision_type in ("buy", "hold"):
        battle = dash.get("battle_plan")
        battle = battle if isinstance(battle, dict) else {}
        sp = battle.get("sniper_points")
        sp = sp if isinstance(sp, dict) else {}
        stop_loss = sp.get("stop_loss")
        if stop_loss is None or (isinstance(stop_loss, str) and not stop_loss.strip()):
            missing.append("dashboard.battle_plan.sniper_points.stop_loss")
    return len(missing) == 0, missing


def apply_placeholder_fill(result: "AnalysisResult", missing_fields: List[str]) -> None:
    """Fill missing mandatory fields with placeholders (in-place). Module-level for pipeline."""
    placeholder = get_placeholder_text(getattr(result, "report_language", "zh"))
    for field in missing_fields:
        if field == "sentiment_score":
            result.sentiment_score = 50
        elif field == "operation_advice":
            result.operation_advice = result.operation_advice or placeholder
        elif field == "analysis_summary":
            result.analysis_summary = result.analysis_summary or placeholder
        elif field == "dashboard.core_conclusion.one_sentence":
            if not result.dashboard:
                result.dashboard = {}
            if "core_conclusion" not in result.dashboard:
                result.dashboard["core_conclusion"] = {}
            result.dashboard["core_conclusion"]["one_sentence"] = (
                result.dashboard["core_conclusion"].get("one_sentence") or placeholder
            )
        elif field == "dashboard.intelligence.risk_alerts":
            if not result.dashboard:
                result.dashboard = {}
            if "intelligence" not in result.dashboard:
                result.dashboard["intelligence"] = {}
            if "risk_alerts" not in result.dashboard["intelligence"]:
                result.dashboard["intelligence"]["risk_alerts"] = []
        elif field == "dashboard.battle_plan.sniper_points.stop_loss":
            if not result.dashboard:
                result.dashboard = {}
            if "battle_plan" not in result.dashboard:
                result.dashboard["battle_plan"] = {}
            if "sniper_points" not in result.dashboard["battle_plan"]:
                result.dashboard["battle_plan"]["sniper_points"] = {}
            result.dashboard["battle_plan"]["sniper_points"]["stop_loss"] = placeholder


def _scorecard_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = value.strip().replace("分", "").replace("%", "")
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))


def _scorecard_label(key: str, default_label: str, report_language: str) -> str:
    if normalize_report_language(report_language) == "en":
        return _SCORECARD_EN_LABELS.get(key, default_label)
    return default_label


def _scorecard_text_delta(key: str, text: str) -> int:
    if not text:
        return 0
    lowered = text.lower()
    positive_terms, negative_terms = _SCORECARD_TEXT_HINTS.get(key, ((), ()))
    positive_hits = sum(1 for term in positive_terms if term.lower() in lowered)
    negative_hits = sum(1 for term in negative_terms if term.lower() in lowered)
    return max(-15, min(15, (positive_hits - negative_hits) * 4))


def _scorecard_dimension_text(payload: Dict[str, Any], dashboard: Dict[str, Any], key: str) -> str:
    top_level_keys = {
        "technical": ("technical_analysis", "ma_analysis", "volume_analysis", "pattern_analysis", "trend_analysis"),
        "fundamental": ("fundamental_analysis", "company_highlights", "sector_position"),
        "valuation": ("valuation_analysis", "fundamental_analysis", "risk_warning"),
        "news_sentiment": ("news_summary", "market_sentiment", "hot_topics", "risk_warning"),
        "macro_risk": ("market_sentiment", "risk_warning", "analysis_summary"),
    }.get(key, ())
    parts: List[str] = []
    for field in top_level_keys:
        value = payload.get(field)
        if value:
            parts.append(str(value))

    nested_keys = {
        "technical": ("data_perspective",),
        "fundamental": ("intelligence",),
        "valuation": ("core_conclusion",),
        "news_sentiment": ("intelligence",),
        "macro_risk": ("intelligence",),
    }.get(key, ())
    for field in nested_keys:
        value = dashboard.get(field)
        if value:
            try:
                parts.append(json.dumps(value, ensure_ascii=False, default=str))
            except TypeError:
                parts.append(str(value))
    return "\n".join(parts)


def ensure_dashboard_scorecard_payload(
    payload: Dict[str, Any],
    report_language: str = "zh",
) -> Optional[int]:
    """Ensure dashboard.scorecard exists, then normalize and return the weighted score.

    LLM and Agent paths do not always honor the expanded schema.  This helper
    creates a transparent fallback scorecard from the legacy score and available
    dimension-specific text so the WebUI has a stable multi-dimensional shape.
    """
    normalized_score = normalize_dashboard_scorecard_payload(payload, report_language)
    dashboard = payload.get("dashboard") if isinstance(payload, dict) else None
    if not isinstance(dashboard, dict):
        if not isinstance(payload, dict):
            return normalized_score
        dashboard = {}
        payload["dashboard"] = dashboard

    scorecard = dashboard.get("scorecard")
    if not isinstance(scorecard, dict):
        scorecard = {}
        dashboard["scorecard"] = scorecard

    raw_dimensions = scorecard.get("dimensions")
    dimensions = raw_dimensions if isinstance(raw_dimensions, dict) else {}
    if normalized_score is not None and len(dimensions) >= len(SCORECARD_DIMENSIONS):
        return normalized_score
    has_model_dimension = any(
        isinstance(dimensions.get(key), dict)
        and _scorecard_int(dimensions[key].get("score")) is not None
        for key, _, _ in SCORECARD_DIMENSIONS
    )

    base_score = _scorecard_int(scorecard.get("overall_score"), None)
    if base_score is None:
        base_score = _scorecard_int(payload.get("sentiment_score"), 50)
    if base_score is None:
        base_score = 50

    synthesized_dimensions: Dict[str, Dict[str, Any]] = dict(dimensions)
    for key, default_label, default_weight in SCORECARD_DIMENSIONS:
        raw_dimension = synthesized_dimensions.get(key)
        if isinstance(raw_dimension, dict) and _scorecard_int(raw_dimension.get("score")) is not None:
            continue

        dimension_text = _scorecard_dimension_text(payload, dashboard, key)
        score_seed = base_score + _scorecard_text_delta(key, dimension_text) if has_model_dimension else base_score
        score = _scorecard_int(score_seed, base_score)
        if normalize_report_language(report_language) == "en":
            evidence = (
                "Synthesized from available report text because the model did not return this dimension score."
            )
        else:
            evidence = "模型未返回该维度评分，系统基于旧综合分和对应文本信号估算；重新分析时会优先使用模型原生分项。"
        if dimension_text.strip():
            evidence = dimension_text.strip().splitlines()[0][:80]

        synthesized_dimensions[key] = {
            "label": _scorecard_label(key, default_label, report_language),
            "score": score,
            "weight": default_weight,
            "evidence": evidence,
            "source": "model" if isinstance(raw_dimension, dict) else "fallback",
        }

    scorecard["dimensions"] = synthesized_dimensions
    scorecard["score_method"] = scorecard.get("score_method") or (
        "综合评分 = 技术面25% + 基本面25% + 估值20% + 新闻/情绪15% + 宏观/风险15%"
        if normalize_report_language(report_language) == "zh"
        else "Overall score = technical 25% + fundamentals 25% + valuation 20% + news/sentiment 15% + macro/risk 15%"
    )
    return normalize_dashboard_scorecard_payload(payload, report_language)


def normalize_dashboard_scorecard_payload(
    payload: Dict[str, Any],
    report_language: str = "zh",
) -> Optional[int]:
    """Normalize dashboard.scorecard and return the weighted overall score."""
    if not isinstance(payload, dict):
        return None
    dashboard = payload.get("dashboard")
    if not isinstance(dashboard, dict):
        return None
    scorecard = dashboard.get("scorecard")
    if not isinstance(scorecard, dict):
        return None

    raw_dimensions = scorecard.get("dimensions")
    if not isinstance(raw_dimensions, dict):
        raw_dimensions = {}

    normalized_dimensions: Dict[str, Dict[str, Any]] = {}
    weighted_total = 0.0
    total_weight = 0.0
    for key, default_label, default_weight in SCORECARD_DIMENSIONS:
        raw_dimension = raw_dimensions.get(key)
        if not isinstance(raw_dimension, dict):
            raw_dimension = scorecard.get(key) if isinstance(scorecard.get(key), dict) else {}
        score = _scorecard_int(raw_dimension.get("score")) if isinstance(raw_dimension, dict) else None
        if score is None:
            continue
        weight_raw = raw_dimension.get("weight") if isinstance(raw_dimension, dict) else None
        try:
            weight = float(weight_raw if weight_raw not in (None, "") else default_weight)
        except (TypeError, ValueError):
            weight = float(default_weight)
        if weight <= 0:
            weight = float(default_weight)
        label = raw_dimension.get("label") if isinstance(raw_dimension, dict) else None
        evidence = raw_dimension.get("evidence") if isinstance(raw_dimension, dict) else None
        normalized_dimension = dict(raw_dimension)
        normalized_dimension.update({
            "label": str(label or _scorecard_label(key, default_label, report_language)),
            "score": score,
            "weight": int(weight) if weight.is_integer() else weight,
            "evidence": str(evidence or ""),
        })
        normalized_dimensions[key] = normalized_dimension
        weighted_total += score * weight
        total_weight += weight

    overall_score = None
    if total_weight > 0:
        overall_score = _scorecard_int(weighted_total / total_weight, 50)
    else:
        overall_score = _scorecard_int(scorecard.get("overall_score"), None)

    if overall_score is None:
        return None

    scorecard["overall_score"] = overall_score
    scorecard["total_weight"] = int(total_weight) if total_weight.is_integer() else total_weight
    scorecard["dimensions"] = normalized_dimensions
    scorecard["score_method"] = scorecard.get("score_method") or (
        "综合评分 = 技术面25% + 基本面25% + 估值20% + 新闻/情绪15% + 宏观/风险15%"
        if normalize_report_language(report_language) == "zh"
        else "Overall score = technical 25% + fundamentals 25% + valuation 20% + news/sentiment 15% + macro/risk 15%"
    )
    dashboard["scorecard"] = scorecard
    payload["sentiment_score"] = overall_score
    return overall_score


# ---------- chip_structure fallback (Issue #589) ----------

_CHIP_KEYS: tuple = ("profit_ratio", "avg_cost", "concentration", "chip_health")


def _is_value_placeholder(v: Any) -> bool:
    """True if value is empty or placeholder (N/A, 数据缺失, etc.)."""
    if v is None:
        return True
    if isinstance(v, (int, float)) and v == 0:
        return True
    s = str(v).strip().lower()
    return s in ("", "n/a", "na", "数据缺失", "未知", "data unavailable", "unknown", "tbd")


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Safely convert to float; return default on failure. Private helper for chip fill."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        try:
            return default if math.isnan(float(v)) else float(v)
        except (ValueError, TypeError):
            return default
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


_BULLISH_TREND_HINTS: Tuple[str, ...] = (
    "多头排列",
    "持续上涨",
    "趋势向上",
    "上升趋势",
    "向上发散",
    "bullish",
    "uptrend",
)
_WEAK_BULLISH_TREND_HINTS: Tuple[str, ...] = ("弱势多头",)
_BEARISH_TREND_HINTS: Tuple[str, ...] = (
    "空头排列",
    "持续下跌",
    "趋势向下",
    "下降趋势",
    "向下发散",
    "bearish",
    "downtrend",
)
_WEAK_BEARISH_TREND_HINTS: Tuple[str, ...] = ("弱势空头",)
_NEGATION_TOKENS: Tuple[str, ...] = (
    "不是",
    "并非",
    "并未",
    "没有",
    "尚不",
    "尚未",
    "未",
    "无",
    "不属",
    "非",
    "not ",
    "no ",
)
_NEGATION_BREAK_CHARS: Tuple[str, ...] = (",", ".", ";", ":", "!", "?", "，", "。", "；", "：", "！", "？", "\n")
_NEGATION_LOOKBACK_CHARS = 16
_NEGATION_MAX_GAP_CHARS = 8
_NEGATION_SCOPE_BREAK_TOKENS: Tuple[str, ...] = (
    "而是",
    "但是",
    "但",
    "反而",
    "反倒",
    "转为",
    "转成",
    "改为",
    "改成",
    " but ",
    " instead ",
    " rather ",
)
_SINGLE_CHAR_NEGATION_GAP_PREFIXES: Tuple[str, ...] = (
    "形成",
    "出现",
    "进入",
    "转为",
    "转成",
    "构成",
    "呈现",
    "显示",
    "属于",
    "是",
    "有",
    "能",
    "见",
    "站",
    "守",
    "破",
)


def _normalize_prompt_reason_items(items: Any) -> List[str]:
    """Normalize prompt reason/risk items into a clean string list."""
    if not isinstance(items, list):
        return []
    normalized: List[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def _contains_trend_hint(text: str, hints: Tuple[str, ...]) -> bool:
    """Return True when text contains a non-negated strong trend hint."""
    lowered = text.strip().lower()

    def _has_negation_scope_break(gap: str) -> bool:
        normalized_gap = gap.lower()
        for token in _NEGATION_SCOPE_BREAK_TOKENS:
            token_index = normalized_gap.find(token)
            if token_index > 0:
                return True
        return False

    def _is_valid_negation_gap(token: str, gap: str) -> bool:
        if not gap:
            return True
        if token not in {"未", "无", "非"}:
            return True
        return any(gap.startswith(prefix) for prefix in _SINGLE_CHAR_NEGATION_GAP_PREFIXES)

    def _is_negated_match(index: int) -> bool:
        prefix = lowered[max(0, index - _NEGATION_LOOKBACK_CHARS):index]
        for token in _NEGATION_TOKENS:
            token_index = prefix.rfind(token)
            if token_index < 0:
                continue
            gap = prefix[token_index + len(token):]
            if any(char in gap for char in _NEGATION_BREAK_CHARS):
                continue
            stripped_gap = gap.strip()
            if len(stripped_gap) > _NEGATION_MAX_GAP_CHARS:
                continue
            if _has_negation_scope_break(stripped_gap):
                continue
            if not _is_valid_negation_gap(token, stripped_gap):
                continue
            return True
        return False

    for hint in hints:
        keyword = hint.lower()
        start = 0
        while True:
            index = lowered.find(keyword, start)
            if index < 0:
                break
            if not _is_negated_match(index):
                return True
            start = index + len(keyword)
    return False


def _infer_trend_direction(trend: Dict[str, Any]) -> str:
    """Infer the final trend direction from trend_status and ma_alignment."""
    combined = " ".join(
        str(trend.get(key, "")).strip()
        for key in ("trend_status", "ma_alignment")
        if str(trend.get(key, "")).strip()
    )
    if not combined:
        return "neutral"
    lowered = combined.lower()
    normalized = lowered.replace(" ", "")
    has_bullish = (
        _contains_trend_hint(combined, _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS)
        or "ma5>ma10>ma20" in normalized
        or (
            "ma5>ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≤ma20", "ma10<=ma20"))
        )
    )
    has_bearish = (
        _contains_trend_hint(combined, _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS)
        or "ma5<ma10<ma20" in normalized
        or (
            "ma5<ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≥ma20", "ma10>=ma20"))
        )
    )
    if has_bullish and not has_bearish:
        return "bullish"
    if has_bearish and not has_bullish:
        return "bearish"
    return "neutral"


def _filter_conflicting_trend_items(items: List[str], conflict_hints: Tuple[str, ...]) -> List[str]:
    """Drop reasons that directly conflict with the final trend direction."""
    return [item for item in items if not _contains_trend_hint(item, conflict_hints)]


def _sanitize_trend_analysis_for_prompt(
    trend: Any,
    *,
    volume_change_ratio: Any = None,
) -> Dict[str, Any]:
    """Clean prompt-only trend hints on a derived copy without touching runtime/provider config."""
    trend_dict = dict(trend) if isinstance(trend, dict) else {}
    signal_reasons = _normalize_prompt_reason_items(trend_dict.get("signal_reasons"))
    risk_factors = _normalize_prompt_reason_items(trend_dict.get("risk_factors"))
    prompt_notes: List[str] = []
    trend_direction = _infer_trend_direction(trend_dict)

    if trend_direction == "bearish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append("当前技术结构偏空，已剔除与空头主判断直接冲突的看多结构理由。")
        signal_reasons = filtered_signal_reasons
        prompt_notes.append(
            "若新闻、业绩或政策催化偏多，只能表述为“事件先行、技术待确认”或“基本面偏多，但技术面尚未确认”，严禁写成确定性买点。"
        )
    elif trend_direction == "bullish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append("当前技术结构偏多，已剔除与多头主判断直接冲突的空头结构理由。")
        signal_reasons = filtered_signal_reasons
        filtered_risk_factors = _filter_conflicting_trend_items(
            risk_factors,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_risk_factors) != len(risk_factors):
            prompt_notes.append("当前技术结构偏多，已剔除与多头主判断直接冲突的空头结构风险表述。")
        risk_factors = filtered_risk_factors

    parsed_volume_change = _safe_float(volume_change_ratio, default=math.nan)
    if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
        prompt_notes.append(
            f"成交量较昨日变化约 {parsed_volume_change:.2f} 倍，可能存在异常数据或一次性冲量；量能信号必须降权解读，不能机械视为强确认。"
        )

    trend_dict["signal_reasons"] = signal_reasons
    trend_dict["risk_factors"] = risk_factors
    trend_dict["prompt_consistency_notes"] = prompt_notes
    trend_dict["prompt_trend_direction"] = trend_direction
    return trend_dict


def _derive_chip_health(profit_ratio: float, concentration_90: float, language: str = "zh") -> str:
    """Derive chip_health from profit_ratio and concentration_90."""
    if profit_ratio >= 0.9:
        return localize_chip_health("警惕", language)  # 获利盘极高
    if concentration_90 >= 0.25:
        return localize_chip_health("警惕", language)  # 筹码分散
    if concentration_90 < 0.15 and 0.3 <= profit_ratio < 0.9:
        return localize_chip_health("健康", language)  # 集中且获利比例适中
    return localize_chip_health("一般", language)


def _build_chip_structure_from_data(chip_data: Any, language: str = "zh") -> Dict[str, Any]:
    """Build chip_structure dict from ChipDistribution or dict."""
    if hasattr(chip_data, "profit_ratio"):
        pr = _safe_float(chip_data.profit_ratio)
        ac = chip_data.avg_cost
        c90 = _safe_float(chip_data.concentration_90)
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        pr = _safe_float(d.get("profit_ratio"))
        ac = d.get("avg_cost")
        c90 = _safe_float(d.get("concentration_90"))
    chip_health = _derive_chip_health(pr, c90, language=language)
    return {
        "profit_ratio": f"{pr:.1%}",
        "avg_cost": ac if (ac is not None and _safe_float(ac) != 0.0) else "N/A",
        "concentration": f"{c90:.2%}",
        "chip_health": chip_health,
    }


def fill_chip_structure_if_needed(result: "AnalysisResult", chip_data: Any) -> None:
    """When chip_data exists, fill chip_structure placeholder fields from chip_data (in-place)."""
    if not result or not chip_data:
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        # Use `or {}` rather than setdefault so that an explicit `null` from LLM is also replaced
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        cs = dp.get("chip_structure") or {}
        filled = _build_chip_structure_from_data(
            chip_data,
            language=getattr(result, "report_language", "zh"),
        )
        # Start from a copy of cs to preserve any extra keys the LLM may have added
        merged = dict(cs)
        for k in _CHIP_KEYS:
            if _is_value_placeholder(merged.get(k)):
                merged[k] = filled[k]
        if merged != cs:
            dp["chip_structure"] = merged
            logger.info("[chip_structure] Filled placeholder chip fields from data source (Issue #589)")
    except Exception as e:
        logger.warning("[chip_structure] Fill failed, skipping: %s", e)


_PRICE_POS_KEYS = ("ma5", "ma10", "ma20", "bias_ma5", "bias_status", "current_price", "support_level", "resistance_level")


def fill_price_position_if_needed(
    result: "AnalysisResult",
    trend_result: Any = None,
    realtime_quote: Any = None,
) -> None:
    """Fill missing price_position fields from trend_result / realtime data (in-place)."""
    if not result:
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        pp = dp.get("price_position") or {}

        computed: Dict[str, Any] = {}
        if trend_result:
            tr = trend_result if isinstance(trend_result, dict) else (
                trend_result.__dict__ if hasattr(trend_result, "__dict__") else {}
            )
            computed["ma5"] = tr.get("ma5")
            computed["ma10"] = tr.get("ma10")
            computed["ma20"] = tr.get("ma20")
            computed["bias_ma5"] = tr.get("bias_ma5")
            computed["current_price"] = tr.get("current_price")
            support_levels = tr.get("support_levels") or []
            resistance_levels = tr.get("resistance_levels") or []
            if support_levels:
                computed["support_level"] = support_levels[0]
            if resistance_levels:
                computed["resistance_level"] = resistance_levels[0]
        if realtime_quote:
            rq = realtime_quote if isinstance(realtime_quote, dict) else (
                realtime_quote.to_dict() if hasattr(realtime_quote, "to_dict") else {}
            )
            if _is_value_placeholder(computed.get("current_price")):
                computed["current_price"] = rq.get("price")

        filled = False
        for k in _PRICE_POS_KEYS:
            if _is_value_placeholder(pp.get(k)) and not _is_value_placeholder(computed.get(k)):
                pp[k] = computed[k]
                filled = True
        if filled:
            dp["price_position"] = pp
            logger.info("[price_position] Filled placeholder fields from computed data")
    except Exception as e:
        logger.warning("[price_position] Fill failed, skipping: %s", e)


def get_stock_name_multi_source(
    stock_code: str,
    context: Optional[Dict] = None,
    data_manager = None
) -> str:
    """
    多来源获取股票中文名称

    获取策略（按优先级）：
    1. 从传入的 context 中获取（realtime 数据）
    2. 从静态映射表 STOCK_NAME_MAP 获取
    3. 从 DataFetcherManager 获取（各数据源）
    4. 返回默认名称（股票+代码）

    Args:
        stock_code: 股票代码
        context: 分析上下文（可选）
        data_manager: DataFetcherManager 实例（可选）

    Returns:
        股票中文名称
    """
    # 1. 从上下文获取（实时行情数据）
    if context:
        # 优先从 stock_name 字段获取
        if context.get('stock_name'):
            name = context['stock_name']
            if name and not name.startswith('股票'):
                return name

        # 其次从 realtime 数据获取
        if 'realtime' in context and context['realtime'].get('name'):
            return context['realtime']['name']

    # 2. 从静态映射表获取
    if stock_code in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[stock_code]

    # 3. 从数据源获取
    if data_manager is None:
        try:
            from data_provider.base import DataFetcherManager
            data_manager = DataFetcherManager()
        except Exception as e:
            logger.debug(f"无法初始化 DataFetcherManager: {e}")

    if data_manager:
        try:
            name = data_manager.get_stock_name(stock_code)
            if name:
                # 更新缓存
                STOCK_NAME_MAP[stock_code] = name
                return name
        except Exception as e:
            logger.debug(f"从数据源获取股票名称失败: {e}")

    # 4. 返回默认名称
    return f'股票{stock_code}'


@dataclass
class AnalysisResult:
    """
    AI 分析结果数据类 - 决策仪表盘版

    封装 Gemini 返回的分析结果，包含决策仪表盘和详细分析
    """
    code: str
    name: str

    # ========== 核心指标 ==========
    sentiment_score: int  # 综合评分 0-100 (>70强烈看多, >60看多, 40-60震荡, <40看空)
    trend_prediction: str  # 趋势预测：强烈看多/看多/震荡/看空/强烈看空
    operation_advice: str  # 操作建议：买入/加仓/持有/减仓/卖出/观望
    decision_type: str = "hold"  # 决策类型：buy/hold/sell（用于统计）
    confidence_level: str = "中"  # 置信度：高/中/低
    report_language: str = "zh"  # 报告输出语言：zh/en

    # ========== 决策仪表盘 (新增) ==========
    dashboard: Optional[Dict[str, Any]] = None  # 完整的决策仪表盘数据

    # ========== 走势分析 ==========
    trend_analysis: str = ""  # 走势形态分析（支撑位、压力位、趋势线等）
    short_term_outlook: str = ""  # 短期展望（1-3日）
    medium_term_outlook: str = ""  # 中期展望（1-2周）

    # ========== 技术面分析 ==========
    technical_analysis: str = ""  # 技术指标综合分析
    ma_analysis: str = ""  # 均线分析（多头/空头排列，金叉/死叉等）
    volume_analysis: str = ""  # 量能分析（放量/缩量，主力动向等）
    pattern_analysis: str = ""  # K线形态分析

    # ========== 基本面分析 ==========
    fundamental_analysis: str = ""  # 基本面综合分析
    sector_position: str = ""  # 板块地位和行业趋势
    company_highlights: str = ""  # 公司亮点/风险点

    # ========== 情绪面/消息面分析 ==========
    news_summary: str = ""  # 近期重要新闻/公告摘要
    market_sentiment: str = ""  # 市场情绪分析
    hot_topics: str = ""  # 相关热点话题

    # ========== 综合分析 ==========
    analysis_summary: str = ""  # 综合分析摘要
    key_points: str = ""  # 核心看点（3-5个要点）
    risk_warning: str = ""  # 风险提示
    buy_reason: str = ""  # 买入/卖出理由

    # ========== 元数据 ==========
    market_snapshot: Optional[Dict[str, Any]] = None  # 当日行情快照（展示用）
    technical_indicator_snapshot: Optional[Dict[str, Any]] = None  # 扩展技术指标快照（展示用）
    macro_snapshot: Optional[Dict[str, Any]] = None  # FRED 宏观指标快照（展示用）
    fundamental_snapshot: Optional[Dict[str, Any]] = None  # 结构化财报摘要（展示用）
    dividend_snapshot: Optional[Dict[str, Any]] = None  # 结构化分红指标（展示用）
    peer_valuation_snapshot: Optional[Dict[str, Any]] = None  # 同类型估值对比（展示用）
    insider_activity_snapshot: Optional[Dict[str, Any]] = None  # SEC 内部人申报快照（展示用）
    filing_references: Optional[List[Dict[str, Any]]] = None  # 财报/SEC 原文链接（展示用）
    news_context_snapshot: Optional[str] = None  # 搜索情报原文摘要（展示用）
    raw_response: Optional[str] = None  # 原始响应（调试用）
    search_performed: bool = False  # 是否执行了联网搜索
    data_sources: str = ""  # 数据来源说明
    success: bool = True
    error_message: Optional[str] = None

    # ========== 价格数据（分析时快照）==========
    current_price: Optional[float] = None  # 分析时的股价
    change_pct: Optional[float] = None     # 分析时的涨跌幅(%)

    # ========== 模型标记（Issue #528）==========
    model_used: Optional[str] = None  # 分析使用的 LLM 模型（完整名，如 gemini/gemini-2.0-flash）

    # ========== 历史对比（Report Engine P0）==========
    query_id: Optional[str] = None  # 本次分析 query_id，用于历史对比时排除本次记录

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'name': self.name,
            'sentiment_score': self.sentiment_score,
            'trend_prediction': self.trend_prediction,
            'operation_advice': self.operation_advice,
            'decision_type': self.decision_type,
            'confidence_level': self.confidence_level,
            'report_language': self.report_language,
            'dashboard': self.dashboard,  # 决策仪表盘数据
            'trend_analysis': self.trend_analysis,
            'short_term_outlook': self.short_term_outlook,
            'medium_term_outlook': self.medium_term_outlook,
            'technical_analysis': self.technical_analysis,
            'ma_analysis': self.ma_analysis,
            'volume_analysis': self.volume_analysis,
            'pattern_analysis': self.pattern_analysis,
            'fundamental_analysis': self.fundamental_analysis,
            'sector_position': self.sector_position,
            'company_highlights': self.company_highlights,
            'news_summary': self.news_summary,
            'market_sentiment': self.market_sentiment,
            'hot_topics': self.hot_topics,
            'analysis_summary': self.analysis_summary,
            'key_points': self.key_points,
            'risk_warning': self.risk_warning,
            'buy_reason': self.buy_reason,
            'market_snapshot': self.market_snapshot,
            'technical_indicator_snapshot': self.technical_indicator_snapshot,
            'macro_snapshot': self.macro_snapshot,
            'fundamental_snapshot': self.fundamental_snapshot,
            'dividend_snapshot': self.dividend_snapshot,
            'peer_valuation_snapshot': self.peer_valuation_snapshot,
            'insider_activity_snapshot': self.insider_activity_snapshot,
            'filing_references': self.filing_references,
            'news_context_snapshot': self.news_context_snapshot,
            'search_performed': self.search_performed,
            'success': self.success,
            'error_message': self.error_message,
            'current_price': self.current_price,
            'change_pct': self.change_pct,
            'model_used': self.model_used,
        }

    def get_core_conclusion(self) -> str:
        """获取核心结论（一句话）"""
        if self.dashboard and 'core_conclusion' in self.dashboard:
            return self.dashboard['core_conclusion'].get('one_sentence', self.analysis_summary)
        return self.analysis_summary

    def get_position_advice(self, has_position: bool = False) -> str:
        """获取持仓建议"""
        if self.dashboard and 'core_conclusion' in self.dashboard:
            pos_advice = self.dashboard['core_conclusion'].get('position_advice', {})
            if has_position:
                return pos_advice.get('has_position', self.operation_advice)
            return pos_advice.get('no_position', self.operation_advice)
        return self.operation_advice

    def get_sniper_points(self) -> Dict[str, str]:
        """获取狙击点位"""
        if self.dashboard and 'battle_plan' in self.dashboard:
            return self.dashboard['battle_plan'].get('sniper_points', {})
        return {}

    def get_checklist(self) -> List[str]:
        """获取检查清单"""
        if self.dashboard and 'battle_plan' in self.dashboard:
            return self.dashboard['battle_plan'].get('action_checklist', [])
        return []

    def get_risk_alerts(self) -> List[str]:
        """获取风险警报"""
        if self.dashboard and 'intelligence' in self.dashboard:
            return self.dashboard['intelligence'].get('risk_alerts', [])
        return []

    def get_emoji(self) -> str:
        """根据操作建议返回对应 emoji"""
        _, emoji, _ = get_signal_level(
            self.operation_advice,
            self.sentiment_score,
            self.report_language,
        )
        return emoji

    def get_confidence_stars(self) -> str:
        """返回置信度星级"""
        star_map = {
            "高": "⭐⭐⭐",
            "high": "⭐⭐⭐",
            "中": "⭐⭐",
            "medium": "⭐⭐",
            "低": "⭐",
            "low": "⭐",
        }
        return star_map.get(str(self.confidence_level or "").strip().lower(), "⭐⭐")


class GeminiAnalyzer:
    """
    Gemini AI 分析器

    职责：
    1. 调用 Google Gemini API 进行股票分析
    2. 结合预先搜索的新闻和技术面数据生成分析报告
    3. 解析 AI 返回的 JSON 格式结果

    使用方式：
        analyzer = GeminiAnalyzer()
        result = analyzer.analyze(context, news_context)
    """

    # ========================================
    # 系统提示词 - 决策仪表盘 v2.0
    # ========================================
    # 输出格式升级：从简单信号升级为决策仪表盘
    # 核心模块：核心结论 + 数据透视 + 舆情情报 + 作战计划
    # ========================================

    LEGACY_DEFAULT_SYSTEM_PROMPT = """你是一位专注于趋势交易的{market_placeholder}投资分析师，负责生成专业的【决策仪表盘】分析报告。

{guidelines_placeholder}

""" + CORE_TRADING_SKILL_POLICY_ZH + """

## 输出格式：决策仪表盘 JSON

请严格按照以下 JSON 格式输出，这是一个完整的【决策仪表盘】：

```json
{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数（必须等于 dashboard.scorecard.overall_score；这是多维综合分，不是单纯技术面分）,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "一句话核心结论（50-90字，直接告诉用户做什么；必须同时引用至少2个维度，不能只复述均线/乖离率）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {
                "no_position": "空仓者建议：具体操作指引",
                "has_position": "持仓者建议：具体操作指引"
            }
        },

        "scorecard": {
            "overall_score": 0-100整数,
            "score_method": "综合评分 = 技术面25% + 基本面25% + 估值20% + 新闻/情绪15% + 宏观/风险15%",
            "dimensions": {
                "technical": {"label": "技术面", "score": 0-100整数, "weight": 25, "evidence": "趋势、均线、量能、波动率等证据"},
                "fundamental": {"label": "基本面", "score": 0-100整数, "weight": 25, "evidence": "收入、利润率、现金流、资产负债、ROE/FCF质量等证据"},
                "valuation": {"label": "估值", "score": 0-100整数, "weight": 20, "evidence": "PE/PB/PS/EV等与同类型公司或历史区间的比较"},
                "news_sentiment": {"label": "新闻/情绪", "score": 0-100整数, "weight": 15, "evidence": "新闻、公告、分析师观点、社交/舆情催化与风险"},
                "macro_risk": {"label": "宏观/风险", "score": 0-100整数, "weight": 15, "evidence": "利率、通胀、美元、行业周期、监管/诉讼等风险背景"}
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "均线排列状态描述",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": 当前价格数值,
                "ma5": MA5数值,
                "ma10": MA10数值,
                "ma20": MA20数值,
                "bias_ma5": 乖离率百分比数值,
                "bias_status": "安全/警戒/危险",
                "support_level": 支撑位价格,
                "resistance_level": 压力位价格
            },
            "volume_analysis": {
                "volume_ratio": 量比数值,
                "volume_status": "放量/缩量/平量",
                "turnover_rate": 换手率百分比,
                "volume_meaning": "量能含义解读（如：缩量回调表示抛压减轻）"
            },
            "chip_structure": {
                "profit_ratio": 获利比例,
                "avg_cost": 平均成本,
                "concentration": 筹码集中度,
                "chip_health": "健康/一般/警惕"
            }
        },

        "intelligence": {
            "latest_news": "【最新消息】近期重要新闻摘要",
            "risk_alerts": ["风险点1：具体描述", "风险点2：具体描述"],
            "positive_catalysts": ["利好1：具体描述", "利好2：具体描述"],
            "earnings_outlook": "业绩预期分析（基于年报预告、业绩快报等）",
            "sentiment_summary": "舆情情绪一句话总结"
        },

        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想买入点：XX元（在MA5附近）",
                "secondary_buy": "次优买入点：XX元（在MA10附近）",
                "stop_loss": "止损位：XX元（跌破MA20或X%）",
                "take_profit": "目标位：XX元（前高/整数关口）"
            },
            "position_strategy": {
                "suggested_position": "建议仓位：X成",
                "entry_plan": "分批建仓策略描述",
                "risk_control": "风控策略描述"
            },
            "action_checklist": [
                "✅/⚠️/❌ 检查项1：多头排列",
                "✅/⚠️/❌ 检查项2：乖离率合理（强势趋势可放宽）",
                "✅/⚠️/❌ 检查项3：量能配合",
                "✅/⚠️/❌ 检查项4：无重大利空",
                "✅/⚠️/❌ 检查项5：筹码健康",
                "✅/⚠️/❌ 检查项6：PE估值合理"
            ]
        }
    },

    "analysis_summary": "100字综合分析摘要",
    "key_points": "3-5个核心看点，逗号分隔",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由，引用交易理念",

    "trend_analysis": "走势形态分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技术面综合分析",
    "ma_analysis": "均线系统分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K线形态分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板块行业分析",
    "company_highlights": "公司亮点/风险",
    "news_summary": "新闻摘要",
    "market_sentiment": "市场情绪",
    "hot_topics": "相关热点",

    "search_performed": true/false,
    "data_sources": "数据来源说明"
}
```

## 评分标准

### 多维评分口径（必须执行）
- sentiment_score 必须等于 dashboard.scorecard.overall_score。
- overall_score 必须按固定权重计算：技术面25%、基本面25%、估值20%、新闻/情绪15%、宏观/风险15%。
- 技术面只能占 25%，不能因为短线均线/乖离率单独决定总分。
- 基本面分必须参考财报、利润质量、现金流、资产负债表、长期竞争力；数据缺失时给中性偏低分，并在 evidence 写明缺口。
- 估值分必须参考同类型公司/历史区间/增长预期，不能只看绝对 PE/PB。
- 核心洞察必须综合长期基本面、估值、消息/情绪、宏观风险和技术位置，不得只写 MA5/MA20/RSI/量能。

### 强烈买入（80-100分）：
- ✅ 多头排列：MA5 > MA10 > MA20
- ✅ 低乖离率：<2%，最佳买点
- ✅ 缩量回调或放量突破
- ✅ 筹码集中健康
- ✅ 消息面有利好催化

### 买入（60-79分）：
- ✅ 多头排列或弱势多头
- ✅ 乖离率 <5%
- ✅ 量能正常
- ⚪ 允许一项次要条件不满足

### 观望（40-59分）：
- ⚠️ 乖离率 >5%（追高风险）
- ⚠️ 均线缠绕趋势不明
- ⚠️ 有风险事件

### 卖出/减仓（0-39分）：
- ❌ 空头排列
- ❌ 跌破MA20
- ❌ 放量下跌
- ❌ 重大利空

## 决策仪表盘核心原则

1. **核心结论先行**：一句话说清该买该卖
2. **分持仓建议**：空仓者和持仓者给不同建议
3. **精确狙击点**：必须给出具体价格，不说模糊的话
4. **检查清单可视化**：用 ✅⚠️❌ 明确显示每项检查结果
5. **风险优先级**：舆情中的风险点要醒目标出"""

    SYSTEM_PROMPT = """你是一位{market_placeholder}投资分析师，负责生成专业的【决策仪表盘】分析报告。

{guidelines_placeholder}

{default_skill_policy_section}
{skills_section}

## 输出格式：决策仪表盘 JSON

请严格按照以下 JSON 格式输出，这是一个完整的【决策仪表盘】：

```json
{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数（必须等于 dashboard.scorecard.overall_score；这是多维综合分，不是单纯技术面分）,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "一句话核心结论（50-90字，直接告诉用户做什么；必须同时引用至少2个维度，不能只复述均线/乖离率）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {
                "no_position": "空仓者建议：具体操作指引",
                "has_position": "持仓者建议：具体操作指引"
            }
        },

        "scorecard": {
            "overall_score": 0-100整数,
            "score_method": "综合评分 = 技术面25% + 基本面25% + 估值20% + 新闻/情绪15% + 宏观/风险15%",
            "dimensions": {
                "technical": {"label": "技术面", "score": 0-100整数, "weight": 25, "evidence": "趋势、均线、量能、波动率等证据"},
                "fundamental": {"label": "基本面", "score": 0-100整数, "weight": 25, "evidence": "收入、利润率、现金流、资产负债、ROE/FCF质量等证据"},
                "valuation": {"label": "估值", "score": 0-100整数, "weight": 20, "evidence": "PE/PB/PS/EV等与同类型公司或历史区间的比较"},
                "news_sentiment": {"label": "新闻/情绪", "score": 0-100整数, "weight": 15, "evidence": "新闻、公告、分析师观点、社交/舆情催化与风险"},
                "macro_risk": {"label": "宏观/风险", "score": 0-100整数, "weight": 15, "evidence": "利率、通胀、美元、行业周期、监管/诉讼等风险背景"}
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "均线排列状态描述",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": 当前价格数值,
                "ma5": MA5数值,
                "ma10": MA10数值,
                "ma20": MA20数值,
                "bias_ma5": 乖离率百分比数值,
                "bias_status": "安全/警戒/危险",
                "support_level": 支撑位价格,
                "resistance_level": 压力位价格
            },
            "volume_analysis": {
                "volume_ratio": 量比数值,
                "volume_status": "放量/缩量/平量",
                "turnover_rate": 换手率百分比,
                "volume_meaning": "量能含义解读（如：缩量回调表示抛压减轻）"
            },
            "chip_structure": {
                "profit_ratio": 获利比例,
                "avg_cost": 平均成本,
                "concentration": 筹码集中度,
                "chip_health": "健康/一般/警惕"
            }
        },

        "intelligence": {
            "latest_news": "【最新消息】近期重要新闻摘要",
            "risk_alerts": ["风险点1：具体描述", "风险点2：具体描述"],
            "positive_catalysts": ["利好1：具体描述", "利好2：具体描述"],
            "earnings_outlook": "业绩预期分析（基于年报预告、业绩快报等）",
            "sentiment_summary": "舆情情绪一句话总结"
        },

        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想入场位：XX元（满足主要技能触发条件）",
                "secondary_buy": "次优入场位：XX元（更保守或确认后执行）",
                "stop_loss": "止损位：XX元（失效条件或X%风险）",
                "take_profit": "目标位：XX元（按阻力位/风险回报比制定）"
            },
            "position_strategy": {
                "suggested_position": "建议仓位：X成",
                "entry_plan": "分批建仓策略描述",
                "risk_control": "风控策略描述"
            },
            "action_checklist": [
                "✅/⚠️/❌ 检查项1：当前结构是否满足激活技能条件",
                "✅/⚠️/❌ 检查项2：入场位置与风险回报是否合理",
                "✅/⚠️/❌ 检查项3：量价/波动/筹码是否支持判断",
                "✅/⚠️/❌ 检查项4：无重大利空",
                "✅/⚠️/❌ 检查项5：仓位与止损计划明确",
                "✅/⚠️/❌ 检查项6：估值/业绩/催化与结论匹配"
            ]
        }
    },

    "analysis_summary": "100字综合分析摘要",
    "key_points": "3-5个核心看点，逗号分隔",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由，引用激活技能或风险框架",

    "trend_analysis": "走势形态分析",
    "short_term_outlook": "短期1-3日展望",
    "medium_term_outlook": "中期1-2周展望",
    "technical_analysis": "技术面综合分析",
    "ma_analysis": "均线系统分析",
    "volume_analysis": "量能分析",
    "pattern_analysis": "K线形态分析",
    "fundamental_analysis": "基本面分析",
    "sector_position": "板块行业分析",
    "company_highlights": "公司亮点/风险",
    "news_summary": "新闻摘要",
    "market_sentiment": "市场情绪",
    "hot_topics": "相关热点",

    "search_performed": true/false,
    "data_sources": "数据来源说明"
}
```

## 评分标准

### 多维评分口径（必须执行）
- sentiment_score 必须等于 dashboard.scorecard.overall_score。
- overall_score 必须按固定权重计算：技术面25%、基本面25%、估值20%、新闻/情绪15%、宏观/风险15%。
- 技术面只能占 25%，不能因为短线均线/乖离率单独决定总分。
- 基本面分必须参考财报、利润质量、现金流、资产负债表、长期竞争力；数据缺失时给中性偏低分，并在 evidence 写明缺口。
- 估值分必须参考同类型公司/历史区间/增长预期，不能只看绝对 PE/PB。
- 核心洞察必须综合长期基本面、估值、消息/情绪、宏观风险和技术位置，不得只写 MA5/MA20/RSI/量能。

### 强烈买入（80-100分）：
- ✅ 多个激活技能同时支持积极结论
- ✅ 上行空间、触发条件与风险回报清晰
- ✅ 关键风险已排查，仓位与止损计划明确
- ✅ 重要数据和情报结论彼此一致

### 买入（60-79分）：
- ✅ 主信号偏积极，但仍有少量待确认项
- ✅ 允许存在可控风险或次优入场点
- ✅ 需要在报告中明确补充观察条件

### 观望（40-59分）：
- ⚠️ 信号分歧较大，或缺乏足够确认
- ⚠️ 风险与机会大致均衡
- ⚠️ 更适合等待触发条件或回避不确定性

### 卖出/减仓（0-39分）：
- ❌ 主要结论转弱，风险明显高于收益
- ❌ 触发了止损/失效条件或重大利空
- ❌ 现有仓位更需要保护而不是进攻

## 决策仪表盘核心原则

1. **核心结论先行**：一句话说清该买该卖
2. **分持仓建议**：空仓者和持仓者给不同建议
3. **精确狙击点**：必须给出具体价格，不说模糊的话
4. **检查清单可视化**：用 ✅⚠️❌ 明确显示每项检查结果
5. **风险优先级**：舆情中的风险点要醒目标出"""

    TEXT_SYSTEM_PROMPT = """你是一位专业的股票分析助手。

- 回答必须基于用户提供的数据与上下文
- 若信息不足，要明确指出不确定性
- 不要编造价格、财报或新闻事实
"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        config: Optional[Config] = None,
        skills: Optional[List[str]] = None,
        skill_instructions: Optional[str] = None,
        default_skill_policy: Optional[str] = None,
        use_legacy_default_prompt: Optional[bool] = None,
    ):
        """Initialize LLM Analyzer via LiteLLM.

        Args:
            api_key: Ignored (kept for backward compatibility). Keys are loaded from config.
        """
        self._config_override = config
        self._requested_skills = list(skills) if skills is not None else None
        self._skill_instructions_override = skill_instructions
        self._default_skill_policy_override = default_skill_policy
        self._use_legacy_default_prompt_override = use_legacy_default_prompt
        self._resolved_prompt_state: Optional[Dict[str, Any]] = None
        self._router = None
        self._litellm_available = False
        self._init_litellm()
        if not self._litellm_available:
            logger.warning("No LLM configured (LITELLM_MODEL / API keys), AI analysis will be unavailable")

    def _get_runtime_config(self) -> Config:
        """Return the runtime config, honoring injected overrides for tests/pipeline."""
        return getattr(self, "_config_override", None) or get_config()

    def _get_skill_prompt_sections(self) -> tuple[str, str, bool]:
        """Resolve skill instructions + default baseline + prompt mode."""
        skill_instructions = getattr(self, "_skill_instructions_override", None)
        default_skill_policy = getattr(self, "_default_skill_policy_override", None)
        use_legacy_default_prompt = getattr(self, "_use_legacy_default_prompt_override", None)

        if skill_instructions is not None and default_skill_policy is not None:
            return (
                skill_instructions,
                default_skill_policy,
                bool(use_legacy_default_prompt) if use_legacy_default_prompt is not None else False,
            )

        resolved_state = getattr(self, "_resolved_prompt_state", None)
        if resolved_state is None:
            from src.agent.factory import resolve_skill_prompt_state

            prompt_state = resolve_skill_prompt_state(
                self._get_runtime_config(),
                skills=getattr(self, "_requested_skills", None),
            )
            resolved_state = {
                "skill_instructions": prompt_state.skill_instructions,
                "default_skill_policy": prompt_state.default_skill_policy,
                "use_legacy_default_prompt": bool(getattr(prompt_state, "use_legacy_default_prompt", False)),
            }
            self._resolved_prompt_state = resolved_state

        return (
            skill_instructions if skill_instructions is not None else resolved_state.get("skill_instructions", ""),
            default_skill_policy if default_skill_policy is not None else resolved_state.get("default_skill_policy", ""),
            (
                use_legacy_default_prompt
                if use_legacy_default_prompt is not None
                else bool(resolved_state.get("use_legacy_default_prompt", False))
            ),
        )

    def _get_analysis_system_prompt(self, report_language: str, stock_code: str = "") -> str:
        """Build the analyzer system prompt with output-language guidance."""
        lang = normalize_report_language(report_language)
        market_role = get_market_role(stock_code, lang)
        market_guidelines = get_market_guidelines(stock_code, lang)
        skill_instructions, default_skill_policy, use_legacy_default_prompt = self._get_skill_prompt_sections()
        if use_legacy_default_prompt:
            base_prompt = self.LEGACY_DEFAULT_SYSTEM_PROMPT.replace(
                "{market_placeholder}", market_role
            ).replace(
                "{guidelines_placeholder}", market_guidelines
            )
        else:
            skills_section = ""
            if skill_instructions:
                skills_section = f"## 激活的交易技能\n\n{skill_instructions}\n"
            default_skill_policy_section = ""
            if default_skill_policy:
                default_skill_policy_section = f"{default_skill_policy}\n"
            base_prompt = (
                self.SYSTEM_PROMPT.replace("{market_placeholder}", market_role)
                .replace("{guidelines_placeholder}", market_guidelines)
                .replace("{default_skill_policy_section}", default_skill_policy_section)
                .replace("{skills_section}", skills_section)
            )
        if lang == "en":
            return base_prompt + """

## Output Language (highest priority)

- Keep all JSON keys unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in English.
- Use the common English company name when you are confident; otherwise keep the original listed company name instead of inventing one.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, nested dashboard text, checklist items, and all narrative summaries.
"""
        return base_prompt + """

## 输出语言（最高优先级）

- 所有 JSON 键名保持不变。
- `decision_type` 必须保持为 `buy|hold|sell`。
- 所有面向用户的人类可读文本值必须使用中文。
"""

    def _has_channel_config(self, config: Config) -> bool:
        """Check if multi-channel config (channels / YAML / legacy model_list) is active."""
        return bool(config.llm_model_list) and not all(
            e.get('model_name', '').startswith('__legacy_') for e in config.llm_model_list
        )

    def _init_litellm(self) -> None:
        """Initialize litellm Router from channels / YAML / legacy keys."""
        config = self._get_runtime_config()
        litellm_model = config.litellm_model
        if not litellm_model:
            logger.warning("Analyzer LLM: LITELLM_MODEL not configured")
            return

        self._litellm_available = True

        # --- Channel / YAML path: build Router from pre-built model_list ---
        if self._has_channel_config(config):
            model_list = config.llm_model_list
            self._router = Router(
                model_list=model_list,
                routing_strategy="simple-shuffle",
                num_retries=2,
            )
            unique_models = list(dict.fromkeys(
                e['litellm_params']['model'] for e in model_list
            ))
            logger.info(
                f"Analyzer LLM: Router initialized from channels/YAML — "
                f"{len(model_list)} deployment(s), models: {unique_models}"
            )
            return

        # --- Legacy path: build Router for multi-key, or use single key ---
        keys = get_api_keys_for_model(litellm_model, config)

        if len(keys) > 1:
            # Build legacy Router for primary model multi-key load-balancing
            extra_params = extra_litellm_params(litellm_model, config)
            legacy_model_list = [
                {
                    "model_name": litellm_model,
                    "litellm_params": {
                        "model": litellm_model,
                        "api_key": k,
                        **extra_params,
                    },
                }
                for k in keys
            ]
            self._router = Router(
                model_list=legacy_model_list,
                routing_strategy="simple-shuffle",
                num_retries=2,
            )
            logger.info(
                f"Analyzer LLM: Legacy Router initialized with {len(keys)} keys "
                f"for {litellm_model}"
            )
        elif keys:
            logger.info(f"Analyzer LLM: litellm initialized (model={litellm_model})")
        else:
            logger.info(
                f"Analyzer LLM: litellm initialized (model={litellm_model}, "
                f"API key from environment)"
            )

    def is_available(self) -> bool:
        """Check if LiteLLM is properly configured with at least one API key."""
        return self._router is not None or self._litellm_available

    def _dispatch_litellm_completion(
        self,
        model: str,
        call_kwargs: Dict[str, Any],
        *,
        config: Config,
        use_channel_router: bool,
        router_model_names: set[str],
    ) -> Any:
        """Dispatch a LiteLLM completion through router or direct fallback."""
        effective_kwargs = dict(call_kwargs)
        if use_channel_router and self._router and model in router_model_names:
            return self._router.completion(**effective_kwargs)
        if self._router and model == config.litellm_model and not use_channel_router:
            return self._router.completion(**effective_kwargs)

        keys = get_api_keys_for_model(model, config)
        if keys:
            effective_kwargs["api_key"] = keys[0]
        effective_kwargs.update(extra_litellm_params(model, config))
        return litellm.completion(**effective_kwargs)

    def _normalize_usage(self, usage_obj: Any) -> Dict[str, Any]:
        """Normalize usage objects from LiteLLM responses/chunks."""
        if not usage_obj:
            return {}

        def _get_value(key: str) -> int:
            if isinstance(usage_obj, dict):
                return int(usage_obj.get(key) or 0)
            return int(getattr(usage_obj, key, 0) or 0)

        return {
            "prompt_tokens": _get_value("prompt_tokens"),
            "completion_tokens": _get_value("completion_tokens"),
            "total_tokens": _get_value("total_tokens"),
        }

    def _extract_stream_text(self, chunk: Any) -> str:
        """Extract provider-agnostic text delta from a LiteLLM streaming chunk."""
        choices = chunk.get("choices") if isinstance(chunk, dict) else getattr(chunk, "choices", None)
        if not choices:
            return ""

        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else getattr(choice, "delta", None)
        message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)

        content: Any = None
        if isinstance(delta, dict):
            content = delta.get("content")
        elif isinstance(delta, str):
            content = delta
        elif delta is not None:
            content = getattr(delta, "content", None)

        if content is None:
            if isinstance(message, dict):
                content = message.get("content")
            elif message is not None:
                content = getattr(message, "content", None)

        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)

        return content if isinstance(content, str) else ""

    def _consume_litellm_stream(
        self,
        stream_response: Any,
        *,
        model: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Consume a LiteLLM stream into a single text payload."""
        chunks: List[str] = []
        usage: Dict[str, Any] = {}
        chars_received = 0
        next_emit_at = 1

        try:
            for chunk in stream_response:
                chunk_usage = chunk.get("usage") if isinstance(chunk, dict) else getattr(chunk, "usage", None)
                normalized_usage = self._normalize_usage(chunk_usage)
                if normalized_usage:
                    usage = normalized_usage

                delta_text = self._extract_stream_text(chunk)
                if not delta_text:
                    continue

                chunks.append(delta_text)
                chars_received += len(delta_text)
                if progress_callback and chars_received >= next_emit_at:
                    progress_callback(chars_received)
                    next_emit_at = chars_received + 160
        except Exception as exc:
            raise _LiteLLMStreamError(
                f"{model} stream interrupted: {exc}",
                partial_received=chars_received > 0,
            ) from exc

        response_text = "".join(chunks).strip()
        if not response_text:
            raise _LiteLLMStreamError(
                f"{model} stream returned empty response",
                partial_received=False,
            )

        if progress_callback and chars_received > 0:
            progress_callback(chars_received)

        return response_text, usage

    def _call_litellm(
        self,
        prompt: str,
        generation_config: dict,
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Call LLM via litellm with fallback across configured models.

        When channels/YAML are configured, every model goes through the Router
        (which handles per-model key selection, load balancing, and retries).
        In legacy mode, the primary model may use the Router while fallback
        models fall back to direct litellm.completion().

        Args:
            prompt: User prompt text.
            generation_config: Dict with optional keys: temperature, max_output_tokens, max_tokens.

        Returns:
            Tuple of (response text, model_used, usage). On success model_used is the full model
            name and usage is a dict with prompt_tokens, completion_tokens, total_tokens.
        """
        config = self._get_runtime_config()
        max_tokens = (
            generation_config.get('max_output_tokens')
            or generation_config.get('max_tokens')
            or 8192
        )
        requested_temperature = generation_config.get('temperature', 0.7)

        models_to_try = [config.litellm_model] + (config.litellm_fallback_models or [])
        models_to_try = [m for m in models_to_try if m]

        use_channel_router = self._has_channel_config(config)

        last_error = None
        effective_system_prompt = system_prompt or self.TEXT_SYSTEM_PROMPT
        router_model_names = set(get_configured_llm_models(config.llm_model_list))
        for model in models_to_try:
            try:
                model_short = model.split("/")[-1] if "/" in model else model
                extra = get_thinking_extra_body(model_short)
                call_kwargs: Dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": effective_system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": normalize_litellm_temperature(
                        model,
                        requested_temperature,
                        model_list=config.llm_model_list,
                        request_overrides={"extra_body": extra} if extra else None,
                    ),
                    "max_tokens": max_tokens,
                }
                if extra:
                    call_kwargs["extra_body"] = extra

                if stream:
                    try:
                        stream_response = self._dispatch_litellm_completion(
                            model,
                            {**call_kwargs, "stream": True},
                            config=config,
                            use_channel_router=use_channel_router,
                            router_model_names=router_model_names,
                        )
                        response_text, usage = self._consume_litellm_stream(
                            stream_response,
                            model=model,
                            progress_callback=stream_progress_callback,
                        )
                        return response_text, model, usage
                    except _LiteLLMStreamError as exc:
                        if exc.partial_received:
                            logger.warning(
                                "[LiteLLM] %s stream failed after partial output, retrying non-stream for same model: %s",
                                model,
                                exc,
                            )
                        else:
                            logger.warning(
                                "[LiteLLM] %s stream unavailable before first chunk, falling back to non-stream: %s",
                                model,
                                exc,
                            )
                        last_error = exc
                    except Exception as exc:
                        logger.warning(
                            "[LiteLLM] %s stream request failed before first chunk, falling back to non-stream: %s",
                            model,
                            exc,
                        )

                response = self._dispatch_litellm_completion(
                    model,
                    call_kwargs,
                    config=config,
                    use_channel_router=use_channel_router,
                    router_model_names=router_model_names,
                )

                if response and response.choices and response.choices[0].message.content:
                    usage = self._normalize_usage(getattr(response, "usage", None))
                    return (response.choices[0].message.content, model, usage)
                raise ValueError("LLM returned empty response")

            except Exception as e:
                logger.warning(f"[LiteLLM] {model} failed: {e}")
                last_error = e
                continue

        raise Exception(f"All LLM models failed (tried {len(models_to_try)} model(s)). Last error: {last_error}")

    def generate_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """Public entry point for free-form text generation.

        External callers (e.g. MarketAnalyzer) must use this method instead of
        calling _call_litellm() directly or accessing private attributes such as
        _litellm_available, _router, _model, _use_openai, or _use_anthropic.

        Args:
            prompt:      Text prompt to send to the LLM.
            max_tokens:  Maximum tokens in the response (default 2048).
            temperature: Sampling temperature (default 0.7).

        Returns:
            Response text, or None if the LLM call fails (error is logged).
        """
        try:
            result = self._call_litellm(
                prompt,
                generation_config={"max_tokens": max_tokens, "temperature": temperature},
            )
            if isinstance(result, tuple):
                text, model_used, usage = result
                persist_llm_usage(usage, model_used, call_type="market_review")
                return text
            return result
        except Exception as exc:
            logger.error("[generate_text] LLM call failed: %s", exc)
            return None

    def analyze(
        self, 
        context: Dict[str, Any],
        news_context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
    ) -> AnalysisResult:
        """
        分析单只股票
        
        流程：
        1. 格式化输入数据（技术面 + 新闻）
        2. 调用 Gemini API（带重试和模型切换）
        3. 解析 JSON 响应
        4. 返回结构化结果
        
        Args:
            context: 从 storage.get_analysis_context() 获取的上下文数据
            news_context: 预先搜索的新闻内容（可选）
            
        Returns:
            AnalysisResult 对象
        """
        def _emit_progress(progress: int, message: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(progress, message)
            except Exception as exc:
                logger.debug("[analyzer] progress callback skipped: %s", exc)

        code = context.get('code', 'Unknown')
        config = self._get_runtime_config()
        report_language = normalize_report_language(getattr(config, "report_language", "zh"))
        system_prompt = self._get_analysis_system_prompt(report_language, stock_code=code)
        
        # 请求前增加延时（防止连续请求触发限流）
        request_delay = config.gemini_request_delay
        if request_delay > 0:
            logger.debug(f"[LLM] 请求前等待 {request_delay:.1f} 秒...")
            _emit_progress(65, f"{code}：LLM 请求前等待 {request_delay:.1f} 秒")
            time.sleep(request_delay)
        
        # 优先从上下文获取股票名称（由 main.py 传入）
        name = context.get('stock_name')
        if not name or name.startswith('股票'):
            # 备选：从 realtime 中获取
            if 'realtime' in context and context['realtime'].get('name'):
                name = context['realtime']['name']
            else:
                # 最后从映射表获取
                name = STOCK_NAME_MAP.get(code, f'股票{code}')
        
        # 如果模型不可用，返回默认结果
        if not self.is_available():
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction='Sideways' if report_language == "en" else '震荡',
                operation_advice='Hold' if report_language == "en" else '持有',
                confidence_level='Low' if report_language == "en" else '低',
                analysis_summary='AI analysis is unavailable because no API key is configured.' if report_language == "en" else 'AI 分析功能未启用（未配置 API Key）',
                risk_warning='Configure an LLM API key (GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY) and retry.' if report_language == "en" else '请配置 LLM API Key（GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY）后重试',
                success=False,
                error_message='LLM API key is not configured' if report_language == "en" else 'LLM API Key 未配置',
                model_used=None,
                report_language=report_language,
            )
        
        try:
            # 格式化输入（包含技术面数据和新闻）
            prompt = self._format_prompt(context, name, news_context, report_language=report_language)
            
            config = self._get_runtime_config()
            model_name = config.litellm_model or "unknown"
            logger.info(f"========== AI 分析 {name}({code}) ==========")
            logger.info(f"[LLM配置] 模型: {model_name}")
            logger.info(f"[LLM配置] Prompt 长度: {len(prompt)} 字符")
            logger.info(f"[LLM配置] 是否包含新闻: {'是' if news_context else '否'}")

            # 记录完整 prompt 到日志（INFO级别记录摘要，DEBUG记录完整）
            prompt_preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
            logger.info(f"[LLM Prompt 预览]\n{prompt_preview}")
            logger.debug(f"=== 完整 Prompt ({len(prompt)}字符) ===\n{prompt}\n=== End Prompt ===")

            # 设置生成配置
            generation_config = {
                "temperature": config.llm_temperature,
                "max_output_tokens": 8192,
            }

            logger.info(f"[LLM调用] 开始调用 {model_name}...")
            _emit_progress(68, f"{name}：LLM 已接收请求，等待响应")

            # 使用 litellm 调用（支持完整性校验重试）
            current_prompt = prompt
            retry_count = 0
            max_retries = config.report_integrity_retry if config.report_integrity_enabled else 0

            while True:
                start_time = time.time()
                response_text, model_used, llm_usage = self._call_litellm(
                    current_prompt,
                    generation_config,
                    system_prompt=system_prompt,
                    stream=True,
                    stream_progress_callback=stream_progress_callback,
                )
                elapsed = time.time() - start_time

                # 记录响应信息
                logger.info(
                    f"[LLM返回] {model_name} 响应成功, 耗时 {elapsed:.2f}s, 响应长度 {len(response_text)} 字符"
                )
                response_preview = response_text[:300] + "..." if len(response_text) > 300 else response_text
                logger.info(f"[LLM返回 预览]\n{response_preview}")
                logger.debug(
                    f"=== {model_name} 完整响应 ({len(response_text)}字符) ===\n{response_text}\n=== End Response ==="
                )
                # Keep parser/retry progress monotonic so task progress/message never "goes backward".
                parse_progress = min(99, 93 + retry_count * 2)
                _emit_progress(parse_progress, f"{name}：LLM 返回完成，正在解析 JSON")

                # 解析响应
                result = self._parse_response(response_text, code, name)
                result.raw_response = response_text
                result.search_performed = bool(news_context)
                result.news_context_snapshot = news_context or ""
                result.market_snapshot = self._build_market_snapshot(context)
                result.technical_indicator_snapshot = self._build_technical_indicator_snapshot(context)
                result.macro_snapshot = self._build_macro_snapshot(context)
                result.fundamental_snapshot = self._build_fundamental_snapshot(context)
                result.dividend_snapshot = self._build_dividend_snapshot(context)
                result.peer_valuation_snapshot = self._build_peer_valuation_snapshot(context)
                result.insider_activity_snapshot = self._build_insider_activity_snapshot(context)
                result.filing_references = self._build_filing_references(context)
                result.model_used = model_used
                result.report_language = report_language

                # 内容完整性校验（可选）
                if not config.report_integrity_enabled:
                    break
                pass_integrity, missing_fields = self._check_content_integrity(result)
                if pass_integrity:
                    break
                if retry_count < max_retries:
                    current_prompt = self._build_integrity_retry_prompt(
                        prompt,
                        response_text,
                        missing_fields,
                        report_language=report_language,
                    )
                    retry_count += 1
                    logger.info(
                        "[LLM完整性] 必填字段缺失 %s，第 %d 次补全重试",
                        missing_fields,
                        retry_count,
                    )
                    retry_progress = min(99, 92 + retry_count * 2)
                    _emit_progress(
                        retry_progress,
                        f"{name}：报告字段不完整，正在补全重试（{retry_count}/{max_retries}）",
                    )
                else:
                    self._apply_placeholder_fill(result, missing_fields)
                    logger.warning(
                        "[LLM完整性] 必填字段缺失 %s，已占位补全，不阻塞流程",
                        missing_fields,
                    )
                    break

            persist_llm_usage(llm_usage, model_used, call_type="analysis", stock_code=code)

            logger.info(f"[LLM解析] {name}({code}) 分析完成: {result.trend_prediction}, 评分 {result.sentiment_score}")

            return result
            
        except Exception as e:
            logger.error(f"AI 分析 {name}({code}) 失败: {e}")
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction='Sideways' if report_language == "en" else '震荡',
                operation_advice='Hold' if report_language == "en" else '持有',
                confidence_level='Low' if report_language == "en" else '低',
                analysis_summary=(f'Analysis failed: {str(e)[:100]}' if report_language == "en" else f'分析过程出错: {str(e)[:100]}'),
                risk_warning='Analysis failed. Please retry later or review manually.' if report_language == "en" else '分析失败，请稍后重试或手动分析',
                success=False,
                error_message=str(e),
                model_used=None,
                report_language=report_language,
            )
    
    def _format_prompt(
        self, 
        context: Dict[str, Any], 
        name: str,
        news_context: Optional[str] = None,
        report_language: str = "zh",
    ) -> str:
        """
        格式化分析提示词（决策仪表盘 v2.0）
        
        包含：技术指标、实时行情（量比/换手率）、筹码分布、趋势分析、新闻
        
        Args:
            context: 技术面数据上下文（包含增强数据）
            name: 股票名称（默认值，可能被上下文覆盖）
            news_context: 预先搜索的新闻内容
        """
        code = context.get('code', 'Unknown')
        report_language = normalize_report_language(report_language)
        _, _, use_legacy_default_prompt = self._get_skill_prompt_sections()
        
        # 优先使用上下文中的股票名称（从 realtime_quote 获取）
        stock_name = context.get('stock_name', name)
        if not stock_name or stock_name == f'股票{code}':
            stock_name = STOCK_NAME_MAP.get(code, f'股票{code}')
            
        today = context.get('today', {})
        unknown_text = get_unknown_text(report_language)
        no_data_text = get_no_data_text(report_language)
        
        # ========== 构建决策仪表盘格式的输入 ==========
        prompt = f"""# 决策仪表盘分析请求

## 📊 股票基础信息
| 项目 | 数据 |
|------|------|
| 股票代码 | **{code}** |
| 股票名称 | **{stock_name}** |
| 分析日期 | {context.get('date', unknown_text)} |

---

## 📈 技术面数据

### 今日行情
| 指标 | 数值 |
|------|------|
| 收盘价 | {today.get('close', 'N/A')} 元 |
| 开盘价 | {today.get('open', 'N/A')} 元 |
| 最高价 | {today.get('high', 'N/A')} 元 |
| 最低价 | {today.get('low', 'N/A')} 元 |
| 涨跌幅 | {today.get('pct_chg', 'N/A')}% |
| 成交量 | {self._format_volume(today.get('volume'))} |
| 成交额 | {self._format_amount(today.get('amount'))} |

### 均线系统（关键判断指标）
| 均线 | 数值 | 说明 |
|------|------|------|
| MA5 | {today.get('ma5', 'N/A')} | 短期趋势线 |
| MA10 | {today.get('ma10', 'N/A')} | 中短期趋势线 |
| MA20 | {today.get('ma20', 'N/A')} | 中期趋势线 |
| 均线形态 | {context.get('ma_status', unknown_text)} | 多头/空头/缠绕 |
"""
        
        # 添加实时行情数据（量比、换手率等）
        if 'realtime' in context:
            rt = context['realtime']
            prompt += f"""
### 实时行情增强数据
| 指标 | 数值 | 解读 |
|------|------|------|
| 当前价格 | {rt.get('price', 'N/A')} 元 | |
| **量比** | **{rt.get('volume_ratio', 'N/A')}** | {rt.get('volume_ratio_desc', '')} |
| **换手率** | **{rt.get('turnover_rate', 'N/A')}%** | |
| 市盈率(动态) | {rt.get('pe_ratio', 'N/A')} | |
| 市净率 | {rt.get('pb_ratio', 'N/A')} | |
| 总市值 | {self._format_amount(rt.get('total_mv'))} | |
| 流通市值 | {self._format_amount(rt.get('circ_mv'))} | |
| 60日涨跌幅 | {rt.get('change_60d', 'N/A')}% | 中期表现 |
"""

        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        peer_valuation = (
            fundamental_context.get("peer_valuation", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        peer_rows = (
            peer_valuation.get("rows", [])
            if isinstance(peer_valuation, dict)
            else []
        )
        if isinstance(peer_rows, list) and peer_rows:
            basis = peer_valuation.get("comparison_basis") or "同类型/同行业可比公司"
            prompt += f"""
### 同类型估值对比（相对估值）
> 对比口径：{basis}

| 公司 | 当前价 | PE | PB | 市值 |
|------|--------|----|----|------|
"""
            for row in peer_rows[:6]:
                if not isinstance(row, dict):
                    continue
                label = f"{row.get('symbol', 'N/A')} {row.get('name', '')}".strip()
                if row.get("is_target"):
                    label = f"**{label}**"
                prompt += (
                    f"| {label} | {row.get('price', 'N/A')} | "
                    f"{row.get('pe_ratio', 'N/A')} | {row.get('pb_ratio', 'N/A')} | "
                    f"{row.get('market_cap_text', 'N/A')} |\n"
                )

            summary = peer_valuation.get("summary", {}) if isinstance(peer_valuation, dict) else {}
            if isinstance(summary, dict) and summary:
                prompt += (
                    f"\n> Peer 中位数：PE={summary.get('peer_median_pe_ratio', 'N/A')}，"
                    f"PB={summary.get('peer_median_pb_ratio', 'N/A')}；"
                    f"标的相对中位数：PE {summary.get('pe_ratio_vs_peer_median_pct', 'N/A')}%，"
                    f"PB {summary.get('pb_ratio_vs_peer_median_pct', 'N/A')}%。"
                    " 请据此判断估值是溢价、折价还是合理，并说明 peer 口径局限，结合增长/利润质量解释。"
                    "\n"
                )

        # 添加财报与分红（价值投资口径）
        earnings_block = (
            fundamental_context.get("earnings", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        earnings_data = (
            earnings_block.get("data", {})
            if isinstance(earnings_block, dict)
            else {}
        )
        financial_report = (
            earnings_data.get("financial_report", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        dividend_metrics = (
            earnings_data.get("dividend", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        if isinstance(financial_report, dict) or isinstance(dividend_metrics, dict):
            financial_report = financial_report if isinstance(financial_report, dict) else {}
            dividend_metrics = dividend_metrics if isinstance(dividend_metrics, dict) else {}
            def _display_value(value: Any) -> str:
                if value is None or value == "":
                    return "N/A"
                return str(value)

            ttm_cash_raw = dividend_metrics.get("ttm_cash_dividend_per_share", "N/A")
            ttm_yield_raw = dividend_metrics.get("ttm_dividend_yield_pct", "N/A")
            if (
                (ttm_yield_raw is None or str(ttm_yield_raw).strip().upper() in {"", "N/A", "NA"})
                and ttm_cash_raw is not None
            ):
                realtime_price = None
                realtime_block = context.get("realtime", {}) if isinstance(context, dict) else {}
                if isinstance(realtime_block, dict):
                    try:
                        realtime_price = float(realtime_block.get("price"))
                    except (TypeError, ValueError):
                        realtime_price = None
                try:
                    ttm_cash_float = float(ttm_cash_raw)
                except (TypeError, ValueError):
                    ttm_cash_float = None
                if ttm_cash_float is not None and realtime_price and realtime_price > 0:
                    ttm_yield_raw = round(ttm_cash_float / realtime_price * 100.0, 4)

            ttm_cash = _display_value(ttm_cash_raw)
            if isinstance(ttm_yield_raw, (int, float)):
                ttm_yield = f"{float(ttm_yield_raw):.4f}%"
            else:
                ttm_yield = _display_value(ttm_yield_raw)
            ttm_count = _display_value(dividend_metrics.get("ttm_event_count", "N/A"))
            report_date = financial_report.get("report_date", "N/A")
            prompt += f"""
### 财报与分红（价值投资口径）
| 指标 | 数值 | 说明 |
|------|------|------|
| 最近报告期 | {report_date} | 来自结构化财报字段 |
| 营业收入 | {financial_report.get('revenue', 'N/A')} | {financial_report.get('revenue_period', '')} |
| 归母净利润 | {financial_report.get('net_profit_parent', 'N/A')} | {financial_report.get('net_profit_parent_period', '')} |
| 经营现金流 | {financial_report.get('operating_cash_flow', 'N/A')} | {financial_report.get('operating_cash_flow_period', '')} |
| ROE | {financial_report.get('roe', 'N/A')} | {financial_report.get('roe_note', '')} |
| 近12个月每股现金分红 | {ttm_cash} | 仅现金分红、税前口径；美股优先用 Yahoo Finance 分红事件 |
| TTM 股息率 | {ttm_yield} | 公式：近12个月每股现金分红 / 当前价格 × 100% |
| TTM 分红事件数 | {ttm_count} | |

> 若上述字段为 N/A 或缺失，请明确写“数据缺失，无法判断”，禁止编造。
"""
            quarterly_trend = financial_report.get("quarterly_trend")
            if isinstance(quarterly_trend, list) and quarterly_trend:
                prompt += """
#### 最近季度趋势（结构化财报）
| 期间 | 收入 | 较前值 | YoY | 净利润 | 较前值 | YoY | 净利率 | FCF |
|------|------|--------|-----|--------|--------|-----|--------|-----|
"""
                for row in quarterly_trend[:5]:
                    if not isinstance(row, dict):
                        continue
                    def _trend_pct(value: Any) -> str:
                        if value is None or value == "":
                            return "N/A"
                        try:
                            return f"{float(value):+.2f}%"
                        except (TypeError, ValueError):
                            return str(value)
                    net_margin = row.get("net_margin_pct")
                    net_margin_text = "N/A" if net_margin is None else f"{float(net_margin):.2f}%"
                    prompt += (
                        f"| {row.get('period', 'N/A')} | {row.get('revenue', 'N/A')} | "
                        f"{_trend_pct(row.get('revenue_value_change_pct'))} | "
                        f"{_trend_pct(row.get('revenue_value_yoy_pct'))} | "
                        f"{row.get('net_profit_parent', 'N/A')} | "
                        f"{_trend_pct(row.get('net_profit_parent_value_change_pct'))} | "
                        f"{_trend_pct(row.get('net_profit_parent_value_yoy_pct'))} | "
                        f"{net_margin_text} | {row.get('free_cash_flow', 'N/A')} |\n"
                    )

            prompt += """
#### 三框架分析方法论要求
请把以下三个框架聚合进诊股判断，但不要写成冗长机构报告：
- 科技股/半导体财报深挖：先识别未来 1-3 年最重要的 1-3 个关键力量，再分析收入、利润率、现金流、产品/业务周期、管理层指引、竞争格局、估值隐含预期。
- 美国价值投资四维：ROE 可持续性、债务安全性、自由现金流质量、经济护城河。明确哪些是硬数据、哪些是定性初判。
- 美国市场情绪与风险预算：用利率、通胀、市场广度/技术热度、估值拥挤度解释当前应该提高、维持还是降低风险预算；它不能单独决定买卖。

请按“收入质量、盈利能力、现金流质量、资产负债、资本配置、红旗/绿灯”六个角度解读财报：
- 收入质量：收入规模、增速可持续性、是否依赖单一产品/地区/一次性项目。
- 盈利能力：净利率、ROE、EPS，说明高 ROE 是否受回购或低权益基数放大。
- 现金流质量：经营现金流/净利润、自由现金流/净利润，判断利润是否转化成现金。
- 资产负债：资产负债率、权益比率、流动性与利率环境压力。
- 资本配置：分红、回购、CapEx、并购，判断对股东是否友好。
- 红旗/绿灯：列出 2-3 个最重要的正面信号和 2-3 个需要跟踪的风险，不要只复述数字。
"""
            filings = (
                earnings_data.get("filings", {})
                if isinstance(earnings_data, dict)
                else {}
            )
            filing_refs = (
                filings.get("filing_references", [])
                if isinstance(filings, dict)
                else []
            )
            if isinstance(filing_refs, list) and filing_refs:
                prompt += """
#### SEC EDGAR 财报原文链接
| 类型 | 报告期 | 提交日期 | 原文链接 |
|------|--------|----------|----------|
"""
                for filing in filing_refs[:5]:
                    if not isinstance(filing, dict):
                        continue
                    url = (
                        filing.get("pdf_url")
                        or filing.get("document_url")
                        or filing.get("sec_url")
                        or filing.get("filing_detail_url")
                        or filing.get("inline_xbrl_url")
                    )
                    url = self._normalize_sec_archive_url(url)
                    if not url:
                        continue
                    prompt += (
                        f"| {filing.get('form', 'N/A')} | {filing.get('report_date', 'N/A')} | "
                        f"{filing.get('filing_date', 'N/A')} | {url} |\n"
                    )
                prompt += """
> 上述链接为公开财报原文。SEC 文件通常是 HTML/Inline XBRL；若披露包中存在 PDF 附件则优先使用 PDF。
"""

        insider_block = (
            fundamental_context.get("insider_activity", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        insider_data = (
            insider_block.get("data", {})
            if isinstance(insider_block, dict)
            else {}
        )
        insider_filings = (
            insider_data.get("recent_filings", [])
            if isinstance(insider_data, dict)
            else []
        )
        if isinstance(insider_filings, list) and insider_filings:
            prompt += """
### SEC 内部人申报（Form 3/4/5/144）
| 类型 | 报告期 | 提交日期 | 原文链接 |
|------|--------|----------|----------|
"""
            for filing in insider_filings[:6]:
                if not isinstance(filing, dict):
                    continue
                url = (
                    filing.get("document_url")
                    or filing.get("sec_url")
                    or filing.get("filing_detail_url")
                    or filing.get("inline_xbrl_url")
                )
                url = self._normalize_sec_archive_url(url)
                if not url:
                    continue
                prompt += (
                    f"| {filing.get('form', 'N/A')} | {filing.get('report_date', 'N/A')} | "
                    f"{filing.get('filing_date', 'N/A')} | {url} |\n"
                )
            prompt += """
> Form 4 通常用于内部人持股变动披露；这里先展示 SEC 原文链接，具体买卖方向需结合表内交易代码进一步确认。
"""

        macro_context = (
            fundamental_context.get("macro", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        indicators = (
            macro_context.get("indicators", {})
            if isinstance(macro_context, dict)
            else {}
        )
        if isinstance(indicators, dict) and indicators:
            prompt += """
### FRED 宏观环境（美股适用）
| 指标 | 数值 | 日期 | 说明 |
|------|------|------|------|
"""
            for indicator in indicators.values():
                if not isinstance(indicator, dict):
                    continue
                value = indicator.get("value", "N/A")
                unit = indicator.get("unit", "")
                prompt += (
                    f"| {indicator.get('label', indicator.get('series_id', 'N/A'))} | "
                    f"{value}{unit} | {indicator.get('date', 'N/A')} | "
                    f"{indicator.get('note', '')} |\n"
                )
            prompt += "\n> 请把宏观指标作为估值折现率、风险偏好和板块风格的背景变量，不要把它当作公司基本面本身。\n"

        # 添加筹码分布数据
        if 'chip' in context:
            chip = context['chip']
            profit_ratio = chip.get('profit_ratio', 0)
            prompt += f"""
### 筹码分布数据（效率指标）
| 指标 | 数值 | 健康标准 |
|------|------|----------|
| **获利比例** | **{profit_ratio:.1%}** | 70-90%时警惕 |
| 平均成本 | {chip.get('avg_cost', 'N/A')} 元 | 现价应高于5-15% |
| 90%筹码集中度 | {chip.get('concentration_90', 0):.2%} | <15%为集中 |
| 70%筹码集中度 | {chip.get('concentration_70', 0):.2%} | |
| 筹码状态 | {chip.get('chip_status', unknown_text)} | |
"""
        
        # 添加趋势分析结果（仅隐式内建 bull_trend 默认回退保留旧口径）
        if 'trend_analysis' in context:
            trend = _sanitize_trend_analysis_for_prompt(
                context['trend_analysis'],
                volume_change_ratio=context.get('volume_change_ratio'),
            )
            consistency_notes = trend.get('prompt_consistency_notes', [])
            if use_legacy_default_prompt:
                bias_warning = "🚨 超过5%，严禁追高！" if trend.get('bias_ma5', 0) > 5 else "✅ 安全范围"
                prompt += f"""
### 趋势分析预判（基于交易理念）
| 指标 | 数值 | 判定 |
|------|------|------|
| 趋势状态 | {trend.get('trend_status', unknown_text)} | |
| 均线排列 | {trend.get('ma_alignment', unknown_text)} | MA5>MA10>MA20为多头 |
| 趋势强度 | {trend.get('trend_strength', 0)}/100 | |
| **乖离率(MA5)** | **{trend.get('bias_ma5', 0):+.2f}%** | {bias_warning} |
| 乖离率(MA10) | {trend.get('bias_ma10', 0):+.2f}% | |
| 量能状态 | {trend.get('volume_status', unknown_text)} | {trend.get('volume_trend', '')} |
| 系统信号 | {trend.get('buy_signal', unknown_text)} | |
| 系统评分 | {trend.get('signal_score', 0)}/100 | |

#### 系统分析理由
**买入理由**：
{chr(10).join('- ' + r for r in trend.get('signal_reasons', ['无'])) if trend.get('signal_reasons') else '- 无'}

**风险因素**：
{chr(10).join('- ' + r for r in trend.get('risk_factors', ['无'])) if trend.get('risk_factors') else '- 无'}
"""
                if consistency_notes:
                    prompt += f"""

**一致性约束**：
{chr(10).join('- ' + note for note in consistency_notes)}
"""
            else:
                bias_warning = (
                    "🚨 偏离较大，需谨慎评估追高风险"
                    if trend.get('bias_ma5', 0) > 5
                    else "✅ 位置相对可控"
                )
                prompt += f"""
### 技术与结构分析（供激活技能判断参考）
| 指标 | 数值 | 说明 |
|------|------|------|
| 趋势状态 | {trend.get('trend_status', unknown_text)} | |
| 均线排列 | {trend.get('ma_alignment', unknown_text)} | 结合激活技能判断结构强弱 |
| 趋势强度 | {trend.get('trend_strength', 0)}/100 | |
| **价格位置(MA5)** | **{trend.get('bias_ma5', 0):+.2f}%** | {bias_warning} |
| 价格位置(MA10) | {trend.get('bias_ma10', 0):+.2f}% | |
| 量能状态 | {trend.get('volume_status', unknown_text)} | {trend.get('volume_trend', '')} |
| 系统信号 | {trend.get('buy_signal', unknown_text)} | |
| 系统评分 | {trend.get('signal_score', 0)}/100 | |

#### 系统分析理由
**支持因素**：
{chr(10).join('- ' + r for r in trend.get('signal_reasons', ['无'])) if trend.get('signal_reasons') else '- 无'}

**风险因素**：
{chr(10).join('- ' + r for r in trend.get('risk_factors', ['无'])) if trend.get('risk_factors') else '- 无'}
"""
                if consistency_notes:
                    prompt += f"""

**一致性约束**：
{chr(10).join('- ' + note for note in consistency_notes)}
"""

            def _fmt_indicator(value: Any, precision: int = 2) -> str:
                if value is None or value == "":
                    return "N/A"
                try:
                    number = float(value)
                    if not math.isfinite(number):
                        return "N/A"
                    return f"{number:.{precision}f}"
                except (TypeError, ValueError):
                    return str(value)

            extended_indicator_keys = (
                "ema10",
                "ma50",
                "ma200",
                "boll_mid",
                "boll_upper",
                "boll_lower",
                "atr14",
                "vwma20",
                "mfi14",
            )
            if any(trend.get(key) is not None for key in extended_indicator_keys):
                prompt += f"""

#### 扩展技术指标（参考 TradingAgents 指标口径）
| 指标 | 数值 | 用途 |
|------|------|------|
| EMA10 | {_fmt_indicator(trend.get('ema10'))} | 短线动量 |
| MA50 | {_fmt_indicator(trend.get('ma50'))} | 中期趋势 |
| MA200 | {_fmt_indicator(trend.get('ma200'))} | 长期趋势/牛熊分界 |
| Boll 中轨 | {_fmt_indicator(trend.get('boll_mid'))} | 20日均值 |
| Boll 上轨 | {_fmt_indicator(trend.get('boll_upper'))} | 波动上沿/压力 |
| Boll 下轨 | {_fmt_indicator(trend.get('boll_lower'))} | 波动下沿/支撑 |
| ATR14 | {_fmt_indicator(trend.get('atr14'))} | 波动率/止损距离 |
| VWMA20 | {_fmt_indicator(trend.get('vwma20'))} | 成交量加权趋势 |
| MFI14 | {_fmt_indicator(trend.get('mfi14'))} | 资金流强弱，>80偏热，<20偏冷 |

> 请把这些指标用于确认趋势、动量、波动率和量价配合；若 MA200 为 N/A，说明本地历史 K 线不足，不要编造长期趋势结论。
"""
        
        # 添加昨日对比数据
        if 'yesterday' in context:
            volume_change = context.get('volume_change_ratio', 'N/A')
            prompt += f"""
### 量价变化
- 成交量较昨日变化：{volume_change}倍
- 价格较昨日变化：{context.get('price_change_ratio', 'N/A')}%
"""
            parsed_volume_change = _safe_float(volume_change, default=math.nan)
            if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
                prompt += """
- ⚠️ 量能异常提示：成交量较昨日放大超过10倍，可能受异常数据或一次性冲量影响，必须降权解读，不能机械视为强确认信号
"""
        
        # 添加新闻搜索结果（重点区域）
        news_window_days: Optional[int] = None
        context_window = context.get("news_window_days")
        try:
            if context_window is not None:
                parsed_window = int(context_window)
                if parsed_window > 0:
                    news_window_days = parsed_window
        except (TypeError, ValueError):
            news_window_days = None

        if news_window_days is None:
            prompt_config = self._get_runtime_config()
            news_window_days = resolve_news_window_days(
                news_max_age_days=getattr(prompt_config, "news_max_age_days", 3),
                news_strategy_profile=getattr(prompt_config, "news_strategy_profile", "short"),
            )
        prompt += """
---

## 📰 舆情情报
"""
        if news_context:
            prompt += f"""
以下是 **{stock_name}({code})** 近{news_window_days}日的新闻搜索结果，请重点提取：
1. 🚨 **风险警报**：减持、处罚、利空
2. 🎯 **利好催化**：业绩、合同、政策
3. 📊 **业绩预期**：年报预告、业绩快报
4. 🕒 **时间规则（强制）**：
   - 输出到 `risk_alerts` / `positive_catalysts` / `latest_news` 的每一条都必须带具体日期（YYYY-MM-DD）
   - 超出近{news_window_days}日窗口的新闻一律忽略
   - 时间未知、无法确定发布日期的新闻一律忽略

```
{news_context}
```
"""
        else:
            prompt += """
未搜索到该股票近期的相关新闻。请主要依据技术面数据进行分析。
"""

        # 注入缺失数据警告
        if context.get('data_missing'):
            prompt += """
⚠️ **数据缺失警告**
由于接口限制，当前无法获取完整的实时行情和技术指标数据。
请 **忽略上述表格中的 N/A 数据**，重点依据 **【📰 舆情情报】** 中的新闻进行基本面和情绪面分析。
在回答技术面问题（如均线、乖离率）时，请直接说明“数据缺失，无法判断”，**严禁编造数据**。
"""

        # 明确的输出要求
        prompt += f"""
---

## ✅ 分析任务

请为 **{stock_name}({code})** 生成【决策仪表盘】，严格按照 JSON 格式输出。
"""
        if context.get('is_index_etf'):
            prompt += """
> ⚠️ **指数/ETF 分析约束**：该标的为指数跟踪型 ETF 或市场指数。
> - 风险分析仅关注：**指数走势、跟踪误差、市场流动性**
> - 严禁将基金公司的诉讼、声誉、高管变动纳入风险警报
> - 业绩预期基于**指数成分股整体表现**，而非基金公司财报
> - `risk_alerts` 中不得出现基金管理人相关的公司经营风险

"""
        prompt += f"""
### ⚠️ 重要：输出正确的股票名称格式
正确的股票名称格式为“股票名称（股票代码）”，例如“贵州茅台（600519）”。
如果上方显示的股票名称为"股票{code}"或不正确，请在分析开头**明确输出该股票的正确中文全称**。
"""
        if use_legacy_default_prompt:
            prompt += f"""

### 重点关注（必须明确回答）：
1. ❓ 是否满足 MA5>MA10>MA20 多头排列？
2. ❓ 当前乖离率是否在安全范围内（<5%）？—— 超过5%必须标注"严禁追高"
3. ❓ 量能是否配合（缩量回调/放量突破）？
4. ❓ 筹码结构是否健康？
5. ❓ 消息面有无重大利空？（减持、处罚、业绩变脸等）
"""
        else:
            prompt += f"""

### 重点关注（必须明确回答）：
1. ❓ 当前结构是否满足激活技能的关键触发条件？
2. ❓ 当前入场位置与风险回报是否合理？若偏离过大，请明确说明等待条件
3. ❓ 量能、波动与筹码结构是否支持当前结论？
4. ❓ 消息面有无重大利空或与技能结论冲突的信息？
5. ❓ 若结论成立，具体触发条件、止损位、观察点分别是什么？
"""
        prompt += f"""

### 决策仪表盘要求：
- **股票名称**：必须输出正确的中文全称（如"贵州茅台"而非"股票600519"）
- **核心结论**：一句话说清该买/该卖/该等
- **持仓分类建议**：空仓者怎么做 vs 持仓者怎么做
- **具体狙击点位**：买入价、止损价、目标价（精确到分）
- **检查清单**：每项用 ✅/⚠️/❌ 标记
- **消息面时间合规**：`latest_news`、`risk_alerts`、`positive_catalysts` 不得包含超出近{news_window_days}日或时间未知的信息
- **技术面一致性**：严禁把“空头排列”和“多头排列”等互斥结论同时当作有效依据；若基本面/事件面与技术面冲突，必须明确写“事件先行、技术待确认”或“基本面偏多，但技术面尚未确认”
 
请输出完整的 JSON 格式决策仪表盘。"""

        if report_language == "en":
            prompt += """

### Output language requirements (highest priority)
- Keep every JSON key exactly as defined above; do not translate keys.
- `decision_type` must remain `buy`, `hold`, or `sell`.
- All human-readable JSON values must be in English.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all nested dashboard text, checklist items, and every summary field.
- Use the common English company name when you are confident. If not, keep the listed company name rather than inventing one.
- When data is missing, explain it in English instead of Chinese.
"""
        else:
            prompt += f"""

### 输出语言要求（最高优先级）
- 所有 JSON 键名必须保持不变，不要翻译键名。
- `decision_type` 必须保持为 `buy`、`hold`、`sell`。
- 所有面向用户的人类可读文本值必须使用中文。
- 当数据缺失时，请使用中文直接说明“{no_data_text}，无法判断”。
"""
        
        return prompt
    
    def _format_volume(self, volume: Optional[float]) -> str:
        """格式化成交量显示"""
        if volume is None:
            return 'N/A'
        if volume >= 1e8:
            return f"{volume / 1e8:.2f} 亿股"
        elif volume >= 1e4:
            return f"{volume / 1e4:.2f} 万股"
        else:
            return f"{volume:.0f} 股"
    
    def _format_amount(self, amount: Optional[float]) -> str:
        """格式化成交额显示"""
        if amount is None:
            return 'N/A'
        if amount >= 1e8:
            return f"{amount / 1e8:.2f} 亿元"
        elif amount >= 1e4:
            return f"{amount / 1e4:.2f} 万元"
        else:
            return f"{amount:.0f} 元"

    def _format_percent(self, value: Optional[float]) -> str:
        """格式化百分比显示"""
        if value is None:
            return 'N/A'
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return 'N/A'

    def _format_price(self, value: Optional[float]) -> str:
        """格式化价格显示"""
        if value is None:
            return 'N/A'
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return 'N/A'

    def _build_market_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """构建当日行情快照（展示用）"""
        today = context.get('today', {}) or {}
        realtime = context.get('realtime', {}) or {}
        yesterday = context.get('yesterday', {}) or {}

        prev_close = yesterday.get('close')
        close = today.get('close')
        high = today.get('high')
        low = today.get('low')

        amplitude = None
        change_amount = None
        if prev_close not in (None, 0) and high is not None and low is not None:
            try:
                amplitude = (float(high) - float(low)) / float(prev_close) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                amplitude = None
        if prev_close is not None and close is not None:
            try:
                change_amount = float(close) - float(prev_close)
            except (TypeError, ValueError):
                change_amount = None

        snapshot = {
            "date": context.get('date', '未知'),
            "close": self._format_price(close),
            "open": self._format_price(today.get('open')),
            "high": self._format_price(high),
            "low": self._format_price(low),
            "prev_close": self._format_price(prev_close),
            "pct_chg": self._format_percent(today.get('pct_chg')),
            "change_amount": self._format_price(change_amount),
            "amplitude": self._format_percent(amplitude),
            "volume": self._format_volume(today.get('volume')),
            "amount": self._format_amount(today.get('amount')),
        }

        if realtime:
            snapshot.update({
                "price": self._format_price(realtime.get('price')),
                "volume_ratio": realtime.get('volume_ratio', 'N/A'),
                "turnover_rate": self._format_percent(realtime.get('turnover_rate')),
                "source": getattr(realtime.get('source'), 'value', realtime.get('source', 'N/A')),
            })

        return snapshot

    def _build_technical_indicator_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Extract extended technical indicators for Markdown/PDF reports."""
        trend_analysis = context.get("trend_analysis") if isinstance(context, dict) else None
        if not isinstance(trend_analysis, dict):
            return {}

        keys = (
            "ema10",
            "ma50",
            "ma200",
            "boll_mid",
            "boll_upper",
            "boll_lower",
            "atr14",
            "vwma20",
            "mfi14",
        )
        values = {key: trend_analysis.get(key) for key in keys}
        if all(value in (None, "", "N/A") for value in values.values()):
            return {}
        snapshot = dict(values)
        snapshot["source"] = trend_analysis.get("source") or "StockTrendAnalyzer"
        return snapshot

    def _build_filing_references(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract public filing links for Markdown/PDF reports."""
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        if not isinstance(fundamental_context, dict):
            return []
        earnings = fundamental_context.get("earnings")
        earnings_data = earnings.get("data") if isinstance(earnings, dict) else None
        filings = earnings_data.get("filings") if isinstance(earnings_data, dict) else None
        refs = filings.get("filing_references") if isinstance(filings, dict) else None
        if not isinstance(refs, list):
            return []

        normalized: List[Dict[str, Any]] = []
        seen = set()
        for item in refs:
            if not isinstance(item, dict):
                continue
            url = (
                item.get("pdf_url")
                or item.get("document_url")
                or item.get("sec_url")
                or item.get("filing_detail_url")
                or item.get("inline_xbrl_url")
            )
            url = self._normalize_sec_archive_url(url)
            if not url or url in seen:
                continue
            seen.add(url)
            normalized.append(
                {
                    "form": item.get("form"),
                    "report_date": item.get("report_date"),
                    "filing_date": item.get("filing_date"),
                    "url": url,
                    "pdf_url": item.get("pdf_url"),
                    "document_url": item.get("document_url") or item.get("sec_url"),
                    "sec_url": item.get("sec_url"),
                    "inline_xbrl_url": item.get("inline_xbrl_url"),
                    "filing_detail_url": item.get("filing_detail_url"),
                    "description": item.get("primary_doc_description"),
                }
            )
        return normalized

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

    def _build_macro_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Extract FRED macro indicators for Markdown/PDF reports."""
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        if not isinstance(fundamental_context, dict):
            return {}
        macro_context = fundamental_context.get("macro")
        if not isinstance(macro_context, dict):
            return {}
        indicators = macro_context.get("indicators")
        if not isinstance(indicators, dict) or not indicators:
            return {}

        return {
            "provider": macro_context.get("provider") or "FRED",
            "status": macro_context.get("status"),
            "indicators": [
                item
                for item in indicators.values()
                if isinstance(item, dict)
            ],
        }

    def _build_fundamental_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Extract the structured financial-report block for Markdown/PDF reports."""
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        if not isinstance(fundamental_context, dict):
            return {}
        earnings = fundamental_context.get("earnings")
        earnings_data = earnings.get("data") if isinstance(earnings, dict) else None
        financial_report = earnings_data.get("financial_report") if isinstance(earnings_data, dict) else None
        if not isinstance(financial_report, dict) or not financial_report:
            return {}

        return {
            "provider": fundamental_context.get("provider") or financial_report.get("source"),
            "form": financial_report.get("form"),
            "report_date": financial_report.get("report_date"),
            "filing_date": financial_report.get("filing_date"),
            "revenue": financial_report.get("revenue"),
            "revenue_period": financial_report.get("revenue_period"),
            "net_profit_parent": financial_report.get("net_profit_parent"),
            "net_profit_parent_period": financial_report.get("net_profit_parent_period"),
            "operating_cash_flow": financial_report.get("operating_cash_flow"),
            "operating_cash_flow_period": financial_report.get("operating_cash_flow_period"),
            "capital_expenditure": financial_report.get("capital_expenditure"),
            "capital_expenditure_period": financial_report.get("capital_expenditure_period"),
            "free_cash_flow": financial_report.get("free_cash_flow"),
            "roe": financial_report.get("roe"),
            "roe_note": financial_report.get("roe_note"),
            "assets": financial_report.get("assets"),
            "liabilities": financial_report.get("liabilities"),
            "shareholders_equity": financial_report.get("shareholders_equity"),
            "eps_diluted": financial_report.get("eps_diluted"),
            "cash_and_equivalents": financial_report.get("cash_and_equivalents"),
            "marketable_securities_current": financial_report.get("marketable_securities_current"),
            "marketable_securities_noncurrent": financial_report.get("marketable_securities_noncurrent"),
            "liquid_assets": financial_report.get("liquid_assets"),
            "commercial_paper": financial_report.get("commercial_paper"),
            "long_term_debt": financial_report.get("long_term_debt"),
            "interest_bearing_debt": financial_report.get("interest_bearing_debt"),
            "net_cash": financial_report.get("net_cash"),
            "revenue_value": financial_report.get("revenue_value"),
            "net_profit_parent_value": financial_report.get("net_profit_parent_value"),
            "operating_cash_flow_value": financial_report.get("operating_cash_flow_value"),
            "capital_expenditure_value": financial_report.get("capital_expenditure_value"),
            "free_cash_flow_value": financial_report.get("free_cash_flow_value"),
            "assets_value": financial_report.get("assets_value"),
            "liabilities_value": financial_report.get("liabilities_value"),
            "shareholders_equity_value": financial_report.get("shareholders_equity_value"),
            "cash_and_equivalents_value": financial_report.get("cash_and_equivalents_value"),
            "marketable_securities_current_value": financial_report.get("marketable_securities_current_value"),
            "marketable_securities_noncurrent_value": financial_report.get("marketable_securities_noncurrent_value"),
            "liquid_assets_value": financial_report.get("liquid_assets_value"),
            "commercial_paper_value": financial_report.get("commercial_paper_value"),
            "long_term_debt_value": financial_report.get("long_term_debt_value"),
            "interest_bearing_debt_value": financial_report.get("interest_bearing_debt_value"),
            "net_cash_value": financial_report.get("net_cash_value"),
            "net_margin_pct": financial_report.get("net_margin_pct"),
            "operating_cash_flow_to_net_income_pct": financial_report.get("operating_cash_flow_to_net_income_pct"),
            "free_cash_flow_to_net_income_pct": financial_report.get("free_cash_flow_to_net_income_pct"),
            "debt_to_assets_pct": financial_report.get("debt_to_assets_pct"),
            "interest_bearing_debt_to_assets_pct": financial_report.get("interest_bearing_debt_to_assets_pct"),
            "liquid_assets_to_interest_bearing_debt_pct": financial_report.get("liquid_assets_to_interest_bearing_debt_pct"),
            "equity_ratio_pct": financial_report.get("equity_ratio_pct"),
            "asset_to_equity": financial_report.get("asset_to_equity"),
            "quarterly_trend": financial_report.get("quarterly_trend") or [],
            "annual_trend": financial_report.get("annual_trend") or [],
        }

    def _build_dividend_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Extract structured dividend metrics for Markdown/PDF reports."""
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        if not isinstance(fundamental_context, dict):
            return {}
        earnings = fundamental_context.get("earnings")
        earnings_data = earnings.get("data") if isinstance(earnings, dict) else None
        dividend = earnings_data.get("dividend") if isinstance(earnings_data, dict) else None
        if not isinstance(dividend, dict) or not dividend:
            return {}

        ttm_cash = dividend.get("ttm_cash_dividend_per_share")
        ttm_yield = dividend.get("ttm_dividend_yield_pct")
        if ttm_yield is None or str(ttm_yield).strip().upper() in {"", "N/A", "NA"}:
            realtime = context.get("realtime", {}) if isinstance(context, dict) else {}
            try:
                price = float(realtime.get("price")) if isinstance(realtime, dict) else None
                cash = float(ttm_cash)
            except (TypeError, ValueError):
                price = None
                cash = None
            if price and price > 0 and cash is not None:
                ttm_yield = round(cash / price * 100.0, 4)

        return {
            "ttm_cash_dividend_per_share": ttm_cash,
            "ttm_dividend_yield_pct": ttm_yield,
            "ttm_event_count": dividend.get("ttm_event_count"),
            "latest_dividend_fact": dividend.get("latest_dividend_fact"),
            "source": dividend.get("source"),
            "events": dividend.get("events", []),
        }

    def _build_peer_valuation_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Extract peer valuation comparison for Markdown/PDF reports."""
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        if not isinstance(fundamental_context, dict):
            return {}
        peer_valuation = fundamental_context.get("peer_valuation")
        if not isinstance(peer_valuation, dict):
            return {}
        rows = peer_valuation.get("rows")
        if not isinstance(rows, list) or not rows:
            return {}
        return {
            "provider": peer_valuation.get("provider") or "Longbridge",
            "source": peer_valuation.get("source"),
            "status": peer_valuation.get("status"),
            "market": peer_valuation.get("market"),
            "target": peer_valuation.get("target"),
            "comparison_basis": peer_valuation.get("comparison_basis"),
            "rows": [row for row in rows if isinstance(row, dict)],
            "summary": peer_valuation.get("summary", {}),
        }

    def _build_insider_activity_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Extract SEC insider filing references for Markdown/PDF reports."""
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        if not isinstance(fundamental_context, dict):
            return {}
        insider_activity = fundamental_context.get("insider_activity")
        if not isinstance(insider_activity, dict):
            return {}
        data = insider_activity.get("data")
        if not isinstance(data, dict):
            return {}
        filings = data.get("recent_filings")
        if not isinstance(filings, list) or not filings:
            return {}
        normalized = [item for item in filings if isinstance(item, dict)]
        if not normalized:
            return {}
        return {
            "provider": fundamental_context.get("provider") or data.get("source") or "SEC EDGAR",
            "source": data.get("source"),
            "forms": data.get("forms", []),
            "recent_filings": normalized[:6],
        }

    def _check_content_integrity(self, result: AnalysisResult) -> Tuple[bool, List[str]]:
        """Delegate to module-level check_content_integrity."""
        return check_content_integrity(result)

    def _build_integrity_complement_prompt(self, missing_fields: List[str], report_language: str = "zh") -> str:
        """Build complement instruction for missing mandatory fields."""
        report_language = normalize_report_language(report_language)
        if report_language == "en":
            lines = ["### Completion requirements: fill the missing mandatory fields below and output the full JSON again:"]
            for f in missing_fields:
                if f == "sentiment_score":
                    lines.append("- sentiment_score: integer score from 0 to 100")
                elif f == "operation_advice":
                    lines.append("- operation_advice: localized action advice")
                elif f == "analysis_summary":
                    lines.append("- analysis_summary: concise analysis summary")
                elif f == "dashboard.core_conclusion.one_sentence":
                    lines.append("- dashboard.core_conclusion.one_sentence: one-line decision")
                elif f == "dashboard.intelligence.risk_alerts":
                    lines.append("- dashboard.intelligence.risk_alerts: risk alert list (can be empty)")
                elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                    lines.append("- dashboard.battle_plan.sniper_points.stop_loss: stop-loss level")
            return "\n".join(lines)

        lines = ["### 补全要求：请在上方分析基础上补充以下必填内容，并输出完整 JSON："]
        for f in missing_fields:
            if f == "sentiment_score":
                lines.append("- sentiment_score: 0-100 综合评分")
            elif f == "operation_advice":
                lines.append("- operation_advice: 买入/加仓/持有/减仓/卖出/观望")
            elif f == "analysis_summary":
                lines.append("- analysis_summary: 综合分析摘要")
            elif f == "dashboard.core_conclusion.one_sentence":
                lines.append("- dashboard.core_conclusion.one_sentence: 一句话决策")
            elif f == "dashboard.intelligence.risk_alerts":
                lines.append("- dashboard.intelligence.risk_alerts: 风险警报列表（可为空数组）")
            elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                lines.append("- dashboard.battle_plan.sniper_points.stop_loss: 止损价")
        return "\n".join(lines)

    def _build_integrity_retry_prompt(
        self,
        base_prompt: str,
        previous_response: str,
        missing_fields: List[str],
        report_language: str = "zh",
    ) -> str:
        """Build retry prompt using the previous response as the complement baseline."""
        complement = self._build_integrity_complement_prompt(missing_fields, report_language=report_language)
        previous_output = previous_response.strip()
        if normalize_report_language(report_language) == "en":
            prefix = "### The previous output is below. Complete the missing fields based on that output and return the full JSON again. Do not omit existing fields:"
        else:
            prefix = "### 上一次输出如下，请在该输出基础上补齐缺失字段，并重新输出完整 JSON。不要省略已有字段："
        return "\n\n".join([
            base_prompt,
            prefix,
            previous_output,
            complement,
        ])

    def _apply_placeholder_fill(self, result: AnalysisResult, missing_fields: List[str]) -> None:
        """Delegate to module-level apply_placeholder_fill."""
        apply_placeholder_fill(result, missing_fields)

    def _parse_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """
        解析 Gemini 响应（决策仪表盘版）
        
        尝试从响应中提取 JSON 格式的分析结果，包含 dashboard 字段
        如果解析失败，尝试智能提取或返回默认结果
        """
        try:
            report_language = normalize_report_language(
                getattr(self._get_runtime_config(), "report_language", "zh")
            )
            # 清理响应文本：移除 markdown 代码块标记
            cleaned_text = response_text
            if '```json' in cleaned_text:
                cleaned_text = cleaned_text.replace('```json', '').replace('```', '')
            elif '```' in cleaned_text:
                cleaned_text = cleaned_text.replace('```', '')
            
            # 尝试找到 JSON 内容
            json_start = cleaned_text.find('{')
            json_end = cleaned_text.rfind('}') + 1
            
            if json_start >= 0 and json_end > json_start:
                json_str = cleaned_text[json_start:json_end]
                
                # 尝试修复常见的 JSON 问题
                json_str = self._fix_json_string(json_str)
                
                data = json.loads(json_str)

                # Schema validation (lenient: on failure, continue with raw dict)
                try:
                    AnalysisReportSchema.model_validate(data)
                except Exception as e:
                    logger.warning(
                        "LLM report schema validation failed, continuing with raw dict: %s",
                        str(e)[:100],
                    )

                normalized_score = ensure_dashboard_scorecard_payload(data, report_language)
                # 提取 dashboard 数据（ensure 可能会为旧格式响应补出 dashboard.scorecard）
                dashboard = data.get('dashboard', None)

                # 优先使用 AI 返回的股票名称（如果原名称无效或包含代码）
                ai_stock_name = data.get('stock_name')
                if ai_stock_name and (name.startswith('股票') or name == code or 'Unknown' in name):
                    name = ai_stock_name

                # 解析所有字段，使用默认值防止缺失
                # 解析 decision_type，如果没有则根据 operation_advice 推断
                decision_type = data.get('decision_type', '')
                if not decision_type:
                    op = data.get('operation_advice', 'Hold' if report_language == "en" else '持有')
                    decision_type = infer_decision_type_from_advice(op, default='hold')
                
                return AnalysisResult(
                    code=code,
                    name=name,
                    # 核心指标
                    sentiment_score=int(normalized_score if normalized_score is not None else data.get('sentiment_score', 50)),
                    trend_prediction=data.get('trend_prediction', 'Sideways' if report_language == "en" else '震荡'),
                    operation_advice=data.get('operation_advice', 'Hold' if report_language == "en" else '持有'),
                    decision_type=decision_type,
                    confidence_level=localize_confidence_level(
                        data.get('confidence_level', 'Medium' if report_language == "en" else '中'),
                        report_language,
                    ),
                    report_language=report_language,
                    # 决策仪表盘
                    dashboard=dashboard,
                    # 走势分析
                    trend_analysis=data.get('trend_analysis', ''),
                    short_term_outlook=data.get('short_term_outlook', ''),
                    medium_term_outlook=data.get('medium_term_outlook', ''),
                    # 技术面
                    technical_analysis=data.get('technical_analysis', ''),
                    ma_analysis=data.get('ma_analysis', ''),
                    volume_analysis=data.get('volume_analysis', ''),
                    pattern_analysis=data.get('pattern_analysis', ''),
                    # 基本面
                    fundamental_analysis=data.get('fundamental_analysis', ''),
                    sector_position=data.get('sector_position', ''),
                    company_highlights=data.get('company_highlights', ''),
                    # 情绪面/消息面
                    news_summary=data.get('news_summary', ''),
                    market_sentiment=data.get('market_sentiment', ''),
                    hot_topics=data.get('hot_topics', ''),
                    # 综合
                    analysis_summary=data.get('analysis_summary', 'Analysis completed' if report_language == "en" else '分析完成'),
                    key_points=data.get('key_points', ''),
                    risk_warning=data.get('risk_warning', ''),
                    buy_reason=data.get('buy_reason', ''),
                    # 元数据
                    search_performed=data.get('search_performed', False),
                    data_sources=data.get('data_sources', 'Technical data' if report_language == "en" else '技术面数据'),
                    success=True,
                )
            else:
                # 没有找到 JSON，标记为失败
                logger.warning(f"无法从响应中提取 JSON，标记为解析失败")
                return self._parse_text_response(response_text, code, name)
                
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}，标记为解析失败")
            return self._parse_text_response(response_text, code, name)
    
    def _fix_json_string(self, json_str: str) -> str:
        """修复常见的 JSON 格式问题"""
        import re
        
        # 移除注释
        json_str = re.sub(r'//.*?\n', '\n', json_str)
        json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
        
        # 修复尾随逗号
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        # 确保布尔值是小写
        json_str = json_str.replace('True', 'true').replace('False', 'false')
        
        # fix by json-repair
        json_str = repair_json(json_str)
        
        return json_str
    
    def _parse_text_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """从纯文本响应中尽可能提取分析信息"""
        report_language = normalize_report_language(
            getattr(self._get_runtime_config(), "report_language", "zh")
        )
        # 尝试识别关键词来判断情绪
        sentiment_score = 50
        trend = 'Sideways' if report_language == "en" else '震荡'
        advice = 'Hold' if report_language == "en" else '持有'
        
        text_lower = response_text.lower()
        
        # 简单的情绪识别
        positive_keywords = ['看多', '买入', '上涨', '突破', '强势', '利好', '加仓', 'bullish', 'buy']
        negative_keywords = ['看空', '卖出', '下跌', '跌破', '弱势', '利空', '减仓', 'bearish', 'sell']
        
        positive_count = sum(1 for kw in positive_keywords if kw in text_lower)
        negative_count = sum(1 for kw in negative_keywords if kw in text_lower)
        
        if positive_count > negative_count + 1:
            sentiment_score = 65
            trend = 'Bullish' if report_language == "en" else '看多'
            advice = 'Buy' if report_language == "en" else '买入'
            decision_type = 'buy'
        elif negative_count > positive_count + 1:
            sentiment_score = 35
            trend = 'Bearish' if report_language == "en" else '看空'
            advice = 'Sell' if report_language == "en" else '卖出'
            decision_type = 'sell'
        else:
            decision_type = 'hold'
        
        # 截取前500字符作为摘要
        summary = response_text[:500] if response_text else ('No analysis result' if report_language == "en" else '无分析结果')
        
        return AnalysisResult(
            code=code,
            name=name,
            sentiment_score=sentiment_score,
            trend_prediction=trend,
            operation_advice=advice,
            decision_type=decision_type,
            confidence_level='Low' if report_language == "en" else '低',
            analysis_summary=summary,
            key_points='JSON parsing failed; treat this as best-effort output.' if report_language == "en" else 'JSON解析失败，仅供参考',
            risk_warning='The result may be inaccurate. Cross-check with other information.' if report_language == "en" else '分析结果可能不准确，建议结合其他信息判断',
            raw_response=response_text,
            success=False,
            error_message='LLM response is not valid JSON; analysis result will not be persisted',
            report_language=report_language,
        )
    
    def batch_analyze(
        self, 
        contexts: List[Dict[str, Any]],
        delay_between: float = 2.0
    ) -> List[AnalysisResult]:
        """
        批量分析多只股票
        
        注意：为避免 API 速率限制，每次分析之间会有延迟
        
        Args:
            contexts: 上下文数据列表
            delay_between: 每次分析之间的延迟（秒）
            
        Returns:
            AnalysisResult 列表
        """
        results = []
        
        for i, context in enumerate(contexts):
            if i > 0:
                logger.debug(f"等待 {delay_between} 秒后继续...")
                time.sleep(delay_between)
            
            result = self.analyze(context)
            results.append(result)
        
        return results


# 便捷函数
def get_analyzer() -> GeminiAnalyzer:
    """获取 LLM 分析器实例"""
    return GeminiAnalyzer()


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    # 模拟上下文数据
    test_context = {
        'code': '600519',
        'date': '2026-01-09',
        'today': {
            'open': 1800.0,
            'high': 1850.0,
            'low': 1780.0,
            'close': 1820.0,
            'volume': 10000000,
            'amount': 18200000000,
            'pct_chg': 1.5,
            'ma5': 1810.0,
            'ma10': 1800.0,
            'ma20': 1790.0,
            'volume_ratio': 1.2,
        },
        'ma_status': '多头排列 📈',
        'volume_change_ratio': 1.3,
        'price_change_ratio': 1.5,
    }
    
    analyzer = GeminiAnalyzer()
    
    if analyzer.is_available():
        print("=== AI 分析测试 ===")
        result = analyzer.analyze(test_context)
        print(f"分析结果: {result.to_dict()}")
    else:
        print("Gemini API 未配置，跳过测试")
