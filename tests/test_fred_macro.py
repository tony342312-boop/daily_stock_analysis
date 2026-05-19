import unittest

from data_provider.fred_macro import FredMacroClient, FredSeriesSpec


class FredMacroClientTest(unittest.TestCase):
    def test_build_indicator_keeps_latest_value(self):
        client = FredMacroClient(api_key="test-key", timeout=1)
        indicator = client._build_indicator(
            FredSeriesSpec("DGS10", "10Y Treasury Yield", "%", "rate backdrop"),
            [
                {"date": "2026-05-01", "value": "4.25"},
                {"date": "2026-04-30", "value": "."},
            ],
        )

        self.assertEqual(indicator["series_id"], "DGS10")
        self.assertEqual(indicator["value"], 4.25)
        self.assertEqual(indicator["unit"], "%")

    def test_build_indicator_computes_yoy_when_requested(self):
        client = FredMacroClient(api_key="test-key", timeout=1)
        values = [112, 111, 110, 109, 108, 107, 106, 105, 104, 103, 102, 100]
        observations = [{"date": f"2026-{index:02d}-01", "value": str(value)} for index, value in enumerate(values, 1)]

        indicator = client._build_indicator(
            FredSeriesSpec("CPIAUCSL", "CPI YoY", "%", "inflation", compute_yoy=True),
            observations,
        )

        self.assertEqual(indicator["series_id"], "CPIAUCSL")
        self.assertAlmostEqual(indicator["value"], 12.0)
        self.assertIn("calculated", indicator["note"])

    def test_parse_float_skips_fred_missing_marker(self):
        self.assertIsNone(FredMacroClient._parse_float("."))
        self.assertEqual(FredMacroClient._parse_float("3.5"), 3.5)


if __name__ == "__main__":
    unittest.main()
