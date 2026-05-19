# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


_A_SHARE_FINANCIAL_SOURCE = "AkShare Sina 财务报表"


def _safe_number(value: Any) -> Optional[float]:
    parsed = _safe_float(value)
    if parsed is None:
        return None
    try:
        if pd.isna(parsed):
            return None
    except Exception:
        pass
    return float(parsed)


def _to_sina_stock_symbol(stock_code: str) -> str:
    code = _normalize_code(stock_code)
    if not code:
        return _safe_str(stock_code).lower()
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return code.lower()


def _format_cny_amount(value: Any) -> str:
    amount = _safe_number(value)
    if amount is None:
        return "N/A"
    sign = "-" if amount < 0 else ""
    abs_amount = abs(amount)
    if abs_amount >= 1e8:
        text = f"{abs_amount / 1e8:.2f}亿"
    elif abs_amount >= 1e4:
        text = f"{abs_amount / 1e4:.2f}万"
    else:
        text = f"{abs_amount:.2f}"
    return f"{sign}¥{text}"


def _format_ratio(value: Any) -> str:
    ratio = _safe_number(value)
    return "N/A" if ratio is None else f"{ratio:.2f}%"


def _format_eps(value: Any) -> str:
    eps = _safe_number(value)
    return "N/A" if eps is None else f"{eps:.2f}"


def _pick_exact(row: Optional[pd.Series], columns: List[str]) -> Optional[Any]:
    if row is None:
        return None
    for col in columns:
        if col in row.index:
            value = row.get(col)
            if value is not None and str(value).strip() not in ("", "-", "nan", "None"):
                return value
    return None


def _row_number(row: Optional[pd.Series], columns: List[str]) -> Optional[float]:
    return _safe_number(_pick_exact(row, columns))


def _prepare_sina_report_frame(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    date_col = next(
        (
            col
            for col in df.columns
            if str(col) in ("报告日", "报告日期", "报告期", "截止日期")
            or any(key in str(col) for key in ("报告日", "报告期", "截止日期"))
        ),
        None,
    )
    if date_col is None:
        return pd.DataFrame()
    work_df = df.copy()
    work_df["_report_dt"] = work_df[date_col].map(_safe_datetime)
    work_df = work_df.dropna(subset=["_report_dt"]).copy()
    if work_df.empty:
        return pd.DataFrame()
    work_df = work_df.sort_values("_report_dt", ascending=False).reset_index(drop=True)
    return work_df


def _date_key(value: datetime) -> str:
    return value.date().isoformat()


def _quarter_of(value: datetime) -> int:
    return (value.month - 1) // 3 + 1


def _quarter_period(value: datetime) -> str:
    return f"{value.year}Q{_quarter_of(value)}"


def _annual_period(value: datetime) -> str:
    return f"{value.year}"


def _calc_change_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100.0, 4)


def _safe_margin_pct(profit: Optional[float], revenue: Optional[float]) -> Optional[float]:
    if profit is None or revenue in (None, 0):
        return None
    return round(profit / revenue * 100.0, 4)


def _sum_optional(values: List[Optional[float]]) -> Optional[float]:
    available = [value for value in values if value is not None]
    if not available:
        return None
    return float(sum(available))


def _value_for_quarter(
    rows_by_yq: Dict[Tuple[int, int], pd.Series],
    year: int,
    quarter: int,
    columns: List[str],
) -> Tuple[Optional[float], bool, str]:
    current_row = rows_by_yq.get((year, quarter))
    current = _row_number(current_row, columns)
    if current is None:
        return None, False, ""
    if quarter <= 1:
        return current, False, "单季"
    previous = _row_number(rows_by_yq.get((year, quarter - 1)), columns)
    if previous is None:
        return current, False, "累计；缺前序季度，未拆单季"
    return current - previous, True, "由累计值相减得出单季值"


def _build_a_share_financial_report_from_sina_frames(
    profit_df: Optional[pd.DataFrame],
    balance_df: Optional[pd.DataFrame],
    cash_df: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """Build a SEC-like financial report payload from A-share Sina statements."""
    profit = _prepare_sina_report_frame(profit_df)
    balance = _prepare_sina_report_frame(balance_df)
    cash = _prepare_sina_report_frame(cash_df)
    if profit.empty and balance.empty and cash.empty:
        return {}

    profit_by_key = {_date_key(row["_report_dt"]): row for _, row in profit.iterrows()}
    cash_by_key = {_date_key(row["_report_dt"]): row for _, row in cash.iterrows()}
    balance_by_key = {_date_key(row["_report_dt"]): row for _, row in balance.iterrows()}
    profit_by_yq = {
        (row["_report_dt"].year, _quarter_of(row["_report_dt"])): row
        for _, row in profit.iterrows()
    }
    cash_by_yq = {
        (row["_report_dt"].year, _quarter_of(row["_report_dt"])): row
        for _, row in cash.iterrows()
    }

    revenue_cols = ["营业总收入", "营业收入"]
    net_profit_cols = ["归属于母公司所有者的净利润", "归属于母公司股东的净利润", "净利润"]
    eps_cols = ["稀释每股收益", "基本每股收益"]
    ocf_cols = ["经营活动产生的现金流量净额"]
    capex_cols = ["购建固定资产、无形资产和其他长期资产所支付的现金"]
    assets_cols = ["资产总计"]
    liabilities_cols = ["负债合计"]
    equity_cols = ["归属于母公司股东权益合计", "所有者权益(或股东权益)合计", "股东权益合计"]
    liquid_cols = ["货币资金", "交易性金融资产", "衍生金融资产"]
    debt_cols = ["短期借款", "一年内到期的非流动负债", "长期借款", "应付债券", "租赁负债"]

    dates = sorted(
        {row["_report_dt"] for _, row in profit.iterrows()}
        | {row["_report_dt"] for _, row in cash.iterrows()}
        | {row["_report_dt"] for _, row in balance.iterrows()},
        reverse=True,
    )
    if not dates:
        return {}

    def _balance_row_for(dt: datetime) -> Optional[pd.Series]:
        key = _date_key(dt)
        if key in balance_by_key:
            return balance_by_key[key]
        for _, row in balance.iterrows():
            if row["_report_dt"] <= dt:
                return row
        return balance.iloc[0] if not balance.empty else None

    quarterly_rows: List[Dict[str, Any]] = []
    for dt in dates:
        quarter = _quarter_of(dt)
        if quarter not in (1, 2, 3, 4):
            continue
        year = dt.year
        revenue, revenue_derived, revenue_note = _value_for_quarter(profit_by_yq, year, quarter, revenue_cols)
        net_profit, profit_derived, profit_note = _value_for_quarter(profit_by_yq, year, quarter, net_profit_cols)
        ocf, ocf_derived, ocf_note = _value_for_quarter(cash_by_yq, year, quarter, ocf_cols)
        capex, capex_derived, capex_note = _value_for_quarter(cash_by_yq, year, quarter, capex_cols)
        fcf = ocf - capex if ocf is not None and capex is not None else None
        profit_row = profit_by_key.get(_date_key(dt))
        filing_date = _normalize_report_date(_pick_exact(profit_row, ["公告日期", "更新日期"]))
        net_margin = _safe_margin_pct(net_profit, revenue)
        eps = _row_number(profit_row, eps_cols)
        row: Dict[str, Any] = {
            "period": _quarter_period(dt),
            "report_date": dt.date().isoformat(),
            "filing_date": filing_date,
            "revenue": _format_cny_amount(revenue),
            "revenue_value": revenue,
            "revenue_period": revenue_note,
            "net_profit_parent": _format_cny_amount(net_profit),
            "net_profit_parent_value": net_profit,
            "net_profit_parent_period": profit_note,
            "operating_cash_flow": _format_cny_amount(ocf),
            "operating_cash_flow_value": ocf,
            "operating_cash_flow_period": ocf_note,
            "capital_expenditure": _format_cny_amount(capex),
            "capital_expenditure_value": capex,
            "capital_expenditure_period": capex_note,
            "free_cash_flow": _format_cny_amount(fcf),
            "free_cash_flow_value": fcf,
            "net_margin_pct": net_margin,
            "eps_diluted": _format_eps(eps),
            "eps_diluted_value": eps,
            "derived": bool(revenue_derived or profit_derived or ocf_derived or capex_derived),
            "source": _A_SHARE_FINANCIAL_SOURCE,
        }
        if any(row.get(k) is not None for k in ("revenue_value", "net_profit_parent_value", "operating_cash_flow_value")):
            quarterly_rows.append(row)

    quarterly_rows.sort(key=lambda row: row.get("report_date") or "", reverse=True)
    by_quarter = {
        (int(str(row["period"])[:4]), int(str(row["period"]).split("Q")[-1])): row
        for row in quarterly_rows
        if isinstance(row.get("period"), str) and "Q" in str(row.get("period"))
    }
    for index, row in enumerate(quarterly_rows):
        older = quarterly_rows[index + 1] if index + 1 < len(quarterly_rows) else {}
        year = int(str(row["period"])[:4])
        quarter = int(str(row["period"]).split("Q")[-1])
        yoy = by_quarter.get((year - 1, quarter), {})
        row["revenue_value_change_pct"] = _calc_change_pct(row.get("revenue_value"), older.get("revenue_value"))
        row["revenue_value_yoy_pct"] = _calc_change_pct(row.get("revenue_value"), yoy.get("revenue_value"))
        row["net_profit_parent_value_change_pct"] = _calc_change_pct(
            row.get("net_profit_parent_value"),
            older.get("net_profit_parent_value"),
        )
        row["net_profit_parent_value_yoy_pct"] = _calc_change_pct(
            row.get("net_profit_parent_value"),
            yoy.get("net_profit_parent_value"),
        )

    annual_rows: List[Dict[str, Any]] = []
    for dt in dates:
        if dt.month != 12:
            continue
        key = _date_key(dt)
        profit_row = profit_by_key.get(key)
        cash_row = cash_by_key.get(key)
        revenue = _row_number(profit_row, revenue_cols)
        net_profit = _row_number(profit_row, net_profit_cols)
        ocf = _row_number(cash_row, ocf_cols)
        capex = _row_number(cash_row, capex_cols)
        fcf = ocf - capex if ocf is not None and capex is not None else None
        eps = _row_number(profit_row, eps_cols)
        row = {
            "period": _annual_period(dt),
            "report_date": dt.date().isoformat(),
            "filing_date": _normalize_report_date(_pick_exact(profit_row, ["公告日期", "更新日期"])),
            "revenue": _format_cny_amount(revenue),
            "revenue_value": revenue,
            "net_profit_parent": _format_cny_amount(net_profit),
            "net_profit_parent_value": net_profit,
            "operating_cash_flow": _format_cny_amount(ocf),
            "operating_cash_flow_value": ocf,
            "capital_expenditure": _format_cny_amount(capex),
            "capital_expenditure_value": capex,
            "free_cash_flow": _format_cny_amount(fcf),
            "free_cash_flow_value": fcf,
            "net_margin_pct": _safe_margin_pct(net_profit, revenue),
            "eps_diluted": _format_eps(eps),
            "eps_diluted_value": eps,
            "source": _A_SHARE_FINANCIAL_SOURCE,
        }
        if any(row.get(k) is not None for k in ("revenue_value", "net_profit_parent_value", "operating_cash_flow_value")):
            annual_rows.append(row)

    annual_rows.sort(key=lambda row: row.get("report_date") or "", reverse=True)
    for index, row in enumerate(annual_rows):
        older = annual_rows[index + 1] if index + 1 < len(annual_rows) else {}
        row["revenue_value_change_pct"] = _calc_change_pct(row.get("revenue_value"), older.get("revenue_value"))
        row["net_profit_parent_value_change_pct"] = _calc_change_pct(
            row.get("net_profit_parent_value"),
            older.get("net_profit_parent_value"),
        )

    latest = quarterly_rows[0] if quarterly_rows else {}
    latest_dt = _safe_datetime(latest.get("report_date")) if latest else dates[0]
    latest_balance = _balance_row_for(latest_dt or dates[0])
    assets = _row_number(latest_balance, assets_cols)
    liabilities = _row_number(latest_balance, liabilities_cols)
    equity = _row_number(latest_balance, equity_cols)
    liquid_assets = _sum_optional([_row_number(latest_balance, [col]) for col in liquid_cols])
    interest_bearing_debt = _sum_optional([_row_number(latest_balance, [col]) for col in debt_cols])
    net_cash = (
        liquid_assets - interest_bearing_debt
        if liquid_assets is not None and interest_bearing_debt is not None
        else None
    )
    debt_to_assets = _safe_margin_pct(liabilities, assets)
    equity_ratio = _safe_margin_pct(equity, assets)
    asset_to_equity = round(assets / equity, 4) if assets is not None and equity not in (None, 0) else None
    roe_value = None
    if annual_rows:
        annual_report_date = str(annual_rows[0].get("report_date") or "")
        annual_balance = balance_by_key.get(annual_report_date)
        if annual_balance is None:
            annual_balance = latest_balance
        annual_equity = _row_number(annual_balance, equity_cols)
        annual_profit = annual_rows[0].get("net_profit_parent_value")
        if annual_profit is not None and annual_equity not in (None, 0):
            roe_value = round(float(annual_profit) / float(annual_equity) * 100.0, 4)

    report: Dict[str, Any] = {
        "form": "A股财报",
        "source": _A_SHARE_FINANCIAL_SOURCE,
        "report_date": latest.get("report_date") or dates[0].date().isoformat(),
        "filing_date": latest.get("filing_date"),
        "revenue": latest.get("revenue"),
        "revenue_value": latest.get("revenue_value"),
        "revenue_period": latest.get("revenue_period") or "单季/累计口径见趋势表",
        "net_profit_parent": latest.get("net_profit_parent"),
        "net_profit_parent_value": latest.get("net_profit_parent_value"),
        "net_profit_parent_period": latest.get("net_profit_parent_period") or "单季/累计口径见趋势表",
        "operating_cash_flow": latest.get("operating_cash_flow"),
        "operating_cash_flow_value": latest.get("operating_cash_flow_value"),
        "operating_cash_flow_period": latest.get("operating_cash_flow_period") or "单季/累计口径见趋势表",
        "capital_expenditure": latest.get("capital_expenditure"),
        "capital_expenditure_value": latest.get("capital_expenditure_value"),
        "free_cash_flow": latest.get("free_cash_flow"),
        "free_cash_flow_value": latest.get("free_cash_flow_value"),
        "net_margin_pct": latest.get("net_margin_pct"),
        "roe": _format_ratio(roe_value),
        "roe_value": roe_value,
        "roe_note": f"最近完整年度归母净利润 / 股东权益，{_A_SHARE_FINANCIAL_SOURCE} 推算" if roe_value is not None else "N/A",
        "eps_diluted": latest.get("eps_diluted"),
        "eps_diluted_value": latest.get("eps_diluted_value"),
        "assets": _format_cny_amount(assets),
        "assets_value": assets,
        "liabilities": _format_cny_amount(liabilities),
        "liabilities_value": liabilities,
        "shareholders_equity": _format_cny_amount(equity),
        "shareholders_equity_value": equity,
        "debt_to_assets_pct": _format_ratio(debt_to_assets),
        "equity_ratio_pct": _format_ratio(equity_ratio),
        "asset_to_equity": asset_to_equity,
        "liquid_assets": _format_cny_amount(liquid_assets),
        "liquid_assets_value": liquid_assets,
        "interest_bearing_debt": _format_cny_amount(interest_bearing_debt),
        "interest_bearing_debt_value": interest_bearing_debt,
        "net_cash": _format_cny_amount(net_cash),
        "net_cash_value": net_cash,
        "quarterly_trend": quarterly_rows[:6],
        "annual_trend": annual_rows[:5],
    }
    if report.get("operating_cash_flow_value") is not None and report.get("net_profit_parent_value") not in (None, 0):
        report["operating_cash_flow_to_net_income_pct"] = round(
            report["operating_cash_flow_value"] / report["net_profit_parent_value"] * 100.0,
            4,
        )
    if report.get("free_cash_flow_value") is not None and report.get("net_profit_parent_value") not in (None, 0):
        report["free_cash_flow_to_net_income_pct"] = round(
            report["free_cash_flow_value"] / report["net_profit_parent_value"] * 100.0,
            4,
        )
    return report


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _build_sina_financial_report(self, stock_code: str) -> Tuple[Dict[str, Any], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return {}, [f"import_akshare:{type(exc).__name__}"]

        symbol = _to_sina_stock_symbol(stock_code)
        frames: Dict[str, Optional[pd.DataFrame]] = {"利润表": None, "资产负债表": None, "现金流量表": None}
        for statement in list(frames.keys()):
            try:
                df = ak.stock_financial_report_sina(stock=symbol, symbol=statement)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    frames[statement] = df
            except Exception as exc:
                errors.append(f"stock_financial_report_sina:{statement}:{type(exc).__name__}")

        report = _build_a_share_financial_report_from_sina_frames(
            frames.get("利润表"),
            frames.get("资产负债表"),
            frames.get("现金流量表"),
        )
        return report, errors

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Prefer structured A-share statements first. Some legacy AkShare
        # endpoints below are useful but slow/fragile; returning the statement
        # payload early keeps the WebUI from losing all fundamental data on
        # timeout.
        sina_report, sina_errors = self._build_sina_financial_report(stock_code)
        result["errors"].extend(sina_errors)
        if sina_report:
            result["earnings"]["financial_report"] = sina_report
            latest_quarter = {}
            quarterly = sina_report.get("quarterly_trend")
            if isinstance(quarterly, list) and quarterly:
                latest_quarter = quarterly[0] if isinstance(quarterly[0], dict) else {}
            result["growth"] = {
                "revenue_yoy": latest_quarter.get("revenue_value_yoy_pct"),
                "net_profit_yoy": latest_quarter.get("net_profit_parent_value_yoy_pct"),
                "roe": sina_report.get("roe"),
                "gross_margin": None,
            }
            result["source_chain"].append("financial_report:stock_financial_report_sina")
            result["status"] = "partial"
            return result

        # Financial indicators
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            row = _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                result["growth"] = {
                    "revenue_yoy": revenue_yoy,
                    "net_profit_yoy": profit_yoy,
                    "roe": roe,
                    "gross_margin": gross_margin,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                if any(v is not None for v in financial_report_payload.values()):
                    result["earnings"]["financial_report"] = financial_report_payload
                result["source_chain"].append(f"growth:{fin_source}")

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates([
            ("stock_yjyg_em", {"symbol": stock_code}),
            ("stock_yjyg_em", {}),
            ("stock_yjbb_em", {"symbol": stock_code}),
            ("stock_yjbb_em", {}),
        ])
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"])
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = self._call_df_candidates([
            ("stock_yjkb_em", {"symbol": stock_code}),
            ("stock_yjkb_em", {}),
        ])
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"])
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders
        inst_df, inst_source, inst_errors = self._call_df_candidates([
            ("stock_institute_hold", {}),
            ("stock_institute_recommend", {}),
        ])
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                inst_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
                result["institution"]["institution_holding_change"] = inst_change
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = self._call_df_candidates([
            ("stock_gdfx_top_10_em", {"symbol": stock_code}),
            ("stock_gdfx_top_10_em", {}),
            ("stock_zh_a_gdhs_detail_em", {"symbol": stock_code}),
            ("stock_zh_a_gdhs_detail_em", {}),
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                holder_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "持股变化", "变动"]))
                result["institution"]["top10_holder_change"] = holder_change
                result["source_chain"].append(f"top10:{top10_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        stock_df, stock_source, stock_errors = self._call_df_candidates([
            ("stock_individual_fund_flow", {"stock": stock_code}),
            ("stock_individual_fund_flow", {"symbol": stock_code}),
            ("stock_individual_fund_flow", {}),
            ("stock_main_fund_flow", {"symbol": stock_code}),
            ("stock_main_fund_flow", {}),
        ])
        result["errors"].extend(stock_errors)
        if stock_df is not None:
            row = _extract_latest_row(stock_df, stock_code)
            if row is not None:
                net_inflow = _safe_float(_pick_by_keywords(row, ["主力净流入", "净流入", "净额"]))
                inflow_5d = _safe_float(_pick_by_keywords(row, ["5日", "五日"]))
                inflow_10d = _safe_float(_pick_by_keywords(row, ["10日", "十日"]))
                result["stock_flow"] = {
                    "main_net_inflow": net_inflow,
                    "inflow_5d": inflow_5d,
                    "inflow_10d": inflow_10d,
                }
                result["source_chain"].append(f"capital_stock:{stock_source}")

        sector_df, sector_source, sector_errors = self._call_df_candidates([
            ("stock_sector_fund_flow_rank", {}),
            ("stock_sector_fund_flow_summary", {}),
        ])
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
            flow_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))), None)
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.dropna(subset=[flow_col])
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                    "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
