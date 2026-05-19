from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from data_provider.sec_edgar import SecEdgarClient


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class SecEdgarClientTest(unittest.TestCase):
    def setUp(self):
        SecEdgarClient._ticker_cache = None

    @patch.object(SecEdgarClient, "_build_yahoo_dividend_metrics", return_value=None)
    @patch("data_provider.sec_edgar.requests.get")
    def test_company_context_maps_filings_and_companyfacts(self, mock_get, _mock_dividends):
        accession = "0000320193-26-000001"
        accession_no_dash = accession.replace("-", "")

        def fake_get(url, **kwargs):
            if url.endswith("/company_tickers.json"):
                return _FakeResponse({
                    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
                })
            if "submissions/CIK0000320193.json" in url:
                return _FakeResponse({
                    "name": "Apple Inc.",
                    "filings": {
                        "recent": {
                            "form": ["10-Q", "10-Q", "10-Q", "10-Q", "10-K", "4"],
                            "accessionNumber": [
                                accession,
                                "0000320193-26-000000",
                                "0000320193-25-000004",
                                "0000320193-25-000003",
                                "0000320193-25-000002",
                                "0000320193-26-000099",
                            ],
                            "filingDate": ["2026-05-01", "2026-02-01", "2025-08-01", "2025-05-01", "2025-11-01", "2026-04-15"],
                            "reportDate": ["2026-03-28", "2025-12-27", "2025-06-28", "2025-03-29", "2025-09-27", "2026-04-12"],
                            "primaryDocument": [
                                "aapl-20260328.htm",
                                "aapl-20251227.htm",
                                "aapl-20250628.htm",
                                "aapl-20250329.htm",
                                "aapl-20250927.htm",
                                "xslF345X05/wk-form4_1713139200.xml",
                            ],
                            "primaryDocDescription": ["10-Q", "10-Q", "10-Q", "10-Q", "10-K", "FORM 4"],
                        }
                    },
                })
            if "companyfacts/CIK0000320193.json" in url:
                fact = {
                    "accn": accession,
                    "form": "10-Q",
                    "filed": "2026-05-01",
                    "start": "2026-01-01",
                    "end": "2026-03-28",
                    "frame": "CY2026Q1",
                }
                return _FakeResponse({
                    "facts": {
                        "us-gaap": {
                            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                                "units": {"USD": [{**fact, "val": 90_000_000_000}]}
                            },
                            "NetIncomeLoss": {
                                "units": {"USD": [{**fact, "val": 22_000_000_000}]}
                            },
                            "NetCashProvidedByUsedInOperatingActivities": {
                                "units": {"USD": [{**fact, "val": 25_000_000_000}]}
                            },
                            "PaymentsToAcquirePropertyPlantAndEquipment": {
                                "units": {"USD": [{**fact, "val": 3_000_000_000}]}
                            },
                            "StockholdersEquity": {
                                "units": {"USD": [{**fact, "val": 75_000_000_000}]}
                            },
                            "Assets": {
                                "units": {"USD": [{**fact, "val": 360_000_000_000}]}
                            },
                            "Liabilities": {
                                "units": {"USD": [{**fact, "val": 285_000_000_000}]}
                            },
                            "EarningsPerShareDiluted": {
                                "units": {"USD/shares": [{**fact, "val": 1.42}]}
                            },
                        }
                    }
                })
            if url.endswith(f"/{accession_no_dash}/index.json"):
                return _FakeResponse({
                    "directory": {
                        "item": [{"name": "aapl-20260328.htm"}, {"name": "financial-report.pdf"}]
                    }
                })
            return _FakeResponse({"directory": {"item": []}})

        mock_get.side_effect = fake_get

        client = SecEdgarClient(user_agent="unit-test example@example.com")
        context = client.get_company_context("AAPL")

        self.assertEqual(context["cik"], "0000320193")
        self.assertEqual(context["latest_filing"]["form"], "10-Q")
        self.assertEqual(len(context["latest_quarterly_filings"]), 4)
        self.assertEqual(len(context["filing_references"]), 5)
        self.assertEqual(context["filing_references"][-1]["form"], "10-K")
        self.assertEqual(len(context["recent_insider_filings"]), 1)
        self.assertEqual(context["recent_insider_filings"][0]["form"], "4")
        self.assertIn("/Archives/edgar/data/320193/", context["latest_filing"]["sec_url"])
        self.assertEqual(
            context["latest_filing"]["pdf_url"],
            f"https://www.sec.gov/Archives/edgar/data/320193/{accession_no_dash}/financial-report.pdf",
        )
        report = context["financial_report"]
        self.assertEqual(report["report_date"], "2026-03-28")
        self.assertEqual(report["revenue"], "$90.00B")
        self.assertEqual(report["net_profit_parent"], "$22.00B")
        self.assertEqual(report["operating_cash_flow"], "$25.00B")
        self.assertEqual(report["roe"], "29.33%")
        self.assertEqual(report["capital_expenditure"], "$3.00B")
        self.assertEqual(report["free_cash_flow"], "$22.00B")
        self.assertEqual(report["liabilities"], "$285.00B")
        self.assertEqual(report["net_margin_pct"], 24.44)
        self.assertEqual(report["operating_cash_flow_to_net_income_pct"], 113.64)
        self.assertEqual(report["free_cash_flow_to_net_income_pct"], 100.0)
        self.assertEqual(report["debt_to_assets_pct"], 79.17)

    def test_financial_report_picks_latest_revenue_across_equivalent_tags(self):
        client = SecEdgarClient(user_agent="unit-test example@example.com")
        latest_filing = {
            "form": "10-Q",
            "filing_date": "2026-05-06",
            "report_date": "2026-03-28",
            "accession_number": "0001628280-26-030777",
        }
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                {
                                    "accn": "0001628280-25-020000",
                                    "form": "10-Q",
                                    "filed": "2025-05-07",
                                    "start": "2024-12-29",
                                    "end": "2025-03-29",
                                    "frame": "CY2025Q1",
                                    "val": 425_200_000,
                                }
                            ]
                        }
                    },
                    "RevenueFromContractWithCustomerIncludingAssessedTax": {
                        "units": {
                            "USD": [
                                {
                                    "accn": "0001628280-26-030777",
                                    "form": "10-Q",
                                    "filed": "2026-05-06",
                                    "start": "2025-12-28",
                                    "end": "2026-03-28",
                                    "frame": "CY2026Q1",
                                    "val": 808_400_000,
                                }
                            ]
                        }
                    },
                }
            }
        }

        report = client._build_financial_report(facts, latest_filing)

        self.assertEqual(report["revenue"], "$808.40M")
        self.assertEqual(report["revenue_period"], "2025-12-28~2026-03-28 (filed 2026-05-06)")
        self.assertEqual(report["quarterly_trend"][0]["period"], "CY2026Q1")
        self.assertEqual(report["quarterly_trend"][0]["revenue"], "$808.40M")

    def test_financial_report_extracts_extended_sec_metrics_and_bank_revenue(self):
        client = SecEdgarClient(user_agent="unit-test example@example.com")
        latest_filing = {
            "form": "10-Q",
            "filing_date": "2026-05-06",
            "report_date": "2026-03-31",
            "accession_number": "0000019617-26-000321",
        }

        def fact(tag_value, unit="USD"):
            return {
                "accn": "0000019617-26-000321",
                "form": "10-Q",
                "filed": "2026-05-06",
                "start": "2026-01-01",
                "end": "2026-03-31",
                "frame": "CY2026Q1",
                "val": tag_value,
            }, unit

        def units(value, unit="USD"):
            entry, unit_name = fact(value, unit)
            return {"units": {unit_name: [entry]}}

        facts = {
            "facts": {
                "us-gaap": {
                    "RevenuesNetOfInterestExpense": units(42_000_000_000),
                    "NetIncomeLoss": units(14_000_000_000),
                    "OperatingIncomeLoss": units(18_000_000_000),
                    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": units(17_000_000_000),
                    "InterestIncomeOperating": units(55_000_000_000),
                    "InterestExpenseOperating": units(23_000_000_000),
                    "InterestIncomeExpenseOperatingNet": units(32_000_000_000),
                    "ProvisionForCreditLosses": units(2_000_000_000),
                    "Assets": units(4_200_000_000_000),
                    "Liabilities": units(3_800_000_000_000),
                    "AssetsCurrent": units(900_000_000_000),
                    "LiabilitiesCurrent": units(750_000_000_000),
                    "ShortTermBorrowings": units(100_000_000_000),
                    "LongTermDebt": units(320_000_000_000),
                    "WeightedAverageNumberOfDilutedSharesOutstanding": units(2_800_000_000, "shares"),
                }
            }
        }

        report = client._build_financial_report(facts, latest_filing)

        self.assertEqual(report["revenue"], "$42.00B")
        self.assertEqual(report["operating_income"], "$18.00B")
        self.assertEqual(report["pretax_income"], "$17.00B")
        self.assertEqual(report["interest_income"], "$55.00B")
        self.assertEqual(report["net_interest_income"], "$32.00B")
        self.assertEqual(report["provision_for_credit_losses"], "$2.00B")
        self.assertEqual(report["current_assets"], "$900.00B")
        self.assertEqual(report["current_ratio"], 1.2)
        self.assertEqual(report["total_debt"], "$420.00B")
        self.assertEqual(report["total_debt_to_assets_pct"], 10.0)
        self.assertEqual(report["metric_coverage"]["financial_services"], 4)
        metric_keys = {metric["key"] for metric in report["additional_metrics"]}
        self.assertIn("diluted_shares", metric_keys)
        self.assertIn("net_interest_income", metric_keys)

    def test_financial_trends_derive_quarter_cash_flow_from_ytd_facts(self):
        client = SecEdgarClient(user_agent="unit-test example@example.com")
        latest_filing = {
            "form": "10-Q",
            "filing_date": "2026-05-06",
            "report_date": "2026-03-28",
            "accession_number": "0001628280-26-030777",
        }

        def quarterly_fact(start, end, frame, val, filed="2026-05-06", accn="0001628280-26-030777"):
            return {
                "accn": accn,
                "form": "10-Q",
                "filed": filed,
                "start": start,
                "end": end,
                "frame": frame,
                "val": val,
            }

        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerIncludingAssessedTax": {
                        "units": {
                            "USD": [
                                quarterly_fact("2025-06-29", "2025-09-27", "CY2025Q3", 533_800_000),
                                quarterly_fact("2025-09-28", "2025-12-27", "CY2025Q4", 665_500_000),
                                quarterly_fact("2025-12-28", "2026-03-28", "CY2026Q1", 808_400_000),
                            ]
                        }
                    },
                    "NetIncomeLoss": {
                        "units": {
                            "USD": [
                                quarterly_fact("2025-06-29", "2025-09-27", "CY2025Q3", 4_200_000),
                                quarterly_fact("2025-09-28", "2025-12-27", "CY2025Q4", 78_200_000),
                                quarterly_fact("2025-12-28", "2026-03-28", "CY2026Q1", 144_200_000),
                            ]
                        }
                    },
                    "NetCashProvidedByUsedInOperatingActivities": {
                        "units": {
                            "USD": [
                                quarterly_fact("2025-06-29", "2025-09-27", "CY2025Q3", 57_900_000),
                                quarterly_fact("2025-06-29", "2025-12-27", None, 184_600_000, filed="2026-02-04"),
                                quarterly_fact("2025-06-29", "2026-03-28", None, 388_400_000),
                            ]
                        }
                    },
                    "PaymentsToAcquirePropertyPlantAndEquipment": {
                        "units": {
                            "USD": [
                                quarterly_fact("2025-06-29", "2025-09-27", "CY2025Q3", 76_200_000),
                                quarterly_fact("2025-06-29", "2025-12-27", None, 159_800_000, filed="2026-02-04"),
                                quarterly_fact("2025-06-29", "2026-03-28", None, 284_500_000),
                            ]
                        }
                    },
                }
            }
        }

        report = client._build_financial_report(facts, latest_filing)
        rows = {row["period"]: row for row in report["quarterly_trend"]}

        self.assertAlmostEqual(rows["CY2025Q4"]["operating_cash_flow_value"], 126_700_000)
        self.assertAlmostEqual(rows["CY2025Q4"]["capital_expenditure_value"], 83_600_000)
        self.assertEqual(rows["CY2025Q4"]["free_cash_flow"], "$43.10M")
        self.assertAlmostEqual(rows["CY2026Q1"]["operating_cash_flow_value"], 203_800_000)
        self.assertAlmostEqual(rows["CY2026Q1"]["capital_expenditure_value"], 124_700_000)
        self.assertEqual(rows["CY2026Q1"]["free_cash_flow"], "$79.10M")

    def test_ytd_quarter_derivation_reuses_existing_sec_frame_row_with_same_end_date(self):
        client = SecEdgarClient(user_agent="unit-test example@example.com")
        latest_filing = {
            "form": "10-Q",
            "filing_date": "2025-11-19",
            "report_date": "2025-10-26",
            "accession_number": "0001045810-25-000001",
        }

        def fact(start, end, frame, val, filed="2025-11-19", form="10-Q"):
            return {
                "accn": "0001045810-25-000001",
                "form": form,
                "filed": filed,
                "start": start,
                "end": end,
                "frame": frame,
                "val": val,
            }

        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [
                            fact("2025-07-28", "2025-10-26", "CY2025Q3I", 57_006_000_000),
                            fact("2025-01-27", "2025-07-27", None, 90_805_000_000, filed="2025-08-27"),
                            fact("2025-01-27", "2025-10-26", None, 147_811_000_000),
                        ]}
                    },
                    "NetIncomeLoss": {
                        "units": {"USD": [
                            fact("2025-07-28", "2025-10-26", "CY2025Q3I", 31_910_000_000),
                            fact("2025-01-27", "2025-07-27", None, 50_000_000_000, filed="2025-08-27"),
                            fact("2025-01-27", "2025-10-26", None, 81_910_000_000),
                        ]}
                    },
                    "NetCashProvidedByUsedInOperatingActivities": {
                        "units": {"USD": [
                            fact("2025-01-27", "2025-07-27", None, 40_000_000_000, filed="2025-08-27"),
                            fact("2025-01-27", "2025-10-26", None, 70_000_000_000),
                        ]}
                    },
                }
            }
        }

        report = client._build_financial_report(facts, latest_filing)
        periods = [row["period"] for row in report["quarterly_trend"]]
        rows_by_period = {row["period"]: row for row in report["quarterly_trend"]}

        self.assertIn("CY2025Q3", periods)
        self.assertNotIn("CY2025Q4", periods)
        self.assertEqual(periods.count("CY2025Q3"), 1)
        self.assertAlmostEqual(rows_by_period["CY2025Q3"]["operating_cash_flow_value"], 30_000_000_000)
        end_dates = [row.get("end_date") for row in report["quarterly_trend"]]
        self.assertEqual(end_dates.count("2025-10-26"), 1)

    def test_yahoo_dividend_metrics_calculates_ttm_events(self):
        client = SecEdgarClient(user_agent="unit-test example@example.com")
        payload = {
            "chart": {
                "result": [
                    {
                        "events": {
                            "dividends": {
                                "1": {"date": int(datetime(2025, 6, 1, tzinfo=timezone.utc).timestamp()), "amount": 0.24},
                                "2": {"date": int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()), "amount": 0.25},
                                "3": {"date": int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp()), "amount": 0.26},
                            }
                        }
                    }
                ]
            }
        }

        with patch("data_provider.sec_edgar.requests.get", return_value=_FakeResponse(payload)):
            with patch("data_provider.sec_edgar.datetime") as mock_datetime:
                mock_datetime.now.return_value = datetime(2026, 5, 3, tzinfo=timezone.utc)
                mock_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
                mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
                metrics = client._build_yahoo_dividend_metrics("AAPL")

        self.assertEqual(metrics["ttm_event_count"], 3)
        self.assertAlmostEqual(metrics["ttm_cash_dividend_per_share"], 0.75)
        self.assertEqual(metrics["events"][0]["event_date"], "2026-04-01")


if __name__ == "__main__":
    unittest.main()
