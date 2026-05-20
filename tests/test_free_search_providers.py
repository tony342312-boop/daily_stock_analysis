# -*- coding: utf-8 -*-
"""Tests for free no-key search providers."""

import unittest
from unittest.mock import MagicMock, patch

from src.search_service import (
    BingNewsRSSSearchProvider,
    DuckDuckGoSearchProvider,
    GoogleNewsRSSSearchProvider,
    MultiSearchEngineProvider,
    SearchService,
)


class TestFreeSearchProviders(unittest.TestCase):
    """Validate no-key search provider parsing and wiring."""

    @staticmethod
    def _response(*, text: str = "", content: bytes = b"", status_code: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.content = content or text.encode("utf-8")
        return resp

    @patch("src.search_service._get_with_retry")
    def test_duckduckgo_html_results_are_parsed(self, mock_get):
        mock_get.return_value = self._response(
            text="""
            <html><body>
              <div class="result">
                <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Faapl">
                  Apple news
                </a>
                <a class="result__snippet">Apple summary</a>
              </div>
            </body></html>
            """
        )

        resp = DuckDuckGoSearchProvider().search("AAPL stock", max_results=3)

        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "DuckDuckGo")
        self.assertEqual(len(resp.results), 1)
        self.assertEqual(resp.results[0].url, "https://example.com/aapl")
        self.assertEqual(resp.results[0].source, "example.com")

    @patch("src.search_service._get_with_retry")
    def test_google_news_rss_results_are_parsed(self, mock_get):
        mock_get.return_value = self._response(
            text="""
            <rss><channel>
              <item>
                <title>AAPL earnings</title>
                <link>https://news.example.com/aapl</link>
                <description>Quarterly update</description>
                <pubDate>Fri, 01 May 2026 12:00:00 GMT</pubDate>
                <source>Example News</source>
              </item>
            </channel></rss>
            """
        )

        resp = GoogleNewsRSSSearchProvider().search("AAPL stock", max_results=3, days=7)

        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "GoogleNewsRSS")
        self.assertEqual(len(resp.results), 1)
        self.assertEqual(resp.results[0].published_date, "2026-05-01")
        self.assertEqual(resp.results[0].source, "Example News")

    @patch("src.search_service._get_with_retry")
    def test_multi_search_merges_and_deduplicates(self, mock_get):
        html = """
        <div class="result">
          <a class="result__a" href="https://example.com/a">A</a>
          <a class="result__snippet">A summary</a>
        </div>
        """
        rss = """
        <rss><channel>
          <item><title>A duplicate</title><link>https://example.com/a</link><description>dup</description></item>
          <item><title>B</title><link>https://example.com/b</link><description>B summary</description></item>
        </channel></rss>
        """
        mock_get.side_effect = [
            self._response(text=html),
            self._response(text=rss),
        ]

        provider = MultiSearchEngineProvider([
            DuckDuckGoSearchProvider(),
            BingNewsRSSSearchProvider(),
        ])
        resp = provider.search("AAPL stock", max_results=5)

        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "MultiSearch")
        self.assertEqual([r.url for r in resp.results], ["https://example.com/a", "https://example.com/b"])

    def test_search_service_adds_multi_search_provider_when_enabled(self):
        service = SearchService(
            searxng_public_instances_enabled=False,
            ddg_search_enabled=True,
            google_news_rss_enabled=True,
            bing_news_rss_enabled=True,
            multi_search_engine_enabled=True,
        )

        self.assertTrue(service.is_available)
        self.assertTrue(any(provider.name == "MultiSearch" for provider in service._providers))


if __name__ == "__main__":
    unittest.main()
