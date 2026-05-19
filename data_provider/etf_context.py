# -*- coding: utf-8 -*-
"""ETF/fund context helpers.

ETF analysis should not pretend that funds have operating-company statements.
This module builds a compact, fail-open fund context from quote data and, for
US-listed ETFs, optional yfinance fund metadata when it is available.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


ETF_DATA_NEEDS = [
    "holdings/top constituents",
    "sector and country exposure",
    "expense ratio and fund operations",
    "NAV/premium-discount when available",
    "AUM, volume, and bid-ask liquidity",
    "tracking index and issuer profile",
]


class EtfContextClient:
    """Build fund-aware context without introducing a hard external dependency."""

    def __init__(self, timeout: float = 4.0) -> None:
        self.timeout = max(1.0, float(timeout or 4.0))

    def get_context(self, symbol: str, market: str, quote: Any = None) -> Dict[str, Any]:
        started = time.time()
        symbol = (symbol or "").strip().upper()
        market = (market or "").strip().lower() or "unknown"
        quote_payload = self.quote_to_dict(quote)
        payload: Dict[str, Any] = {
            "provider": "etf_context",
            "status": "partial" if quote_payload else "not_supported",
            "asset_type": "etf",
            "market": market,
            "symbol": symbol,
            "name": quote_payload.get("name") or symbol,
            "quote": quote_payload,
            "fund_profile": {
                "asset_type": "etf",
                "symbol": symbol,
                "market": market,
                "name": quote_payload.get("name") or symbol,
                "statement_model": "fund",
                "company_financial_report_applicable": False,
                "data_needs": ETF_DATA_NEEDS,
            },
            "holdings": {},
            "exposure": {},
            "operations": {},
            "liquidity": self._build_liquidity(quote_payload),
            "errors": [],
            "source_chain": [
                {
                    "provider": quote_payload.get("source") or "realtime_quote",
                    "result": "partial" if quote_payload else "not_supported",
                    "duration_ms": 0,
                }
            ],
            "elapsed_ms": 0,
        }

        if market == "us" and symbol:
            self._merge_yfinance_fund_data(payload, symbol)

        if payload.get("holdings") or payload.get("exposure") or payload.get("operations"):
            payload["status"] = "ok" if quote_payload else "partial"
        payload["elapsed_ms"] = int((time.time() - started) * 1000)
        return payload

    @staticmethod
    def quote_to_dict(quote: Any) -> Dict[str, Any]:
        if quote is None:
            return {}
        if hasattr(quote, "to_dict"):
            try:
                payload = quote.to_dict()
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass
        source = getattr(getattr(quote, "source", None), "value", getattr(quote, "source", None))
        payload = {
            "code": getattr(quote, "code", None),
            "name": getattr(quote, "name", None),
            "source": source,
            "price": getattr(quote, "price", None),
            "change_pct": getattr(quote, "change_pct", None),
            "change_amount": getattr(quote, "change_amount", None),
            "volume": getattr(quote, "volume", None),
            "amount": getattr(quote, "amount", None),
            "volume_ratio": getattr(quote, "volume_ratio", None),
            "turnover_rate": getattr(quote, "turnover_rate", None),
            "amplitude": getattr(quote, "amplitude", None),
            "open_price": getattr(quote, "open_price", None),
            "high": getattr(quote, "high", None),
            "low": getattr(quote, "low", None),
            "pre_close": getattr(quote, "pre_close", None),
            "total_mv": getattr(quote, "total_mv", None),
            "circ_mv": getattr(quote, "circ_mv", None),
        }
        return {key: value for key, value in payload.items() if value is not None}

    @staticmethod
    def _build_liquidity(quote_payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: quote_payload[key]
            for key in (
                "price",
                "change_pct",
                "volume",
                "amount",
                "volume_ratio",
                "turnover_rate",
                "amplitude",
                "total_mv",
                "circ_mv",
            )
            if key in quote_payload
        }

    def _merge_yfinance_fund_data(self, payload: Dict[str, Any], symbol: str) -> None:
        started = time.time()
        chain_item = {
            "provider": "yfinance_funds_data",
            "result": "started",
            "duration_ms": 0,
        }
        try:
            import yfinance as yf

            ticker = yf.Ticker(symbol)
            funds_data = getattr(ticker, "funds_data", None)
            if funds_data is None:
                raise RuntimeError("funds_data unavailable")

            overview = self._safe_attr(funds_data, "fund_overview")
            operations = self._safe_attr(funds_data, "fund_operations")
            top_holdings = self._df_to_records(self._safe_attr(funds_data, "top_holdings"), limit=15)
            sector_weightings = self._df_to_records(self._safe_attr(funds_data, "sector_weightings"), limit=20)
            asset_classes = self._df_to_records(self._safe_attr(funds_data, "asset_classes"), limit=20)
            equity_holdings = self._safe_attr(funds_data, "equity_holdings")
            bond_holdings = self._safe_attr(funds_data, "bond_holdings")
            bond_ratings = self._df_to_records(self._safe_attr(funds_data, "bond_ratings"), limit=20)
            description = self._safe_attr(funds_data, "description")

            if isinstance(overview, dict):
                payload["fund_profile"]["overview"] = overview
            if description:
                payload["fund_profile"]["description"] = str(description)
            if isinstance(operations, dict):
                payload["operations"] = operations
            if top_holdings:
                payload["holdings"]["top_holdings"] = top_holdings
            if isinstance(equity_holdings, dict):
                payload["holdings"]["equity_holdings"] = equity_holdings
            if isinstance(bond_holdings, dict):
                payload["holdings"]["bond_holdings"] = bond_holdings
            if sector_weightings:
                payload["exposure"]["sector_weightings"] = sector_weightings
            if asset_classes:
                payload["exposure"]["asset_classes"] = asset_classes
            if bond_ratings:
                payload["exposure"]["bond_ratings"] = bond_ratings

            has_data = bool(payload["holdings"] or payload["exposure"] or payload["operations"])
            chain_item["result"] = "ok" if has_data else "not_supported"
            if not has_data:
                payload["errors"].append("yfinance funds_data returned no ETF detail")
        except Exception as exc:
            chain_item["result"] = "failed"
            payload["errors"].append(f"yfinance_funds_data: {exc}")
            logger.debug("ETF yfinance fund data failed for %s: %s", symbol, exc)
        finally:
            chain_item["duration_ms"] = int((time.time() - started) * 1000)
            payload["source_chain"].append(chain_item)

    @staticmethod
    def _safe_attr(obj: Any, attr: str) -> Any:
        try:
            return getattr(obj, attr)
        except Exception:
            return None

    @staticmethod
    def _df_to_records(value: Any, limit: int = 20) -> List[Dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, pd.Series):
            return [
                {"name": str(index), "value": item}
                for index, item in value.dropna().head(limit).items()
            ]
        if isinstance(value, pd.DataFrame):
            if value.empty:
                return []
            frame = value.head(limit).copy()
            frame = frame.reset_index()
            return frame.where(pd.notna(frame), None).to_dict(orient="records")
        if isinstance(value, dict):
            return [
                {"name": str(key), "value": item}
                for key, item in list(value.items())[:limit]
                if item is not None
            ]
        return []
