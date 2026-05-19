# -*- coding: utf-8 -*-
"""
Peer/type valuation comparison for stock reports.

The first version deliberately avoids broad mega-cap comparisons. A stock can
have more than one reasonable peer universe, so the default maps here prefer a
same-industry or same-business-model set and leave env overrides available for
portfolio-specific views.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_US_PEER_MAP: Dict[str, List[str]] = {
    # Consumer electronics / PC hardware / devices. MSFT/NVDA are intentionally
    # left out of AAPL's default set because their margin and asset structures
    # are too different for a simple PE/PB median.
    "AAPL": ["DELL", "HPQ", "SONY", "LOGI", "GRMN"],
    "DELL": ["HPQ", "AAPL", "LOGI", "SONY", "GRMN"],
    "HPQ": ["DELL", "AAPL", "LOGI", "SONY", "GRMN"],
    "MSFT": ["ORCL", "ADBE", "CRM", "NOW", "INTU"],
    "GOOGL": ["META", "SNAP", "PINS", "BIDU", "YELP"],
    "GOOG": ["META", "SNAP", "PINS", "BIDU", "YELP"],
    "META": ["GOOGL", "SNAP", "PINS", "BIDU", "YELP"],
    "AMZN": ["WMT", "COST", "BABA", "JD", "MELI"],
    "NVDA": ["AMD", "AVGO", "QCOM", "MRVL", "INTC"],
    "TSLA": ["GM", "F", "TM", "RIVN", "NIO"],
    "AMD": ["NVDA", "INTC", "QCOM", "AVGO", "MU"],
}

DEFAULT_CN_PEER_MAP: Dict[str, List[str]] = {
    "600519": ["000858", "000568", "000596", "600809", "603369"],
    "000858": ["600519", "000568", "000596", "600809", "603369"],
    "300750": ["002812", "300014", "002074", "688567", "002594"],
    "002594": ["300750", "601633", "600104", "000625", "601238"],
    "600036": ["000001", "601166", "601398", "601288", "601328"],
    "000001": ["600036", "601166", "601398", "601288", "601328"],
    "601318": ["601601", "601628", "601336", "601319", "601688"],
}

DEFAULT_HK_PEER_MAP: Dict[str, List[str]] = {
    "HK00700": ["HK09999", "HK03690", "HK09888", "HK09988", "HK01024"],
    "HK09988": ["HK09618", "HK03690", "HK09888", "HK09999", "HK01024"],
    "HK03690": ["HK09988", "HK09618", "HK01024", "HK09888", "HK00700"],
    "HK01810": ["HK02015", "HK09866", "HK09868", "HK01211", "HK00285"],
    "HK01211": ["HK02015", "HK09866", "HK09868", "HK01810", "HK02238"],
}

DEFAULT_MARKET_PEER_MAPS: Dict[str, Dict[str, List[str]]] = {
    "us": DEFAULT_US_PEER_MAP,
    "cn": DEFAULT_CN_PEER_MAP,
    "hk": DEFAULT_HK_PEER_MAP,
}

DEFAULT_MARKET_BASIS: Dict[str, str] = {
    "us": "同类型/同行业可比公司（默认使用业务模式更接近的手工 peer set，可用 US_PEER_VALUATION_MAP 覆盖）",
    "cn": "A股同行业/同类型可比公司（默认常用行业 peer set，可用 CN_PEER_VALUATION_MAP 覆盖）",
    "hk": "港股同行业/同类型可比公司（默认常用行业 peer set，可用 HK_PEER_VALUATION_MAP 覆盖）",
}


def parse_peer_map(raw: Optional[str]) -> Dict[str, List[str]]:
    """Parse a JSON peer map from env/config."""
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("US_PEER_VALUATION_MAP is not valid JSON; using defaults")
        return {}
    if not isinstance(payload, dict):
        return {}

    parsed: Dict[str, List[str]] = {}
    for ticker, peers in payload.items():
        if not isinstance(peers, list):
            continue
        ticker_norm = str(ticker).strip().upper()
        peer_list = [
            str(peer).strip().upper()
            for peer in peers
            if str(peer).strip()
        ]
        if ticker_norm and peer_list:
            parsed[ticker_norm] = peer_list
    return parsed


class PeerValuationClient:
    """Build a compact relative valuation block for one ticker."""

    def __init__(
        self,
        quote_fetcher: Optional[Callable[[str], Any]] = None,
        peer_map: Optional[Dict[str, List[str]]] = None,
        max_peers: int = 5,
        market: str = "us",
        comparison_basis: Optional[str] = None,
    ):
        self.max_peers = max(1, int(max_peers or 5))
        self.market = (market or "us").strip().lower()
        defaults = DEFAULT_MARKET_PEER_MAPS.get(self.market, {})
        self.peer_map = {**defaults, **(peer_map or {})}
        self.comparison_basis = comparison_basis or DEFAULT_MARKET_BASIS.get(
            self.market,
            "同类型/同行业可比公司",
        )
        if quote_fetcher is None:
            from .longbridge_fetcher import LongbridgeFetcher

            lb_fetcher = LongbridgeFetcher()
            quote_fetcher = lb_fetcher.get_realtime_quote
        self.quote_fetcher = quote_fetcher

    @classmethod
    def from_env(cls) -> "PeerValuationClient":
        return cls(
            peer_map=parse_peer_map(os.getenv("US_PEER_VALUATION_MAP")),
            max_peers=int(os.getenv("PEER_VALUATION_MAX_PEERS", "5") or "5"),
        )

    def get_peer_valuation_context(self, ticker: str) -> Dict[str, Any]:
        start_ts = time.time()
        target = (ticker or "").strip().upper()
        if not target:
            return self._empty("empty ticker", start_ts)

        target = self._normalize_symbol(target)
        peers = self.peer_map.get(target, [])[: self.max_peers]
        if not peers:
            return self._empty(f"no peer map configured for {target}", start_ts, status="not_supported")

        symbols = [target, *[self._normalize_symbol(peer) for peer in peers if self._normalize_symbol(peer) != target]]
        rows: List[Dict[str, Any]] = []
        errors: List[str] = []

        for symbol in symbols:
            try:
                quote = self.quote_fetcher(symbol)
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                continue
            row = self._quote_to_row(symbol, quote, is_target=(symbol == target))
            if row:
                rows.append(row)

        if not rows or not any(row.get("is_target") for row in rows):
            return self._empty("target valuation quote unavailable", start_ts, errors=errors, status="failed")

        summary = self._build_summary(rows)
        status = "ok" if len(rows) >= 3 else "partial"
        data_quality = self._build_data_quality(rows, peers)
        return {
            "provider": "realtime_quote",
            "status": status,
            "market": self.market,
            "target": target,
            "peers": peers,
            "rows": rows,
            "summary": summary,
            "data_quality": data_quality,
            "errors": errors,
            "comparison_basis": self.comparison_basis,
            "source": "Realtime quote PE/PB/market-cap fields",
            "source_chain": [
                {
                    "provider": f"{self.market}_peer_valuation",
                    "result": status,
                    "duration_ms": int((time.time() - start_ts) * 1000),
                }
            ],
        }

    def _empty(
        self,
        reason: str,
        start_ts: float,
        *,
        errors: Optional[List[str]] = None,
        status: str = "failed",
    ) -> Dict[str, Any]:
        return {
            "provider": "realtime_quote",
            "status": status,
            "market": self.market,
            "rows": [],
            "summary": {},
            "data_quality": self._build_data_quality([], []),
            "errors": [reason, *(errors or [])],
            "comparison_basis": self.comparison_basis,
            "source": "Realtime quote PE/PB/market-cap fields",
            "source_chain": [
                {
                    "provider": f"{self.market}_peer_valuation",
                    "result": status,
                    "duration_ms": int((time.time() - start_ts) * 1000),
                }
            ],
        }

    @staticmethod
    def _quote_to_row(symbol: str, quote: Any, *, is_target: bool) -> Optional[Dict[str, Any]]:
        if quote is None:
            return None
        price = PeerValuationClient._float_or_none(getattr(quote, "price", None))
        pe = PeerValuationClient._float_or_none(getattr(quote, "pe_ratio", None))
        pb = PeerValuationClient._float_or_none(getattr(quote, "pb_ratio", None))
        market_cap = PeerValuationClient._float_or_none(getattr(quote, "total_mv", None))
        if price is None and pe is None and pb is None and market_cap is None:
            return None
        return {
            "symbol": symbol,
            "name": getattr(quote, "name", "") or symbol,
            "is_target": is_target,
            "price": price,
            "pe_ratio": pe,
            "pb_ratio": pb,
            "market_cap": market_cap,
            "market_cap_text": PeerValuationClient._format_compact(market_cap),
        }

    @staticmethod
    def _build_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        target = next((row for row in rows if row.get("is_target")), {})
        peers = [row for row in rows if not row.get("is_target")]
        summary: Dict[str, Any] = {
            "peer_count": len(peers),
            "target_symbol": target.get("symbol"),
        }
        for metric in ("pe_ratio", "pb_ratio"):
            target_value = PeerValuationClient._float_or_none(target.get(metric))
            peer_values = [
                PeerValuationClient._float_or_none(row.get(metric))
                for row in peers
            ]
            peer_values = [value for value in peer_values if value is not None and value > 0]
            median_value = statistics.median(peer_values) if peer_values else None
            summary[f"target_{metric}"] = target_value
            summary[f"peer_median_{metric}"] = round(median_value, 4) if median_value is not None else None
            if target_value is not None and median_value and median_value > 0:
                summary[f"{metric}_vs_peer_median_pct"] = round((target_value / median_value - 1.0) * 100.0, 2)
            else:
                summary[f"{metric}_vs_peer_median_pct"] = None
        return summary

    @staticmethod
    def _build_data_quality(rows: List[Dict[str, Any]], peers: List[str]) -> Dict[str, Any]:
        rows_with_pe = sum(1 for row in rows if PeerValuationClient._float_or_none(row.get("pe_ratio")) is not None)
        rows_with_pb = sum(1 for row in rows if PeerValuationClient._float_or_none(row.get("pb_ratio")) is not None)
        return {
            "basis": "quote-derived PE/PB/market-cap fields",
            "requested_peer_count": len(peers),
            "rows_returned": len(rows),
            "rows_with_pe": rows_with_pe,
            "rows_with_pb": rows_with_pb,
            "limitations": [
                "provider PE/PB fields may be absent, delayed, or calculated differently across markets",
                "default peer sets are compact hand-curated comparables and should be overridden for sector-specific work",
            ],
        }

    @staticmethod
    def _float_or_none(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        value = (symbol or "").strip().upper()
        if value.endswith(".HK"):
            base = value[:-3]
            if base.isdigit():
                return f"HK{base.zfill(5)}"
        if value.startswith("HK"):
            digits = value[2:]
            if digits.isdigit():
                return f"HK{digits.zfill(5)}"
        if "." in value and value.rsplit(".", 1)[1] in {"SH", "SZ", "SS", "BJ"}:
            return value.rsplit(".", 1)[0]
        return value

    @staticmethod
    def _format_compact(value: Optional[float]) -> str:
        if value is None:
            return "N/A"
        sign = "-" if value < 0 else ""
        abs_value = abs(value)
        if abs_value >= 1_000_000_000_000:
            return f"{sign}{abs_value / 1_000_000_000_000:.2f}T"
        if abs_value >= 1_000_000_000:
            return f"{sign}{abs_value / 1_000_000_000:.2f}B"
        if abs_value >= 1_000_000:
            return f"{sign}{abs_value / 1_000_000:.2f}M"
        return f"{sign}{abs_value:,.0f}"
