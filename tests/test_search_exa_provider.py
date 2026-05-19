# -*- coding: utf-8 -*-
"""Unit tests for Exa search provider."""

import unittest
from unittest.mock import MagicMock, patch

from src.search_service import ExaSearchProvider, SearchService


class TestExaSearchProvider(unittest.TestCase):
    """Validate Exa API request shape and response parsing."""

    @staticmethod
    def _response(*, status_code: int = 200, json_payload=None, text: str = "") -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.json.return_value = {} if json_payload is None else json_payload
        return resp

    @patch("src.search_service._post_with_retry")
    def test_exa_search_uses_highlights_and_maps_fields(self, mock_post):
        mock_post.return_value = self._response(
            json_payload={
                "results": [
                    {
                        "title": "Apple earnings recap",
                        "url": "https://example.com/aapl",
                        "publishedDate": "2026-05-01T12:00:00.000Z",
                        "highlights": ["Revenue beat expectations", "Buyback increased"],
                    }
                ]
            }
        )

        provider = ExaSearchProvider(["exa-test-key"])
        resp = provider.search("AAPL latest earnings", max_results=3, days=7)

        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "Exa")
        self.assertEqual(len(resp.results), 1)
        self.assertEqual(resp.results[0].source, "example.com")
        self.assertIn("Revenue beat", resp.results[0].snippet)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "auto")
        self.assertEqual(payload["category"], "news")
        self.assertTrue(payload["contents"]["highlights"])
        self.assertIn("startPublishedDate", payload)

    def test_search_service_adds_exa_provider_when_configured(self):
        service = SearchService(
            tavily_keys=[],
            exa_keys=["exa-test-key"],
            searxng_public_instances_enabled=False,
        )

        self.assertTrue(service.is_available)
        self.assertTrue(any(provider.name == "Exa" for provider in service._providers))


if __name__ == "__main__":
    unittest.main()
