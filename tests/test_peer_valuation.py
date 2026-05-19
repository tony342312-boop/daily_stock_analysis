import unittest
from types import SimpleNamespace

from data_provider.peer_valuation import PeerValuationClient, parse_peer_map


class PeerValuationClientTest(unittest.TestCase):
    def test_parse_peer_map_accepts_json(self):
        parsed = parse_peer_map('{"aapl": ["msft", "googl", "meta"]}')
        self.assertEqual(parsed["AAPL"], ["MSFT", "GOOGL", "META"])

    def test_peer_context_builds_summary(self):
        quotes = {
            "AAPL": SimpleNamespace(
                name="Apple",
                price=280.0,
                pe_ratio=35.0,
                pb_ratio=40.0,
                total_mv=4_000_000_000_000,
            ),
            "MSFT": SimpleNamespace(
                name="Microsoft",
                price=520.0,
                pe_ratio=30.0,
                pb_ratio=12.0,
                total_mv=3_800_000_000_000,
            ),
            "GOOGL": SimpleNamespace(
                name="Alphabet",
                price=330.0,
                pe_ratio=25.0,
                pb_ratio=8.0,
                total_mv=3_900_000_000_000,
            ),
        }
        client = PeerValuationClient(
            quote_fetcher=lambda symbol: quotes.get(symbol),
            peer_map={"AAPL": ["MSFT", "GOOGL"]},
            max_peers=2,
        )

        context = client.get_peer_valuation_context("AAPL")

        self.assertEqual(context["status"], "ok")
        self.assertEqual(len(context["rows"]), 3)
        self.assertEqual(context["summary"]["peer_median_pe_ratio"], 27.5)
        self.assertEqual(context["summary"]["peer_median_pb_ratio"], 10.0)
        self.assertEqual(context["summary"]["pe_ratio_vs_peer_median_pct"], 27.27)
        self.assertEqual(context["rows"][0]["market_cap_text"], "4.00T")
        self.assertEqual(context["market"], "us")
        self.assertIn("comparison_basis", context)
        self.assertEqual(context["data_quality"]["basis"], "quote-derived PE/PB/market-cap fields")
        self.assertEqual(context["data_quality"]["rows_returned"], 3)

    def test_hk_symbol_normalization_for_peer_map(self):
        quotes = {
            "HK00700": SimpleNamespace(name="腾讯控股", price=400, pe_ratio=20, pb_ratio=4, total_mv=3_800_000_000_000),
            "HK09999": SimpleNamespace(name="网易-S", price=180, pe_ratio=16, pb_ratio=3, total_mv=600_000_000_000),
        }
        client = PeerValuationClient(
            quote_fetcher=lambda symbol: quotes.get(symbol),
            peer_map={"HK00700": ["09999.HK"]},
            max_peers=1,
            market="hk",
        )
        context = client.get_peer_valuation_context("00700.HK")
        self.assertEqual(context["target"], "HK00700")
        self.assertEqual(context["rows"][1]["symbol"], "HK09999")


if __name__ == "__main__":
    unittest.main()
