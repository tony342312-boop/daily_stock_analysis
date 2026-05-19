# -*- coding: utf-8 -*-
"""
===================================
Report Engine - Schema parsing and fallback tests
===================================

Tests for AnalysisReportSchema validation and analyzer fallback behavior.
"""

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Mock litellm before importing analyzer (optional runtime dep)
try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.schemas.report_schema import AnalysisReportSchema
from src.analyzer import GeminiAnalyzer, AnalysisResult


class TestAnalysisReportSchema(unittest.TestCase):
    """Schema parsing tests."""

    def test_valid_dashboard_parses(self) -> None:
        """Valid LLM-like JSON parses successfully."""
        data = {
            "stock_name": "贵州茅台",
            "sentiment_score": 75,
            "trend_prediction": "看多",
            "operation_advice": "持有",
            "decision_type": "hold",
            "confidence_level": "中",
            "dashboard": {
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110元"}},
            },
            "analysis_summary": "基本面稳健",
        }
        schema = AnalysisReportSchema.model_validate(data)
        self.assertEqual(schema.stock_name, "贵州茅台")
        self.assertEqual(schema.sentiment_score, 75)
        self.assertIsNotNone(schema.dashboard)

    def test_schema_allows_optional_fields_missing(self) -> None:
        """Schema accepts minimal valid structure."""
        data = {
            "stock_name": "测试",
            "sentiment_score": 50,
            "trend_prediction": "震荡",
            "operation_advice": "观望",
        }
        schema = AnalysisReportSchema.model_validate(data)
        self.assertIsNone(schema.dashboard)
        self.assertIsNone(schema.analysis_summary)

    def test_schema_allows_numeric_strings(self) -> None:
        """Schema accepts string values for numeric fields (LLM may return N/A)."""
        data = {
            "stock_name": "测试",
            "sentiment_score": 60,
            "trend_prediction": "看多",
            "operation_advice": "买入",
            "dashboard": {
                "data_perspective": {
                    "price_position": {
                        "current_price": "N/A",
                        "bias_ma5": "2.5",
                    }
                }
            },
        }
        schema = AnalysisReportSchema.model_validate(data)
        self.assertIsNotNone(schema.dashboard)
        pp = schema.dashboard and schema.dashboard.data_perspective and schema.dashboard.data_perspective.price_position
        self.assertIsNotNone(pp)
        if pp:
            self.assertEqual(pp.current_price, "N/A")
            self.assertEqual(pp.bias_ma5, "2.5")

    def test_schema_fails_on_invalid_sentiment_score(self) -> None:
        """Schema validation fails when sentiment_score out of range."""
        data = {
            "stock_name": "测试",
            "sentiment_score": 150,  # out of 0-100
            "trend_prediction": "看多",
            "operation_advice": "买入",
        }
        with self.assertRaises(Exception):
            AnalysisReportSchema.model_validate(data)


class TestAnalyzerSchemaFallback(unittest.TestCase):
    """Analyzer fallback when schema validation fails."""

    def test_parse_response_continues_when_schema_fails(self) -> None:
        """When schema validation fails, analyzer continues with raw dict."""
        analyzer = GeminiAnalyzer()
        response = json.dumps({
            "stock_name": "贵州茅台",
            "sentiment_score": 150,  # invalid for schema
            "trend_prediction": "看多",
            "operation_advice": "持有",
            "analysis_summary": "测试摘要",
        })
        result = analyzer._parse_response(response, "600519", "贵州茅台")
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.code, "600519")
        self.assertEqual(result.sentiment_score, 100)  # clamped into scorecard range
        self.assertEqual(result.dashboard["scorecard"]["overall_score"], 100)
        self.assertTrue(result.success)

    def test_parse_response_valid_json_succeeds(self) -> None:
        """Valid JSON produces correct AnalysisResult."""
        analyzer = GeminiAnalyzer()
        response = json.dumps({
            "stock_name": "贵州茅台",
            "sentiment_score": 72,
            "trend_prediction": "看多",
            "operation_advice": "持有",
            "decision_type": "hold",
            "confidence_level": "高",
            "analysis_summary": "技术面向好",
        })
        result = analyzer._parse_response(response, "600519", "股票600519")
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.name, "贵州茅台")
        self.assertEqual(result.sentiment_score, 72)
        self.assertEqual(result.analysis_summary, "技术面向好")
        self.assertEqual(result.dashboard["scorecard"]["overall_score"], 72)
        self.assertEqual(len(result.dashboard["scorecard"]["dimensions"]), 5)

    def test_parse_response_uses_weighted_scorecard_as_sentiment_score(self) -> None:
        """New multi-dimensional scorecard should drive the top-level score."""
        analyzer = GeminiAnalyzer()
        response = json.dumps({
            "stock_name": "测试公司",
            "sentiment_score": 90,
            "trend_prediction": "震荡",
            "operation_advice": "观望",
            "analysis_summary": "多维分歧，等待确认",
            "dashboard": {
                "core_conclusion": {"one_sentence": "基本面不错但估值和宏观压制，技术面需确认，暂时观望。"},
                "scorecard": {
                    "dimensions": {
                        "technical": {"score": 40, "weight": 25, "evidence": "跌破均线"},
                        "fundamental": {"score": 80, "weight": 25, "evidence": "利润和现金流改善"},
                        "valuation": {"score": 45, "weight": 20, "evidence": "估值偏贵"},
                        "news_sentiment": {"score": 60, "weight": 15, "evidence": "新闻中性偏正"},
                        "macro_risk": {"score": 45, "weight": 15, "evidence": "利率压力"},
                    }
                },
                "intelligence": {"risk_alerts": []},
            },
        })

        result = analyzer._parse_response(response, "TEST", "测试公司")

        self.assertEqual(result.sentiment_score, 55)
        self.assertEqual(result.dashboard["scorecard"]["overall_score"], 55)
        self.assertEqual(result.dashboard["scorecard"]["dimensions"]["fundamental"]["score"], 80)
        self.assertIn("技术面25%", result.dashboard["scorecard"]["score_method"])

    def test_parse_text_response_honors_injected_runtime_report_language(self) -> None:
        """Fallback text parsing should use the analyzer's injected config, not the global singleton."""
        with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
            analyzer = GeminiAnalyzer(config=SimpleNamespace(report_language="en"))

        result = analyzer._parse_text_response("bullish buy setup", "AAPL", "Apple")

        self.assertEqual(result.report_language, "en")
        self.assertEqual(result.trend_prediction, "Bullish")
        self.assertEqual(result.operation_advice, "Buy")
        self.assertEqual(result.confidence_level, "Low")
