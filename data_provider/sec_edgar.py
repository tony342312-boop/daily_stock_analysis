# -*- coding: utf-8 -*-
"""
SEC EDGAR data client for US stock fundamentals and filing references.

The SEC data APIs are public JSON endpoints. We keep this module deliberately
small and dependency-free so it can be used as a fail-open fundamental source
without changing the rest of the data-provider stack.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class SecEdgarError(RuntimeError):
    """Raised when SEC EDGAR data cannot be fetched or parsed."""


class SecEdgarClient:
    """Minimal SEC EDGAR client for ticker -> CIK, filings, and XBRL facts."""

    COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
    COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
    YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

    PERIODIC_FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A")
    QUARTERLY_FORMS = ("10-Q", "10-Q/A")
    ANNUAL_FORMS = ("10-K", "10-K/A")
    INSIDER_FORMS = ("3", "4", "5", "144", "3/A", "4/A", "5/A", "144/A")
    QUARTERLY_TREND_LIMIT = 5
    ANNUAL_TREND_LIMIT = 4
    REVENUE_TAGS = (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenuesNetOfInterestExpense",
        "OperatingRevenues",
        "TotalRevenuesAndOtherIncome",
    )
    CAPEX_TAGS = (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "CapitalExpendituresIncurredButNotYetPaid",
    )
    FINANCIAL_METRIC_SPECS = (
        ("cost_of_revenue", "Cost of revenue", "income_statement", ("CostOfRevenue", "CostOfGoodsAndServicesSold"), ("USD",)),
        ("gross_profit", "Gross profit", "income_statement", ("GrossProfit",), ("USD",)),
        (
            "research_and_development",
            "Research and development",
            "income_statement",
            ("ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
            ("USD",),
        ),
        (
            "selling_general_admin",
            "Selling, general and administrative",
            "income_statement",
            ("SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"),
            ("USD",),
        ),
        ("operating_income", "Operating income", "income_statement", ("OperatingIncomeLoss",), ("USD",)),
        (
            "interest_income",
            "Interest income",
            "financial_services",
            ("InterestIncomeOperating", "InterestAndDividendIncomeOperating", "InvestmentIncomeInterest"),
            ("USD",),
        ),
        (
            "interest_expense",
            "Interest expense",
            "financial_services",
            ("InterestExpenseOperating", "InterestExpenseNonOperating", "InterestExpense"),
            ("USD",),
        ),
        (
            "net_interest_income",
            "Net interest income",
            "financial_services",
            ("InterestIncomeExpenseOperatingNet", "InterestIncomeExpenseNonOperatingNet"),
            ("USD",),
        ),
        (
            "provision_for_credit_losses",
            "Provision for credit losses",
            "financial_services",
            ("ProvisionForCreditLosses", "ProvisionForLoanLeaseAndOtherLosses"),
            ("USD",),
        ),
        (
            "pretax_income",
            "Income before income taxes",
            "income_statement",
            (
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxes",
            ),
            ("USD",),
        ),
        ("income_tax_expense", "Income tax expense", "income_statement", ("IncomeTaxExpenseBenefit",), ("USD",)),
        ("eps_basic", "Basic EPS", "per_share", ("EarningsPerShareBasic",), ("USD/shares",)),
        (
            "diluted_shares",
            "Diluted weighted-average shares",
            "per_share",
            ("WeightedAverageNumberOfDilutedSharesOutstanding",),
            ("shares",),
        ),
        ("current_assets", "Current assets", "balance_sheet", ("AssetsCurrent",), ("USD",)),
        ("current_liabilities", "Current liabilities", "balance_sheet", ("LiabilitiesCurrent",), ("USD",)),
        (
            "accounts_receivable",
            "Accounts receivable",
            "balance_sheet",
            ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"),
            ("USD",),
        ),
        ("inventory", "Inventory", "balance_sheet", ("InventoryNet",), ("USD",)),
        ("accounts_payable", "Accounts payable", "balance_sheet", ("AccountsPayableCurrent", "AccountsPayableTradeCurrent"), ("USD",)),
        ("short_term_borrowings", "Short-term borrowings", "balance_sheet", ("ShortTermBorrowings",), ("USD",)),
        (
            "operating_lease_liability",
            "Operating lease liabilities",
            "balance_sheet",
            ("OperatingLeaseLiability", "OperatingLeaseLiabilityCurrent", "OperatingLeaseLiabilityNoncurrent"),
            ("USD",),
        ),
        (
            "finance_lease_liability",
            "Finance lease liabilities",
            "balance_sheet",
            ("FinanceLeaseLiability", "FinanceLeaseLiabilityCurrent", "FinanceLeaseLiabilityNoncurrent"),
            ("USD",),
        ),
        (
            "depreciation_amortization",
            "Depreciation and amortization",
            "cash_flow",
            ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization"),
            ("USD",),
        ),
        (
            "share_based_compensation",
            "Share-based compensation",
            "cash_flow",
            ("ShareBasedCompensation",),
            ("USD",),
        ),
        ("dividends_paid", "Dividends paid", "cash_flow", ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"), ("USD",)),
        (
            "stock_repurchases",
            "Stock repurchases",
            "cash_flow",
            ("PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"),
            ("USD",),
        ),
    )

    _ticker_cache: Optional[Dict[str, Dict[str, Any]]] = None

    def __init__(self, user_agent: Optional[str] = None, timeout: float = 8.0):
        self.user_agent = (
            user_agent
            or os.getenv("SEC_EDGAR_USER_AGENT")
            or "daily_stock_analysis/1.0 admin@stock.cn.mt"
        )
        self.timeout = max(1.0, float(timeout or 8.0))

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        }

    def get_company_context(self, ticker: str) -> Dict[str, Any]:
        """Return SEC-derived company fundamentals and filing links."""
        normalized_ticker = (ticker or "").strip().upper()
        if not normalized_ticker:
            raise SecEdgarError("empty ticker")

        company = self.get_company_for_ticker(normalized_ticker)
        cik = self._format_cik(company["cik"])
        submissions = self._get_json(self.SUBMISSIONS_URL.format(cik=cik))
        facts = self._get_json(self.COMPANY_FACTS_URL.format(cik=cik))

        recent_filings = self._extract_recent_filings(submissions, company)
        latest_filing = self._first_form(recent_filings, self.PERIODIC_FORMS)
        latest_quarterlies = self._first_n_forms(recent_filings, self.QUARTERLY_FORMS, limit=4)
        latest_annual = self._first_form(recent_filings, self.ANNUAL_FORMS)
        recent_insider_filings = self._first_n_insider_filings(recent_filings, limit=6)
        if not latest_filing:
            raise SecEdgarError(f"no recent 10-Q/10-K filing found for {normalized_ticker}")

        pdf_by_accession: Dict[str, Optional[str]] = {}
        for filing in self._dedupe_filings([latest_filing, latest_annual]):
            if filing:
                pdf_url = self._find_pdf_attachment(filing)
                filing["pdf_url"] = pdf_url
                if filing.get("accession_number"):
                    pdf_by_accession[str(filing["accession_number"])] = pdf_url

        references = self._dedupe_filings([*latest_quarterlies, latest_annual])
        if not references:
            references = [latest_filing]
        for filing in references:
            accession = str(filing.get("accession_number") or "")
            if accession in pdf_by_accession:
                filing["pdf_url"] = pdf_by_accession[accession]

        financial_report = self._build_financial_report(facts, latest_filing)

        return {
            "ticker": normalized_ticker,
            "cik": cik,
            "company_name": company.get("title") or submissions.get("name") or normalized_ticker,
            "latest_filing": latest_filing,
            "latest_quarterly_filings": latest_quarterlies,
            "latest_annual_filing": latest_annual,
            "filing_references": references,
            "recent_insider_filings": recent_insider_filings,
            "financial_report": financial_report,
            "dividend": (
                self._build_yahoo_dividend_metrics(normalized_ticker)
                or self._build_sec_dividend_metrics(facts, latest_filing)
            ),
            "source_chain": [
                {
                    "provider": "sec_edgar",
                    "result": "ok",
                    "duration_ms": 0,
                    "url": self.SUBMISSIONS_URL.format(cik=cik),
                },
                {
                    "provider": "sec_edgar_companyfacts",
                    "result": "ok",
                    "duration_ms": 0,
                    "url": self.COMPANY_FACTS_URL.format(cik=cik),
                },
            ],
        }

    def get_company_for_ticker(self, ticker: str) -> Dict[str, Any]:
        mapping = self._load_ticker_mapping()
        company = mapping.get(ticker.upper())
        if not company:
            raise SecEdgarError(f"ticker not found in SEC company_tickers.json: {ticker}")
        return company

    def _get_json(self, url: str) -> Dict[str, Any]:
        response = requests.get(url, headers=self.headers, timeout=self.timeout)
        if response.status_code != 200:
            raise SecEdgarError(f"SEC request failed HTTP {response.status_code}: {url}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SecEdgarError(f"SEC response is not valid JSON: {url}") from exc
        if not isinstance(data, dict):
            raise SecEdgarError(f"SEC response has unexpected shape: {url}")
        return data

    def _load_ticker_mapping(self) -> Dict[str, Dict[str, Any]]:
        if SecEdgarClient._ticker_cache is not None:
            return SecEdgarClient._ticker_cache

        raw = self._get_json(self.COMPANY_TICKERS_URL)
        mapping: Dict[str, Dict[str, Any]] = {}
        for item in raw.values():
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker", "")).strip().upper()
            cik = item.get("cik_str")
            if not ticker or cik is None:
                continue
            mapping[ticker] = {
                "ticker": ticker,
                "cik": cik,
                "title": item.get("title") or ticker,
            }
        SecEdgarClient._ticker_cache = mapping
        return mapping

    @classmethod
    def _format_cik(cls, cik: Any) -> str:
        return str(cik).strip().lstrip("0").zfill(10)

    def _extract_recent_filings(self, submissions: Dict[str, Any], company: Dict[str, Any]) -> List[Dict[str, Any]]:
        recent = (submissions.get("filings") or {}).get("recent") or {}
        if not isinstance(recent, dict):
            return []

        forms = list(recent.get("form") or [])
        accessions = list(recent.get("accessionNumber") or [])
        filing_dates = list(recent.get("filingDate") or [])
        report_dates = list(recent.get("reportDate") or [])
        primary_docs = list(recent.get("primaryDocument") or [])
        descriptions = list(recent.get("primaryDocDescription") or [])
        count = min(len(forms), len(accessions), len(filing_dates), len(primary_docs))
        cik = self._format_cik(company["cik"])

        filings: List[Dict[str, Any]] = []
        for idx in range(count):
            accession = accessions[idx]
            primary_doc = primary_docs[idx]
            if not accession or not primary_doc:
                continue
            form = str(forms[idx] or "").strip()
            accession_no_dash = str(accession).replace("-", "")
            cik_int = str(int(cik))
            doc_path = f"/Archives/edgar/data/{cik_int}/{accession_no_dash}/{primary_doc}"
            sec_url = f"https://www.sec.gov{doc_path}"
            detail_url = f"{self.ARCHIVES_BASE_URL}/{cik_int}/{accession_no_dash}/{accession}-index.html"
            filings.append(
                {
                    "form": form,
                    "filing_date": filing_dates[idx] if idx < len(filing_dates) else None,
                    "report_date": report_dates[idx] if idx < len(report_dates) else None,
                    "accession_number": accession,
                    "primary_document": primary_doc,
                    "primary_doc_description": descriptions[idx] if idx < len(descriptions) else None,
                    "document_url": sec_url,
                    "sec_url": sec_url,
                    "inline_xbrl_url": None,
                    "filing_detail_url": detail_url,
                }
            )
        return filings

    @staticmethod
    def _first_form(filings: List[Dict[str, Any]], forms: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
        for filing in filings:
            if filing.get("form") in forms:
                return dict(filing)
        return None

    @staticmethod
    def _first_n_forms(
        filings: List[Dict[str, Any]],
        forms: Tuple[str, ...],
        limit: int,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen_periods = set()
        for filing in filings:
            if filing.get("form") not in forms:
                continue
            period_key = (
                str(filing.get("form") or "").replace("/A", ""),
                filing.get("report_date") or filing.get("filing_date"),
            )
            if period_key in seen_periods:
                continue
            seen_periods.add(period_key)
            results.append(dict(filing))
            if len(results) >= limit:
                break
        return results

    @classmethod
    def _first_n_insider_filings(cls, filings: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen = set()
        for filing in filings:
            if filing.get("form") not in cls.INSIDER_FORMS:
                continue
            key = filing.get("accession_number") or filing.get("sec_url")
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(dict(filing))
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _dedupe_filings(filings: List[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        deduped: List[Dict[str, Any]] = []
        seen = set()
        for filing in filings:
            if not isinstance(filing, dict):
                continue
            key = filing.get("accession_number") or filing.get("sec_url") or (
                filing.get("form"),
                filing.get("report_date"),
            )
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(filing)
        return deduped

    def _find_pdf_attachment(self, filing: Dict[str, Any]) -> Optional[str]:
        """Return a PDF attachment URL when the filing package contains one."""
        try:
            sec_url = str(filing.get("sec_url") or "")
            if sec_url.lower().endswith(".pdf"):
                return sec_url

            accession = str(filing.get("accession_number") or "").replace("-", "")
            if not accession:
                return None
            parts = sec_url.split("/Archives/edgar/data/", 1)
            if len(parts) != 2:
                return None
            cik_int = parts[1].split("/", 1)[0]
            index_url = f"{self.ARCHIVES_BASE_URL}/{cik_int}/{accession}/index.json"
            index_data = self._get_json(index_url)
            items = ((index_data.get("directory") or {}).get("item") or [])
            if not isinstance(items, list):
                return None
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if name.lower().endswith(".pdf"):
                    return f"{self.ARCHIVES_BASE_URL}/{cik_int}/{accession}/{name}"
        except Exception as exc:
            logger.debug("SEC PDF attachment lookup failed: %s", exc)
        return None

    def _build_financial_report(self, facts: Dict[str, Any], latest_filing: Dict[str, Any]) -> Dict[str, Any]:
        revenue = self._pick_numeric_fact(
            facts,
            self.REVENUE_TAGS,
            ("USD",),
            latest_filing,
        )
        net_income = self._pick_numeric_fact(facts, ("NetIncomeLoss",), ("USD",), latest_filing)
        operating_cash_flow = self._pick_numeric_fact(
            facts,
            ("NetCashProvidedByUsedInOperatingActivities",),
            ("USD",),
            latest_filing,
        )
        capex = self._pick_numeric_fact(
            facts,
            self.CAPEX_TAGS,
            ("USD",),
            latest_filing,
        )
        equity = self._pick_numeric_fact(
            facts,
            ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
            ("USD",),
            latest_filing,
        )
        assets = self._pick_numeric_fact(facts, ("Assets",), ("USD",), latest_filing)
        liabilities = self._pick_numeric_fact(facts, ("Liabilities",), ("USD",), latest_filing)
        eps_diluted = self._pick_numeric_fact(facts, ("EarningsPerShareDiluted",), ("USD/shares",), latest_filing)
        cash = self._pick_numeric_fact(
            facts,
            (
                "CashAndCashEquivalentsAtCarryingValue",
                "Cash",
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            ),
            ("USD",),
            latest_filing,
        )
        marketable_securities_current = self._pick_numeric_fact(
            facts,
            ("MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
            ("USD",),
            latest_filing,
        )
        marketable_securities_noncurrent = self._pick_numeric_fact(
            facts,
            ("MarketableSecuritiesNoncurrent", "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent"),
            ("USD",),
            latest_filing,
        )
        commercial_paper = self._pick_numeric_fact(facts, ("CommercialPaper",), ("USD",), latest_filing)
        long_term_debt_current = self._pick_numeric_fact(facts, ("LongTermDebtCurrent",), ("USD",), latest_filing)
        long_term_debt_noncurrent = self._pick_numeric_fact(facts, ("LongTermDebtNoncurrent",), ("USD",), latest_filing)
        long_term_debt_total = self._pick_numeric_fact(facts, ("LongTermDebt",), ("USD",), latest_filing)
        additional_metric_facts = self._pick_additional_metric_facts(facts, latest_filing)

        revenue_value = self._fact_value(revenue)
        net_income_value = self._fact_value(net_income)
        operating_cash_flow_value = self._fact_value(operating_cash_flow)
        capex_value = self._fact_value(capex)
        assets_value = self._fact_value(assets)
        equity_value = self._fact_value(equity)
        liabilities_value = self._fact_value(liabilities)
        cash_value = self._fact_value(cash)
        marketable_current_value = self._fact_value(marketable_securities_current)
        marketable_noncurrent_value = self._fact_value(marketable_securities_noncurrent)
        commercial_paper_value = self._fact_value(commercial_paper)
        long_term_debt_current_value = self._fact_value(long_term_debt_current)
        long_term_debt_noncurrent_value = self._fact_value(long_term_debt_noncurrent)
        long_term_debt_total_value = self._fact_value(long_term_debt_total)
        additional_metric_values = {
            key: self._fact_value(fact)
            for key, fact in additional_metric_facts.items()
        }
        if liabilities_value is None and assets_value is not None and equity_value is not None:
            liabilities_value = assets_value - equity_value

        liquid_assets_value = self._sum_optional_values(
            cash_value,
            marketable_current_value,
            marketable_noncurrent_value,
        )
        if long_term_debt_current_value is not None or long_term_debt_noncurrent_value is not None:
            long_term_debt_value = self._sum_optional_values(
                long_term_debt_current_value,
                long_term_debt_noncurrent_value,
            )
        else:
            long_term_debt_value = long_term_debt_total_value
        short_term_borrowings_value = additional_metric_values.get("short_term_borrowings")
        operating_lease_liability_value = additional_metric_values.get("operating_lease_liability")
        finance_lease_liability_value = additional_metric_values.get("finance_lease_liability")
        interest_bearing_debt_value = self._sum_optional_values(
            commercial_paper_value,
            short_term_borrowings_value,
            long_term_debt_value,
            finance_lease_liability_value,
        )
        total_debt_value = self._sum_optional_values(
            commercial_paper_value,
            short_term_borrowings_value,
            long_term_debt_value,
            operating_lease_liability_value,
            finance_lease_liability_value,
        )
        net_cash_value = None
        if liquid_assets_value is not None and interest_bearing_debt_value is not None:
            net_cash_value = liquid_assets_value - interest_bearing_debt_value

        free_cash_flow_value = None
        if operating_cash_flow_value is not None and capex_value is not None:
            free_cash_flow_value = operating_cash_flow_value - abs(capex_value)

        net_margin_pct = self._safe_ratio_pct(net_income_value, revenue_value)
        gross_margin_pct = self._safe_ratio_pct(additional_metric_values.get("gross_profit"), revenue_value)
        operating_margin_pct = self._safe_ratio_pct(additional_metric_values.get("operating_income"), revenue_value)
        pretax_margin_pct = self._safe_ratio_pct(additional_metric_values.get("pretax_income"), revenue_value)
        ocf_to_net_income_pct = self._safe_ratio_pct(operating_cash_flow_value, net_income_value)
        fcf_to_net_income_pct = self._safe_ratio_pct(free_cash_flow_value, net_income_value)
        debt_to_assets_pct = self._safe_ratio_pct(liabilities_value, assets_value)
        interest_bearing_debt_to_assets_pct = self._safe_ratio_pct(interest_bearing_debt_value, assets_value)
        total_debt_to_assets_pct = self._safe_ratio_pct(total_debt_value, assets_value)
        liquid_assets_to_interest_bearing_debt_pct = self._safe_ratio_pct(
            liquid_assets_value,
            interest_bearing_debt_value,
        )
        equity_ratio_pct = self._safe_ratio_pct(equity_value, assets_value)
        asset_to_equity = self._safe_ratio(assets_value, equity_value)
        current_ratio = self._safe_ratio(
            additional_metric_values.get("current_assets"),
            additional_metric_values.get("current_liabilities"),
        )

        roe = None
        if net_income_value is not None and equity_value not in (None, 0):
            roe = round(net_income_value / equity_value * 100.0, 2)

        trends = self._build_financial_trends(facts)
        additional_metrics = self._build_additional_metrics_payload(
            additional_metric_facts,
            additional_metric_values,
        )
        report_date = latest_filing.get("report_date") or latest_filing.get("filing_date")
        report = {
            "report_date": report_date or "N/A",
            "form": latest_filing.get("form"),
            "filing_date": latest_filing.get("filing_date"),
            "period": self._format_period(latest_filing),
            "revenue": self._format_usd_fact(revenue),
            "revenue_period": self._fact_period_label(revenue),
            "net_profit_parent": self._format_usd_fact(net_income),
            "net_profit_parent_period": self._fact_period_label(net_income),
            "operating_cash_flow": self._format_usd_fact(operating_cash_flow),
            "operating_cash_flow_period": self._fact_period_label(operating_cash_flow),
            "capital_expenditure": self._format_usd_value(capex_value),
            "capital_expenditure_period": self._fact_period_label(capex),
            "free_cash_flow": self._format_usd_value(free_cash_flow_value),
            "roe": f"{roe:.2f}%" if roe is not None else "N/A",
            "roe_note": "net_income / shareholders_equity, SEC XBRL derived" if roe is not None else "N/A",
            "assets": self._format_usd_fact(assets),
            "liabilities": self._format_usd_value(liabilities_value),
            "shareholders_equity": self._format_usd_fact(equity),
            "eps_diluted": self._format_plain_fact(eps_diluted),
            "cash_and_equivalents": self._format_usd_value(cash_value),
            "marketable_securities_current": self._format_usd_value(marketable_current_value),
            "marketable_securities_noncurrent": self._format_usd_value(marketable_noncurrent_value),
            "liquid_assets": self._format_usd_value(liquid_assets_value),
            "commercial_paper": self._format_usd_value(commercial_paper_value),
            "short_term_borrowings": self._format_usd_value(short_term_borrowings_value),
            "long_term_debt": self._format_usd_value(long_term_debt_value),
            "interest_bearing_debt": self._format_usd_value(interest_bearing_debt_value),
            "total_debt": self._format_usd_value(total_debt_value),
            "net_cash": self._format_usd_value(net_cash_value),
            "revenue_value": revenue_value,
            "net_profit_parent_value": net_income_value,
            "operating_cash_flow_value": operating_cash_flow_value,
            "capital_expenditure_value": capex_value,
            "free_cash_flow_value": free_cash_flow_value,
            "assets_value": assets_value,
            "liabilities_value": liabilities_value,
            "shareholders_equity_value": equity_value,
            "cash_and_equivalents_value": cash_value,
            "marketable_securities_current_value": marketable_current_value,
            "marketable_securities_noncurrent_value": marketable_noncurrent_value,
            "liquid_assets_value": liquid_assets_value,
            "commercial_paper_value": commercial_paper_value,
            "short_term_borrowings_value": short_term_borrowings_value,
            "long_term_debt_value": long_term_debt_value,
            "interest_bearing_debt_value": interest_bearing_debt_value,
            "total_debt_value": total_debt_value,
            "net_cash_value": net_cash_value,
            "net_margin_pct": net_margin_pct,
            "gross_margin_pct": gross_margin_pct,
            "operating_margin_pct": operating_margin_pct,
            "pretax_margin_pct": pretax_margin_pct,
            "operating_cash_flow_to_net_income_pct": ocf_to_net_income_pct,
            "free_cash_flow_to_net_income_pct": fcf_to_net_income_pct,
            "debt_to_assets_pct": debt_to_assets_pct,
            "interest_bearing_debt_to_assets_pct": interest_bearing_debt_to_assets_pct,
            "total_debt_to_assets_pct": total_debt_to_assets_pct,
            "liquid_assets_to_interest_bearing_debt_pct": liquid_assets_to_interest_bearing_debt_pct,
            "equity_ratio_pct": equity_ratio_pct,
            "asset_to_equity": asset_to_equity,
            "current_ratio": current_ratio,
            "additional_metrics": additional_metrics,
            "metric_coverage": self._build_metric_coverage(additional_metrics),
            "quarterly_trend": trends.get("quarterly", []),
            "annual_trend": trends.get("annual", []),
            "source": "SEC EDGAR companyfacts",
            "filing_url": latest_filing.get("sec_url"),
        }
        for key, fact in additional_metric_facts.items():
            value = additional_metric_values.get(key)
            report[key] = self._format_metric_value(value, str(fact.get("unit") or ""))
            report[f"{key}_value"] = value
            report[f"{key}_period"] = self._fact_period_label(fact)
            report[f"{key}_tag"] = fact.get("tag")
        return report

    @staticmethod
    def _sum_optional_values(*values: Optional[float]) -> Optional[float]:
        clean = [float(value) for value in values if value is not None]
        if not clean:
            return None
        return sum(clean)

    def _pick_additional_metric_facts(
        self,
        facts: Dict[str, Any],
        latest_filing: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        metric_facts: Dict[str, Dict[str, Any]] = {}
        for key, _label, _category, tags, units in self.FINANCIAL_METRIC_SPECS:
            fact = self._pick_numeric_fact(facts, tags, units, latest_filing)
            if fact and self._fact_value(fact) is not None:
                metric_facts[key] = fact
        return metric_facts

    def _build_additional_metrics_payload(
        self,
        metric_facts: Dict[str, Dict[str, Any]],
        metric_values: Dict[str, Optional[float]],
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for key, label, category, _tags, _units in self.FINANCIAL_METRIC_SPECS:
            fact = metric_facts.get(key)
            value = metric_values.get(key)
            if not fact or value is None:
                continue
            rows.append({
                "key": key,
                "label": label,
                "category": category,
                "value": value,
                "display": self._format_metric_value(value, str(fact.get("unit") or "")),
                "period": self._fact_period_label(fact),
                "tag": fact.get("tag"),
                "unit": fact.get("unit"),
            })
        return rows

    @staticmethod
    def _build_metric_coverage(metrics: List[Dict[str, Any]]) -> Dict[str, int]:
        coverage: Dict[str, int] = {}
        for metric in metrics:
            category = str(metric.get("category") or "other")
            coverage[category] = coverage.get(category, 0) + 1
        return coverage

    def _build_financial_trends(self, facts: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        """Build compact quarterly/annual financial trend rows from SEC companyfacts."""
        metric_specs = {
            "revenue_value": (
                self.REVENUE_TAGS,
                ("USD",),
            ),
            "net_profit_parent_value": (("NetIncomeLoss",), ("USD",)),
            "operating_cash_flow_value": (("NetCashProvidedByUsedInOperatingActivities",), ("USD",)),
            "capital_expenditure_value": (
                self.CAPEX_TAGS,
                ("USD",),
            ),
            "eps_diluted_value": (("EarningsPerShareDiluted",), ("USD/shares",)),
        }

        quarterly: Dict[str, Dict[str, Any]] = {}
        annual: Dict[str, Dict[str, Any]] = {}
        metric_entries: Dict[str, List[Dict[str, Any]]] = {}

        for metric, (tags, units) in metric_specs.items():
            entries = self._iter_fact_entries(facts, tags, units)
            metric_entries[metric] = entries
            for entry in entries:
                frame = str(entry.get("frame") or "")
                duration = self._duration_days(entry)

                if self._is_quarter_frame(frame) and duration is not None and 70 <= duration <= 110:
                    key = frame.replace("I", "")
                    row = quarterly.setdefault(key, self._new_trend_row(key, entry, "quarter"))
                    self._set_trend_metric(row, metric, entry)
                elif duration is not None and 70 <= duration <= 110:
                    key = self._quarter_period_from_end_date(entry.get("end"))
                    if key:
                        row = quarterly.setdefault(key, self._new_trend_row(key, entry, "quarter"))
                        self._set_trend_metric(row, metric, entry)
                elif self._is_annual_frame(frame) and duration is not None and 330 <= duration <= 400:
                    key = frame
                    row = annual.setdefault(key, self._new_trend_row(key, entry, "annual"))
                    self._set_trend_metric(row, metric, entry)

        self._derive_missing_quarter_metrics_from_ytd(quarterly, metric_entries)
        self._derive_missing_quarters_from_ytd(quarterly, annual, metric_entries)
        quarterly_rows = self._finalize_trend_rows(
            quarterly.values(),
            limit=self.QUARTERLY_TREND_LIMIT,
            same_period_yoy=True,
        )
        annual_rows = self._finalize_trend_rows(
            annual.values(),
            limit=self.ANNUAL_TREND_LIMIT,
            same_period_yoy=False,
        )
        return {"quarterly": quarterly_rows, "annual": annual_rows}

    def _iter_fact_entries(
        self,
        facts: Dict[str, Any],
        tags: Tuple[str, ...],
        preferred_units: Tuple[str, ...],
    ) -> List[Dict[str, Any]]:
        us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        if not isinstance(us_gaap, dict):
            return []

        entries_out: List[Dict[str, Any]] = []
        for tag in tags:
            concept = us_gaap.get(tag)
            if not isinstance(concept, dict):
                continue
            units = concept.get("units") or {}
            if not isinstance(units, dict):
                continue
            ordered_units = list(preferred_units) + [unit for unit in units.keys() if unit not in preferred_units]
            tag_entries: List[Dict[str, Any]] = []
            for unit in ordered_units:
                entries = units.get(unit)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("val") is None:
                        continue
                    form = str(entry.get("form") or "")
                    if form not in self.PERIODIC_FORMS:
                        continue
                    enriched = dict(entry)
                    enriched["tag"] = tag
                    enriched["unit"] = unit
                    enriched["value"] = entry.get("val")
                    tag_entries.append(enriched)
                if tag_entries:
                    break
            entries_out.extend(tag_entries)
        return entries_out

    @staticmethod
    def _is_quarter_frame(frame: str) -> bool:
        text = frame.replace("I", "")
        return (
            len(text) == 8
            and text.startswith("CY")
            and text[2:6].isdigit()
            and text[6] == "Q"
            and text[7].isdigit()
        )

    @staticmethod
    def _is_annual_frame(frame: str) -> bool:
        return len(frame) == 6 and frame.startswith("CY") and frame[2:].isdigit()

    @staticmethod
    def _new_trend_row(period: str, entry: Dict[str, Any], period_type: str) -> Dict[str, Any]:
        return {
            "period": period,
            "period_type": period_type,
            "start_date": entry.get("start"),
            "end_date": entry.get("end"),
            "filing_date": entry.get("filed"),
            "form": entry.get("form"),
            "_metric_meta": {},
        }

    @staticmethod
    def _set_trend_metric(row: Dict[str, Any], metric: str, entry: Dict[str, Any]) -> None:
        meta = row.setdefault("_metric_meta", {})
        previous = meta.get(metric)
        current_rank = (
            str(entry.get("filed") or ""),
            str(entry.get("end") or ""),
            1 if entry.get("frame") else 0,
        )
        if previous and current_rank <= previous:
            return
        try:
            row[metric] = float(entry.get("value"))
        except (TypeError, ValueError):
            return
        row[f"{metric}_period"] = SecEdgarClient._fact_period_label(entry)
        meta[metric] = current_rank
        if str(entry.get("filed") or "") > str(row.get("filing_date") or ""):
            row["filing_date"] = entry.get("filed")
            row["form"] = entry.get("form")

    @staticmethod
    def _quarter_period_from_end_date(end_date: Any) -> str:
        try:
            parsed = datetime.fromisoformat(str(end_date))
        except (TypeError, ValueError):
            return ""
        quarter = (parsed.month - 1) // 3 + 1
        return f"CY{parsed.year}Q{quarter}"

    def _derive_missing_quarter_metrics_from_ytd(
        self,
        quarterly: Dict[str, Dict[str, Any]],
        metric_entries: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """Fill quarter metrics when a 10-Q only provides fiscal YTD values."""
        additive_metrics = (
            "revenue_value",
            "net_profit_parent_value",
            "operating_cash_flow_value",
            "capital_expenditure_value",
        )
        for metric in additive_metrics:
            entries = [
                entry
                for entry in metric_entries.get(metric, [])
                if entry.get("form") in self.QUARTERLY_FORMS
                and entry.get("start")
                and entry.get("end")
                and entry.get("value") is not None
            ]
            for current in entries:
                duration = self._duration_days(current)
                if duration is None or duration <= 110:
                    continue
                previous = self._previous_ytd_entry_for_metric(entries, current)
                if not previous:
                    continue

                derived_start = self._next_iso_date(str(previous.get("end") or ""))
                current_end = str(current.get("end") or "")
                row = self._find_existing_quarter_row_for_derived_ytd(
                    quarterly,
                    derived_start,
                    current_end,
                )
                if row is None:
                    period = self._quarter_period_from_end_date(current.get("end"))
                    if not period:
                        continue
                    row = quarterly.setdefault(period, self._new_trend_row(period, current, "quarter"))
                if row.get(metric) is not None:
                    continue
                try:
                    current_value = float(current.get("value"))
                    previous_value = float(previous.get("value"))
                except (TypeError, ValueError):
                    continue

                row[metric] = current_value - previous_value
                if not row.get("start_date"):
                    row["start_date"] = self._next_iso_date(str(previous.get("end") or ""))
                if not row.get("end_date"):
                    row["end_date"] = current.get("end")
                if str(current.get("filed") or "") > str(row.get("filing_date") or ""):
                    row["filing_date"] = current.get("filed")
                    row["form"] = current.get("form")
                row[f"{metric}_period"] = (
                    f"{row.get('start_date') or 'N/A'}~{current.get('end') or 'N/A'} "
                    f"(derived from SEC YTD {current.get('start') or 'N/A'}~{current.get('end') or 'N/A'} "
                    f"minus through {previous.get('end') or 'N/A'})"
                )
                derived_metrics = row.setdefault("derived_metrics", [])
                if metric not in derived_metrics:
                    derived_metrics.append(metric)

    @staticmethod
    def _find_existing_quarter_row_for_derived_ytd(
        quarterly: Dict[str, Dict[str, Any]],
        derived_start: str,
        current_end: str,
    ) -> Optional[Dict[str, Any]]:
        """Return an existing SEC quarter row matching a YTD-derived single quarter.

        Some fiscal-year issuers (for example NVDA) expose the true single-quarter
        fact with an SEC frame such as CY2025Q3I while the fiscal YTD fact ends in
        calendar Q4. Mapping that YTD end date with calendar quarters creates a
        duplicate CY2025Q4 row for the same 2025-07-28~2025-10-26 quarter. Prefer
        the existing frame row when the derived YTD range has the same end date.
        """
        if not current_end:
            return None
        exact_matches = []
        end_matches = []
        for row in quarterly.values():
            if not isinstance(row, dict) or str(row.get("end_date") or "") != current_end:
                continue
            end_matches.append(row)
            if derived_start and str(row.get("start_date") or "") == derived_start:
                exact_matches.append(row)
        if exact_matches:
            return exact_matches[0]
        if len(end_matches) == 1:
            return end_matches[0]
        return None

    @staticmethod
    def _previous_ytd_entry_for_metric(
        entries: List[Dict[str, Any]],
        current: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        current_start = current.get("start")
        current_end = str(current.get("end") or "")
        current_duration = SecEdgarClient._duration_days(current)
        if current_duration is None:
            return None
        candidates = []
        for entry in entries:
            if entry is current:
                continue
            if entry.get("start") != current_start:
                continue
            entry_end = str(entry.get("end") or "")
            if not entry_end or entry_end >= current_end:
                continue
            entry_duration = SecEdgarClient._duration_days(entry)
            if entry_duration is None or entry_duration >= current_duration:
                continue
            candidates.append(entry)
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (str(entry.get("end") or ""), str(entry.get("filed") or "")), reverse=True)
        return candidates[0]

    def _derive_missing_quarters_from_ytd(
        self,
        quarterly: Dict[str, Dict[str, Any]],
        annual: Dict[str, Dict[str, Any]],
        metric_entries: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """Derive fiscal Q4 rows when SEC only exposes annual and YTD facts.

        Many companies do not file a standalone 10-Q for the fiscal fourth
        quarter. For those periods, companyfacts often has annual 10-K values
        plus first-nine-month 10-Q YTD values. Revenue/net income/cash-flow
        metrics can be derived as annual minus YTD. EPS is intentionally left
        blank because per-share figures are not safely additive.
        """
        additive_metrics = (
            "revenue_value",
            "net_profit_parent_value",
            "operating_cash_flow_value",
            "capital_expenditure_value",
        )
        for annual_period, annual_row in list(annual.items()):
            annual_start = annual_row.get("start_date")
            annual_end = annual_row.get("end_date")
            if not annual_start or not annual_end:
                continue

            ytd_candidates: List[Dict[str, Any]] = []
            for entries in metric_entries.values():
                for entry in entries:
                    duration = self._duration_days(entry)
                    if (
                        entry.get("form") in self.QUARTERLY_FORMS
                        and entry.get("start") == annual_start
                        and entry.get("end")
                        and str(entry.get("end")) < str(annual_end)
                        and duration is not None
                        and 160 <= duration <= 310
                    ):
                        ytd_candidates.append(entry)
            if not ytd_candidates:
                continue
            latest_ytd_end = max(str(entry.get("end")) for entry in ytd_candidates)
            missing_period = self._missing_quarter_period_from_ytd_end(latest_ytd_end)
            if not missing_period or missing_period in quarterly:
                continue

            row: Dict[str, Any] = {
                "period": missing_period,
                "period_type": "quarter",
                "derived": True,
                "derived_note": "annual 10-K minus YTD 10-Q SEC XBRL",
                "start_date": self._next_iso_date(latest_ytd_end),
                "end_date": annual_end,
                "filing_date": annual_row.get("filing_date"),
                "form": annual_row.get("form") or "10-K",
                "_metric_meta": {},
            }
            for metric in additive_metrics:
                annual_value = annual_row.get(metric)
                if annual_value is None:
                    continue
                ytd_entry = self._latest_ytd_entry_for_metric(
                    metric_entries.get(metric, []),
                    annual_start,
                    latest_ytd_end,
                )
                if not ytd_entry:
                    continue
                try:
                    row[metric] = float(annual_value) - float(ytd_entry.get("value"))
                except (TypeError, ValueError):
                    continue
                row[f"{metric}_period"] = (
                    f"{row.get('start_date') or 'N/A'}~{annual_end} "
                    "(derived from annual 10-K minus YTD 10-Q)"
                )
            if row.get("revenue_value") is not None or row.get("net_profit_parent_value") is not None:
                quarterly[missing_period] = row

    @staticmethod
    def _missing_quarter_period_from_ytd_end(ytd_end: str) -> str:
        try:
            end_date = datetime.fromisoformat(str(ytd_end))
        except ValueError:
            return ""
        completed_quarter = (end_date.month - 1) // 3 + 1
        missing_quarter = completed_quarter + 1
        if missing_quarter > 4:
            return ""
        return f"CY{end_date.year}Q{missing_quarter}"

    @staticmethod
    def _next_iso_date(date_text: str) -> str:
        try:
            return (datetime.fromisoformat(str(date_text)) + timedelta(days=1)).date().isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _latest_ytd_entry_for_metric(
        entries: List[Dict[str, Any]],
        annual_start: Any,
        ytd_end: str,
    ) -> Optional[Dict[str, Any]]:
        candidates = [
            entry
            for entry in entries
            if entry.get("form") in SecEdgarClient.QUARTERLY_FORMS
            and entry.get("start") == annual_start
            and str(entry.get("end") or "") == ytd_end
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda entry: str(entry.get("filed") or ""), reverse=True)
        return candidates[0]

    def _finalize_trend_rows(
        self,
        rows: Any,
        limit: int,
        same_period_yoy: bool,
    ) -> List[Dict[str, Any]]:
        sorted_rows = [
            dict(row)
            for row in rows
            if isinstance(row, dict)
            and (row.get("revenue_value") is not None or row.get("net_profit_parent_value") is not None)
        ]
        sorted_rows.sort(key=lambda row: str(row.get("end_date") or row.get("period") or ""), reverse=True)

        for row in sorted_rows:
            ocf = row.get("operating_cash_flow_value")
            capex = row.get("capital_expenditure_value")
            if row.get("free_cash_flow_value") is None and ocf is not None and capex is not None:
                row["free_cash_flow_value"] = float(ocf) - abs(float(capex))

        by_period = {str(row.get("period")): row for row in sorted_rows}
        for idx, row in enumerate(sorted_rows):
            older = sorted_rows[idx + 1] if idx + 1 < len(sorted_rows) else None
            for metric in ("revenue_value", "net_profit_parent_value", "operating_cash_flow_value", "free_cash_flow_value"):
                if older:
                    row[f"{metric}_change_pct"] = self._safe_change_pct(row.get(metric), older.get(metric))
            if same_period_yoy:
                prev_period = self._previous_year_period(str(row.get("period") or ""))
                prev_year_row = by_period.get(prev_period)
                if prev_year_row:
                    for metric in ("revenue_value", "net_profit_parent_value"):
                        row[f"{metric}_yoy_pct"] = self._safe_change_pct(row.get(metric), prev_year_row.get(metric))

            row["net_margin_pct"] = self._safe_ratio_pct(row.get("net_profit_parent_value"), row.get("revenue_value"))
            row["revenue"] = self._format_usd_value(row.get("revenue_value"))
            row["net_profit_parent"] = self._format_usd_value(row.get("net_profit_parent_value"))
            row["operating_cash_flow"] = self._format_usd_value(row.get("operating_cash_flow_value"))
            row["free_cash_flow"] = self._format_usd_value(row.get("free_cash_flow_value"))
            row["eps_diluted"] = (
                "N/A" if row.get("eps_diluted_value") is None else f"{float(row['eps_diluted_value']):.2f}"
            )
            row.pop("_metric_meta", None)
        return sorted_rows[:limit]

    @staticmethod
    def _previous_year_period(period: str) -> str:
        if (
            len(period) == 8
            and period.startswith("CY")
            and period[2:6].isdigit()
            and period[6] == "Q"
            and period[7].isdigit()
        ):
            return f"CY{int(period[2:6]) - 1}{period[6:]}"
        return ""

    @staticmethod
    def _safe_change_pct(current: Any, previous: Any) -> Optional[float]:
        try:
            current_number = float(current)
            previous_number = float(previous)
        except (TypeError, ValueError):
            return None
        if previous_number == 0:
            return None
        return round((current_number - previous_number) / abs(previous_number) * 100.0, 2)

    def _build_yahoo_dividend_metrics(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Return trailing-12-month cash dividend metrics from Yahoo Finance events."""
        try:
            response = requests.get(
                self.YAHOO_CHART_URL.format(ticker=ticker),
                params={
                    "range": "2y",
                    "interval": "1d",
                    "events": "div",
                },
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=min(self.timeout, 5.0),
            )
            if response.status_code != 200:
                raise SecEdgarError(f"Yahoo Finance HTTP {response.status_code}")
            payload = response.json()
            results = ((payload.get("chart") or {}).get("result") or [])
            if not results or not isinstance(results[0], dict):
                raise SecEdgarError("Yahoo Finance response has no chart result")
            dividends = (((results[0].get("events") or {}).get("dividends")) or {})
        except Exception as exc:
            logger.debug("Yahoo Finance dividend lookup failed for %s: %s", ticker, exc)
            return None

        if not isinstance(dividends, dict) or not dividends:
            return {
                "ttm_cash_dividend_per_share": 0.0,
                "ttm_dividend_yield_pct": "N/A",
                "ttm_event_count": 0,
                "events": [],
                "latest_dividend_fact": "No dividend events found",
                "source": "Yahoo Finance dividends",
            }

        today = datetime.now(timezone.utc).date()
        ttm_start = today - timedelta(days=365)
        events: List[Dict[str, Any]] = []
        latest_event: Optional[Dict[str, Any]] = None

        for item in dividends.values():
            if not isinstance(item, dict):
                continue
            raw_ts = item.get("date")
            if raw_ts is None:
                continue
            try:
                event_date = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).date()
            except (TypeError, ValueError, OSError):
                continue
            try:
                amount = round(float(item.get("amount")), 6)
            except (TypeError, ValueError):
                continue
            if event_date > today:
                continue
            event = {
                "event_date": event_date.isoformat(),
                "cash_dividend_per_share": amount,
            }
            if latest_event is None or event["event_date"] > latest_event["event_date"]:
                latest_event = event
            if ttm_start <= event_date <= today:
                events.append(event)

        events.sort(key=lambda item: item["event_date"], reverse=True)
        ttm_cash = round(sum(float(item["cash_dividend_per_share"]) for item in events), 6)
        latest_text = "N/A"
        if latest_event:
            latest_text = (
                f"{latest_event['cash_dividend_per_share']} USD/shares "
                f"on {latest_event['event_date']}"
            )
        return {
            "ttm_cash_dividend_per_share": ttm_cash,
            "ttm_dividend_yield_pct": "N/A",
            "ttm_event_count": len(events),
            "events": events[:8],
            "latest_dividend_fact": latest_text,
            "source": "Yahoo Finance dividends",
        }

    def _build_sec_dividend_metrics(self, facts: Dict[str, Any], latest_filing: Dict[str, Any]) -> Dict[str, Any]:
        dividend = self._pick_numeric_fact(
            facts,
            ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends", "CommonStockDividendsPerShareDeclared"),
            ("USD", "USD/shares"),
            latest_filing,
        )
        return {
            "ttm_cash_dividend_per_share": "N/A",
            "ttm_dividend_yield_pct": "N/A",
            "ttm_event_count": "N/A",
            "latest_dividend_fact": self._format_plain_fact(dividend),
            "source": "SEC EDGAR companyfacts",
        }

    def _pick_numeric_fact(
        self,
        facts: Dict[str, Any],
        tags: Tuple[str, ...],
        preferred_units: Tuple[str, ...],
        latest_filing: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        if not isinstance(us_gaap, dict):
            return None

        candidates: List[Tuple[int, Dict[str, Any]]] = []
        for tag in tags:
            concept = us_gaap.get(tag)
            if not isinstance(concept, dict):
                continue
            units = concept.get("units") or {}
            if not isinstance(units, dict):
                continue

            ordered_units = list(preferred_units) + [unit for unit in units.keys() if unit not in preferred_units]
            tag_candidates: List[Tuple[int, Dict[str, Any]]] = []
            for unit in ordered_units:
                entries = units.get(unit)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("val") is None:
                        continue
                    form = str(entry.get("form") or "")
                    if form not in self.PERIODIC_FORMS:
                        continue
                    score = self._score_fact(entry, latest_filing)
                    enriched = dict(entry)
                    enriched["tag"] = tag
                    enriched["unit"] = unit
                    enriched["value"] = entry.get("val")
                    tag_candidates.append((score, enriched))
                if tag_candidates:
                    break
            candidates.extend(tag_candidates)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                item[0],
                str(item[1].get("filed") or ""),
                str(item[1].get("end") or ""),
            ),
            reverse=True,
        )
        return candidates[0][1]

    def _score_fact(self, entry: Dict[str, Any], latest_filing: Dict[str, Any]) -> int:
        score = 0
        if entry.get("accn") == latest_filing.get("accession_number"):
            score += 100
        if entry.get("form") == latest_filing.get("form"):
            score += 20
        if entry.get("end") == latest_filing.get("report_date"):
            score += 40
        if entry.get("frame"):
            score += 8

        duration = self._duration_days(entry)
        form = str(latest_filing.get("form") or "")
        if duration is not None:
            if form.startswith("10-Q"):
                if 70 <= duration <= 110:
                    score += 15
                elif duration > 120:
                    score -= 5
            elif form.startswith("10-K") and 330 <= duration <= 400:
                score += 15
        return score

    @staticmethod
    def _duration_days(entry: Dict[str, Any]) -> Optional[int]:
        start = entry.get("start")
        end = entry.get("end")
        if not start or not end:
            return None
        try:
            return (datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).days
        except ValueError:
            return None

    @staticmethod
    def _format_period(filing: Dict[str, Any]) -> str:
        form = filing.get("form") or "N/A"
        report_date = filing.get("report_date") or "N/A"
        return f"{form} period ended {report_date}"

    @staticmethod
    def _format_usd_fact(fact: Optional[Dict[str, Any]]) -> str:
        if not fact or fact.get("value") is None:
            return "N/A"
        return SecEdgarClient._format_usd_value(fact.get("value"))

    @staticmethod
    def _format_usd_value(value: Any) -> str:
        if value is None:
            return "N/A"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "N/A"
        sign = "-" if value < 0 else ""
        abs_value = abs(value)
        if abs_value >= 1_000_000_000:
            return f"{sign}${abs_value / 1_000_000_000:.2f}B"
        if abs_value >= 1_000_000:
            return f"{sign}${abs_value / 1_000_000:.2f}M"
        return f"{sign}${abs_value:,.0f}"

    @classmethod
    def _format_metric_value(cls, value: Any, unit: str) -> str:
        if value is None:
            return "N/A"
        if unit == "USD":
            return cls._format_usd_value(value)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return str(value)
        if unit == "USD/shares":
            return f"{numeric:.2f} USD/shares".rstrip("0").rstrip(".")
        if unit == "shares":
            sign = "-" if numeric < 0 else ""
            abs_value = abs(numeric)
            if abs_value >= 1_000_000_000:
                return f"{sign}{abs_value / 1_000_000_000:.2f}B shares"
            if abs_value >= 1_000_000:
                return f"{sign}{abs_value / 1_000_000:.2f}M shares"
            return f"{sign}{abs_value:,.0f} shares"
        if abs(numeric) >= 1:
            return f"{numeric:,.2f} {unit}".rstrip("0").rstrip(".").strip()
        return f"{numeric:.4f} {unit}".rstrip("0").rstrip(".").strip()

    @staticmethod
    def _fact_value(fact: Optional[Dict[str, Any]]) -> Optional[float]:
        if not fact or fact.get("value") is None:
            return None
        try:
            return float(fact["value"])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
        if numerator is None or denominator in (None, 0):
            return None
        try:
            return round(float(numerator) / float(denominator), 4)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @classmethod
    def _safe_ratio_pct(cls, numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
        ratio = cls._safe_ratio(numerator, denominator)
        if ratio is None:
            return None
        return round(ratio * 100.0, 2)

    @staticmethod
    def _fact_period_label(fact: Optional[Dict[str, Any]]) -> str:
        if not fact:
            return "N/A"
        start = fact.get("start")
        end = fact.get("end")
        filed = fact.get("filed")
        if start and end:
            return f"{start}~{end} (filed {filed or 'N/A'})"
        if end:
            return f"as of {end} (filed {filed or 'N/A'})"
        return f"filed {filed}" if filed else "N/A"

    @staticmethod
    def _format_plain_fact(fact: Optional[Dict[str, Any]]) -> str:
        if not fact or fact.get("value") is None:
            return "N/A"
        value = fact.get("value")
        unit = fact.get("unit") or ""
        try:
            numeric = float(value)
            if abs(numeric) >= 1:
                value_text = f"{numeric:,.2f}".rstrip("0").rstrip(".")
            else:
                value_text = f"{numeric:.4f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            value_text = str(value)
        return f"{value_text} {unit}".strip()
