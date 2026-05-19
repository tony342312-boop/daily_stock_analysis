# -*- coding: utf-8 -*-
"""
FRED macro indicator client for US-stock report context.

FRED is best used as a macro backdrop rather than a company data source. This
client fetches a small, fixed indicator set and returns a compact fail-open
payload that can be injected into LLM prompts and Markdown reports.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class FredMacroError(RuntimeError):
    """Raised when FRED data cannot be fetched or parsed."""


@dataclass(frozen=True)
class FredSeriesSpec:
    series_id: str
    label: str
    unit: str
    note: str
    compute_yoy: bool = False


class FredMacroClient:
    """Small FRED API client for US macro indicators."""

    OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

    SERIES: tuple[FredSeriesSpec, ...] = (
        FredSeriesSpec("DGS10", "10Y Treasury Yield", "%", "Long-rate discount-rate backdrop"),
        FredSeriesSpec("DGS2", "2Y Treasury Yield", "%", "Policy-rate expectation proxy"),
        FredSeriesSpec("T10Y2Y", "10Y-2Y Treasury Spread", "pp", "Yield-curve recession/risk signal"),
        FredSeriesSpec("FEDFUNDS", "Fed Funds Rate", "%", "Policy-rate level"),
        FredSeriesSpec("CPIAUCSL", "CPI YoY", "%", "Inflation trend", compute_yoy=True),
        FredSeriesSpec("UNRATE", "Unemployment Rate", "%", "Labor-market slack"),
    )

    def __init__(self, api_key: str, timeout: float = 6.0):
        self.api_key = (api_key or "").strip()
        self.timeout = max(1.0, float(timeout or 6.0))
        if not self.api_key:
            raise FredMacroError("FRED_API_KEY is not configured")

    def get_macro_context(self) -> Dict[str, Any]:
        indicators: Dict[str, Dict[str, Any]] = {}
        errors: List[str] = []

        with ThreadPoolExecutor(max_workers=min(6, len(self.SERIES))) as executor:
            future_to_spec = {
                executor.submit(self._fetch_indicator, spec): spec
                for spec in self.SERIES
            }
            try:
                for future in as_completed(future_to_spec, timeout=self.timeout):
                    spec = future_to_spec[future]
                    try:
                        indicator = future.result()
                        if indicator:
                            indicators[spec.series_id] = indicator
                    except Exception as exc:
                        logger.debug("FRED %s fetch failed: %s", spec.series_id, exc)
                        errors.append(f"{spec.series_id}: {exc}")
            except TimeoutError:
                errors.append("FRED indicator fetch timeout")
                for future in future_to_spec:
                    future.cancel()

        status = "ok" if indicators else "failed"
        return {
            "provider": "FRED",
            "status": status,
            "indicators": indicators,
            "errors": errors,
            "source_chain": [
                {
                    "provider": "fred",
                    "result": status,
                    "duration_ms": 0,
                    "url": self.OBSERVATIONS_URL,
                }
            ],
        }

    def _fetch_indicator(self, spec: FredSeriesSpec) -> Optional[Dict[str, Any]]:
        observations = self._get_observations(spec.series_id, limit=24 if spec.compute_yoy else 8)
        return self._build_indicator(spec, observations)

    def _get_observations(self, series_id: str, limit: int) -> List[Dict[str, Any]]:
        response = requests.get(
            self.OBSERVATIONS_URL,
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise FredMacroError(f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FredMacroError("invalid JSON response") from exc
        observations = payload.get("observations")
        if not isinstance(observations, list):
            raise FredMacroError("unexpected response shape")
        return observations

    def _build_indicator(
        self,
        spec: FredSeriesSpec,
        observations: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        valid = [item for item in observations if self._parse_float(item.get("value")) is not None]
        if not valid:
            return None

        latest = valid[0]
        latest_value = self._parse_float(latest.get("value"))
        if latest_value is None:
            return None

        value = latest_value
        unit = spec.unit
        note = spec.note
        if spec.compute_yoy:
            prior = valid[11] if len(valid) >= 12 else None
            prior_value = self._parse_float(prior.get("value")) if prior else None
            if prior_value is not None and prior_value != 0:
                value = (latest_value / prior_value - 1.0) * 100.0
                unit = "%"
                note = f"{spec.note}; calculated from latest index vs about 12 observations earlier"
            else:
                note = f"{spec.note}; YoY calculation unavailable"

        return {
            "series_id": spec.series_id,
            "label": spec.label,
            "value": round(value, 4),
            "unit": unit,
            "date": latest.get("date"),
            "note": note,
        }

    @staticmethod
    def _parse_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text == ".":
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
