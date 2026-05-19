# -*- coding: utf-8 -*-
"""
Tests for fundamental adapter helpers.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _build_a_share_financial_report_from_sina_frames,
    _build_dividend_payload,
    _extract_latest_row,
    _parse_dividend_plan_to_per_share,
)


class TestFundamentalAdapter(unittest.TestCase):
    def test_parse_dividend_plan_to_per_share_supports_cn_patterns(self) -> None:
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("10派3元(含税)"), 0.3, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每10股派发2.5元"), 0.25, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每股派0.8元"), 0.8, places=6)
        self.assertIsNone(_parse_dividend_plan_to_per_share("仅送股，不现金分红"))

    def test_extract_latest_row_returns_none_when_code_mismatch(self) -> None:
        df = pd.DataFrame(
            {
                "股票代码": ["600000", "000001"],
                "值": [1, 2],
            }
        )
        row = _extract_latest_row(df, "600519")
        self.assertIsNone(row)

    def test_extract_latest_row_fallback_when_no_code_column(self) -> None:
        df = pd.DataFrame({"值": [1, 2]})
        row = _extract_latest_row(df, "600519")
        self.assertIsNotNone(row)
        self.assertEqual(row["值"], 1)

    def test_dragon_tiger_no_match_with_code_column_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-01"],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["is_on_list"])
        self.assertEqual(result["recent_count"], 0)

    def test_dragon_tiger_match_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "日期": [today],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_on_list"])
        self.assertGreaterEqual(result["recent_count"], 1)

    def test_fundamental_bundle_includes_financial_report_and_dividend_payload(self) -> None:
        adapter = AkshareFundamentalAdapter()
        now = datetime.now()
        within_ttm = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        future_day = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        old_day = (now - timedelta(days=500)).strftime("%Y-%m-%d")
        fin_df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "报告期": [within_ttm],
                "营业总收入": [1000.0],
                "归母净利润": [300.0],
                "经营活动产生的现金流量净额": [500.0],
                "净资产收益率": [18.2],
                "营业收入同比": [12.0],
                "净利润同比": [9.5],
            }
        )
        forecast_df = pd.DataFrame({"股票代码": ["600519"], "预告": ["预增"]})
        quick_df = pd.DataFrame({"股票代码": ["600519"], "快报": ["快报摘要"]})
        dividend_df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519", "600519", "600519"],
                "除息日": [within_ttm, within_ttm, future_day, old_day],
                "分配方案": ["10派3元(含税)", "10派3元(含税)", "10派5元", "10派1元"],
            }
        )

        with patch.object(adapter, "_build_sina_financial_report", return_value=({}, [])):
            with patch.object(
                adapter,
                "_call_df_candidates",
                side_effect=[
                    (fin_df, "stock_financial_abstract", []),
                    (forecast_df, "stock_yjyg_em", []),
                    (quick_df, "stock_yjkb_em", []),
                    (dividend_df, "stock_fhps_detail_em", []),
                    (None, None, []),
                    (None, None, []),
                ],
            ):
                result = adapter.get_fundamental_bundle("600519")

        financial_report = result["earnings"].get("financial_report", {})
        self.assertEqual(financial_report.get("report_date"), within_ttm)
        self.assertEqual(financial_report.get("revenue"), 1000.0)
        self.assertEqual(financial_report.get("net_profit_parent"), 300.0)
        self.assertEqual(financial_report.get("operating_cash_flow"), 500.0)
        self.assertEqual(financial_report.get("roe"), 18.2)

        dividend_payload = result["earnings"].get("dividend", {})
        events = dividend_payload.get("events", [])
        self.assertEqual(len(events), 2)  # duplicate + future day filtered
        self.assertEqual(dividend_payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(dividend_payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    def test_build_dividend_payload_returns_empty_when_code_not_matched(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "除息日": [now],
                "分配方案": ["10派3元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_skips_after_tax_plan(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "除息日": [now],
                "分配方案": ["10派3元(税后)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_ttm_window_boundary(self) -> None:
        now = datetime.now()
        day_365 = (now - timedelta(days=365)).strftime("%Y-%m-%d")
        day_366 = (now - timedelta(days=366)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519"],
                "除息日": [day_365, day_366],
                "分配方案": ["10派3元(含税)", "10派5元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    def test_build_a_share_financial_report_from_sina_frames(self) -> None:
        profit_df = pd.DataFrame(
            {
                "报告日": ["20260331", "20251231", "20250930", "20250630", "20250331", "20241231", "20240331"],
                "公告日期": ["20260430", "20260328", "20251030", "20250830", "20250430", "20250328", "20240430"],
                "营业总收入": [1000.0, 3600.0, 2700.0, 1800.0, 900.0, 3200.0, 800.0],
                "归属于母公司所有者的净利润": [200.0, 720.0, 540.0, 360.0, 180.0, 640.0, 160.0],
                "稀释每股收益": [1.0, 3.6, 2.7, 1.8, 0.9, 3.2, 0.8],
            }
        )
        cash_df = pd.DataFrame(
            {
                "报告日": ["20260331", "20251231", "20250930", "20250630", "20250331", "20241231", "20240331"],
                "经营活动产生的现金流量净额": [240.0, 800.0, 600.0, 400.0, 200.0, 700.0, 150.0],
                "购建固定资产、无形资产和其他长期资产所支付的现金": [50.0, 160.0, 120.0, 80.0, 40.0, 120.0, 30.0],
            }
        )
        balance_df = pd.DataFrame(
            {
                "报告日": ["20260331", "20251231"],
                "资产总计": [10000.0, 9600.0],
                "负债合计": [3000.0, 2800.0],
                "归属于母公司股东权益合计": [7000.0, 6800.0],
                "货币资金": [1200.0, 1000.0],
                "短期借款": [100.0, 120.0],
                "一年内到期的非流动负债": [50.0, 60.0],
                "长期借款": [300.0, 320.0],
            }
        )

        report = _build_a_share_financial_report_from_sina_frames(profit_df, balance_df, cash_df)

        self.assertEqual(report.get("form"), "A股财报")
        self.assertEqual(report.get("source"), "AkShare Sina 财务报表")
        self.assertEqual(report.get("report_date"), "2026-03-31")
        self.assertEqual(report.get("revenue_value"), 1000.0)
        self.assertEqual(report.get("net_profit_parent_value"), 200.0)
        self.assertEqual(report.get("free_cash_flow_value"), 190.0)
        self.assertEqual(report.get("debt_to_assets_pct"), "30.00%")
        self.assertEqual(report.get("equity_ratio_pct"), "70.00%")
        self.assertEqual(report.get("roe"), "10.59%")

        quarterly = report.get("quarterly_trend", [])
        self.assertEqual(quarterly[0]["period"], "2026Q1")
        self.assertAlmostEqual(quarterly[0]["revenue_value_yoy_pct"], 11.1111, places=4)
        self.assertEqual(quarterly[1]["period"], "2025Q4")
        self.assertTrue(quarterly[1]["derived"])
        self.assertEqual(quarterly[1]["revenue_value"], 900.0)
        self.assertEqual(quarterly[1]["free_cash_flow_value"], 160.0)

        annual = report.get("annual_trend", [])
        self.assertEqual(annual[0]["period"], "2025")
        self.assertAlmostEqual(annual[0]["revenue_value_change_pct"], 12.5, places=4)


if __name__ == "__main__":
    unittest.main()
