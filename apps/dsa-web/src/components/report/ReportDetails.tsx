import type React from 'react';
import { useEffect, useRef, useState } from 'react';
import type { ReportDetails as ReportDetailsType, ReportLanguage } from '../../types/analysis';
import { Card } from '../common';
import { DashboardPanelHeader } from '../dashboard';
import { getReportText, normalizeReportLanguage } from '../../utils/reportLanguage';

interface ReportDetailsProps {
  details?: ReportDetailsType;
  recordId?: number;  // 分析历史记录主键 ID
  language?: ReportLanguage;
}

type UnknownRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is UnknownRecord => (
  typeof value === 'object' && value !== null && !Array.isArray(value)
);

const pickValue = (record: UnknownRecord | undefined, ...keys: string[]): unknown => {
  if (!record) return undefined;
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null && value !== '') {
      return value;
    }
  }
  return undefined;
};

const displayValue = (value?: unknown, fallback = 'N/A'): string => {
  if (value === undefined || value === null || value === '') return fallback;
  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(value) : fallback;
  }
  return String(value);
};

const hasDisplayableValue = (value: unknown): boolean => {
  if (value === undefined || value === null || value === '') return false;
  if (typeof value === 'string' && value.trim().toUpperCase() === 'N/A') return false;
  return true;
};

const getFinancialExtraSections = (financialReport: UnknownRecord | undefined): Array<{
  title: string;
  items: Array<[string, unknown]>;
}> => {
  if (!financialReport) return [];
  const sectionDefs: Array<{
    title: string;
    items: Array<[string, unknown]>;
  }> = [
    {
      title: '利润表扩展',
      items: [
        ['毛利', pickValue(financialReport, 'grossProfit', 'gross_profit')],
        ['毛利率', pickValue(financialReport, 'grossMarginPct', 'gross_margin_pct')],
        ['营业利润', pickValue(financialReport, 'operatingIncome', 'operating_income')],
        ['营业利润率', pickValue(financialReport, 'operatingMarginPct', 'operating_margin_pct')],
        ['税前利润', pickValue(financialReport, 'pretaxIncome', 'pretax_income')],
        ['税前利润率', pickValue(financialReport, 'pretaxMarginPct', 'pretax_margin_pct')],
        ['所得税', pickValue(financialReport, 'incomeTaxExpense', 'income_tax_expense')],
        ['基本 EPS', pickValue(financialReport, 'epsBasic', 'eps_basic')],
      ],
    },
    {
      title: '费用与金融股口径',
      items: [
        ['营业成本', pickValue(financialReport, 'costOfRevenue', 'cost_of_revenue')],
        ['研发费用', pickValue(financialReport, 'researchAndDevelopment', 'research_and_development')],
        ['销售管理费用', pickValue(financialReport, 'sellingGeneralAdmin', 'selling_general_admin')],
        ['利息收入', pickValue(financialReport, 'interestIncome', 'interest_income')],
        ['利息支出', pickValue(financialReport, 'interestExpense', 'interest_expense')],
        ['净利息收入', pickValue(financialReport, 'netInterestIncome', 'net_interest_income')],
        ['信用损失准备', pickValue(financialReport, 'provisionForCreditLosses', 'provision_for_credit_losses')],
      ],
    },
    {
      title: '资产负债补充',
      items: [
        ['流动资产', pickValue(financialReport, 'currentAssets', 'current_assets')],
        ['流动负债', pickValue(financialReport, 'currentLiabilities', 'current_liabilities')],
        ['流动比率', pickValue(financialReport, 'currentRatio', 'current_ratio')],
        ['应收账款', pickValue(financialReport, 'accountsReceivable', 'accounts_receivable')],
        ['存货', pickValue(financialReport, 'inventory')],
        ['应付账款', pickValue(financialReport, 'accountsPayable', 'accounts_payable')],
        ['短期借款', pickValue(financialReport, 'shortTermBorrowings', 'short_term_borrowings')],
        ['总债务', pickValue(financialReport, 'totalDebt', 'total_debt')],
        ['总债务/资产', pickValue(financialReport, 'totalDebtToAssetsPct', 'total_debt_to_assets_pct')],
        ['净现金', pickValue(financialReport, 'netCash', 'net_cash')],
      ],
    },
    {
      title: '现金流补充',
      items: [
        ['折旧摊销', pickValue(financialReport, 'depreciationAmortization', 'depreciation_amortization')],
        ['股权激励', pickValue(financialReport, 'shareBasedCompensation', 'share_based_compensation')],
        ['分红支付', pickValue(financialReport, 'dividendsPaid', 'dividends_paid')],
        ['股票回购', pickValue(financialReport, 'stockRepurchases', 'stock_repurchases')],
        ['摊薄股数', pickValue(financialReport, 'dilutedShares', 'diluted_shares')],
      ],
    },
  ];

  return sectionDefs
    .map((section) => ({
      ...section,
      items: section.items.filter(([, value]) => hasDisplayableValue(value)),
    }))
    .filter((section) => section.items.length > 0);
};

/**
 * 透明度与追溯区组件 - 终端风格
 */
export const ReportDetails: React.FC<ReportDetailsProps> = ({
  details,
  recordId,
  language = 'zh',
}) => {
  type JsonPanel = 'raw' | 'snapshot';
  type CopiedPanelState = Record<JsonPanel, boolean>;

  const reportLanguage = normalizeReportLanguage(language);
  const text = getReportText(reportLanguage);
  const [showRaw, setShowRaw] = useState(false);
  const [showSnapshot, setShowSnapshot] = useState(false);
  const [copiedPanels, setCopiedPanels] = useState<CopiedPanelState>({
    raw: false,
    snapshot: false,
  });
  const copyResetTimerRef = useRef<Partial<Record<JsonPanel, number>>>({});
  const financialReport = isRecord(details?.financialReport) ? details?.financialReport : undefined;
  const financialExtraSections = getFinancialExtraSections(financialReport);

  useEffect(() => {
    return () => {
      Object.values(copyResetTimerRef.current).forEach((timerId) => {
        if (timerId !== undefined) {
          window.clearTimeout(timerId);
        }
      });
      copyResetTimerRef.current = {};
    };
  }, []);

  if (!details?.rawResult && !details?.contextSnapshot && !recordId && financialExtraSections.length === 0) {
    return null;
  }

  const copyToClipboard = async (content: string, panel: JsonPanel) => {
    try {
      await navigator.clipboard.writeText(content);
      setCopiedPanels((prev) => ({
        ...prev,
        [panel]: true,
      }));
      const existingTimer = copyResetTimerRef.current[panel];
      if (existingTimer !== undefined) {
        window.clearTimeout(existingTimer);
      }
      copyResetTimerRef.current[panel] = window.setTimeout(() => {
        setCopiedPanels((prev) => ({
          ...prev,
          [panel]: false,
        }));
        delete copyResetTimerRef.current[panel];
      }, 2000);
    } catch (err) {
      console.error('Copy failed:', err);
    }
  };

  const renderJson = (data: unknown, panel: JsonPanel) => {
    const jsonStr = JSON.stringify(data, null, 2);
    return (
      <div className="relative overflow-hidden">
        <span className="absolute top-2 right-2 z-10 inline-flex">
          <button
            type="button"
            onClick={() => copyToClipboard(jsonStr, panel)}
            className="home-accent-link text-xs text-muted-text"
            aria-label={copiedPanels[panel] ? text.copied : text.copy}
          >
            {copiedPanels[panel] ? text.copied : text.copy}
          </button>
        </span>
        <pre className="home-trace-pre home-trace-pre-content text-xs text-foreground font-mono overflow-x-auto p-3 bg-base rounded-lg max-h-80 overflow-y-auto text-left w-0 min-w-full">
          {jsonStr}
        </pre>
      </div>
    );
  };

  return (
    <Card variant="bordered" padding="md" className="home-panel-card text-left">
      <DashboardPanelHeader
        eyebrow={text.transparency}
        title={text.traceability}
        className="mb-3"
      />

      {/* Record ID */}
      {recordId && (
        <div className="home-divider mb-3 flex items-center gap-2 border-b pb-3 text-xs text-muted-text">
          <span>{text.recordId}:</span>
          <code className="home-accent-chip px-1.5 py-0.5 font-mono text-xs">
            {recordId}
          </code>
        </div>
      )}

      {/* 折叠区域 */}
      <div className="space-y-2">
        {financialExtraSections.length > 0 && (
          <div className="home-subpanel rounded-lg px-3 py-3">
            <div className="mb-2 text-xs font-medium text-foreground">SEC 扩展字段</div>
            <div className="grid gap-2 lg:grid-cols-2">
              {financialExtraSections.map((section) => (
                <div key={section.title} className="rounded-md border border-subtle px-3 py-2">
                  <div className="mb-2 text-[11px] font-medium text-muted-text">{section.title}</div>
                  <div className="space-y-1.5 text-xs">
                    {section.items.map(([label, value]) => (
                      <div key={`${section.title}-${label}`} className="flex items-center justify-between gap-3">
                        <span className="text-muted-text">{label}</span>
                        <span className="text-right text-foreground">{displayValue(value)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        {/* 原始分析结果 */}
        {details?.rawResult && (
          <div>
            <button
              type="button"
              onClick={() => setShowRaw(!showRaw)}
              className="home-surface-button home-trace-toggle flex w-full items-center justify-between rounded-lg p-2.5"
            >
              <span className="text-xs text-foreground">{text.rawResult}</span>
              <svg
                className={`w-3.5 h-3.5 text-muted-text transition-transform ${showRaw ? 'rotate-180' : ''}`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {showRaw && (
              <div className="mt-2 animate-fade-in min-w-0 overflow-hidden">
                {renderJson(details.rawResult, 'raw')}
              </div>
            )}
          </div>
        )}

        {/* 分析快照 */}
        {details?.contextSnapshot && (
          <div>
            <button
              type="button"
              onClick={() => setShowSnapshot(!showSnapshot)}
              className="home-surface-button home-trace-toggle flex w-full items-center justify-between rounded-lg p-2.5"
            >
              <span className="text-xs text-foreground">{text.analysisSnapshot}</span>
              <svg
                className={`w-3.5 h-3.5 text-muted-text transition-transform ${showSnapshot ? 'rotate-180' : ''}`}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {showSnapshot && (
              <div className="mt-2 animate-fade-in min-w-0 overflow-hidden">
                {renderJson(details.contextSnapshot, 'snapshot')}
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
};
