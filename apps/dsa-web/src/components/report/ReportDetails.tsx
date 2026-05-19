import type React from 'react';
import { useEffect, useRef, useState } from 'react';
import type { ReportDetails as ReportDetailsType, ReportLanguage } from '../../types/analysis';
import { Card, Drawer } from '../common';
import { DashboardPanelHeader } from '../dashboard';
import { getReportText, normalizeReportLanguage } from '../../utils/reportLanguage';

interface ReportDetailsProps {
  details?: ReportDetailsType;
  recordId?: number;  // 分析历史记录主键 ID
  language?: ReportLanguage;
}

type UnknownRecord = Record<string, unknown>;

interface FilingReference {
  form?: string;
  reportDate?: string;
  filingDate?: string;
  url?: string;
}

interface SearchContextSection {
  title: string;
  source: string;
  items: string[];
}

interface ExpandedTextItem {
  title: string;
  body: string;
  subtitle?: string;
}

const isRecord = (value: unknown): value is UnknownRecord => (
  typeof value === 'object' && value !== null && !Array.isArray(value)
);

const pickString = (record: UnknownRecord | undefined, key: string): string | undefined => {
  const value = record?.[key];
  return typeof value === 'string' && value.trim() ? value : undefined;
};

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

const toNumber = (value: unknown): number | undefined => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'string') return undefined;
  const normalized = value.replace(/[$,%]/g, '').trim();
  if (!normalized || normalized.toUpperCase() === 'N/A') return undefined;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : undefined;
};

const formatNumber = (value: unknown, digits = 2): string => {
  const parsed = toNumber(value);
  if (parsed === undefined) return displayValue(value);
  return parsed.toFixed(digits);
};

const formatSignedPct = (value: unknown): string => {
  const parsed = toNumber(value);
  if (parsed === undefined) return displayValue(value, '不可比');
  const sign = parsed > 0 ? '+' : '';
  return `${sign}${parsed.toFixed(2)}%`;
};

const asRecordArray = (value: unknown): UnknownRecord[] => (
  Array.isArray(value) ? value.filter(isRecord) : []
);

const truncateText = (value: string, maxLength = 360): string => (
  value.length > maxLength ? `${value.slice(0, maxLength).trim()}...` : value
);

const formatCompactNumber = (value: number): string => {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  if (abs >= 10) return value.toFixed(0);
  return value.toFixed(2);
};

const cleanSearchContextText = (value: string): string => (
  value
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/\[\s*\.\.\.\s*\]/g, '')
    .replace(/#+/g, '')
    .replace(/\s+/g, ' ')
    .trim()
);

const stripLeadingSearchIcon = (value: string): string => {
  const chars = Array.from(value.trim());
  while (chars.length > 0 && !/[A-Za-z0-9\u3400-\u9fff]/.test(chars[0])) {
    chars.shift();
  }
  return chars.join('').trim();
};

const shouldSkipSearchContextSection = (value: string): boolean => {
  const normalized = value.toLowerCase();
  return [
    'sec form 144',
    'form 144',
    'sec 内部人',
    '内部人交易',
    '内部人申报',
    'insider transaction',
    'insider trading',
    'insider filing',
  ].some((token) => normalized.includes(token));
};

const isLowSignalSearchItem = (value: string): boolean => {
  const normalized = value.trim().toLowerCase();
  if (!normalized || normalized === '...' || normalized === '[...]') return true;
  if (/^\d+$/.test(normalized)) return true;
  if (/^watch\s+live\b/.test(normalized)) return true;
  if (/^image\s+\d+\s*:/i.test(normalized)) return true;
  if (/^(read more|click here|sign up|subscribe)$/i.test(normalized)) return true;
  return false;
};

const readNestedRecord = (record: UnknownRecord | undefined, path: string[]): UnknownRecord | undefined => {
  let current: unknown = record;
  for (const key of path) {
    if (!isRecord(current)) return undefined;
    current = current[key];
  }
  return isRecord(current) ? current : undefined;
};

const normalizeFilingReferences = (value: unknown): FilingReference[] => {
  if (!Array.isArray(value)) return [];
  return value
    .filter(isRecord)
    .map((item) => ({
      form: pickString(item, 'form'),
      reportDate: pickString(item, 'reportDate') || pickString(item, 'report_date'),
      filingDate: pickString(item, 'filingDate') || pickString(item, 'filing_date'),
      url: (
        pickString(item, 'inlineXbrlUrl')
        || pickString(item, 'inline_xbrl_url')
        || pickString(item, 'secUrl')
        || pickString(item, 'sec_url')
        || pickString(item, 'url')
      ),
    }))
    .filter((item) => item.form || item.url)
    .slice(0, 5);
};

const extractFilingReferences = (details?: ReportDetailsType): FilingReference[] => {
  const rawResult = isRecord(details?.rawResult) ? details?.rawResult : undefined;
  const rawRefs = normalizeFilingReferences(rawResult?.filingReferences || rawResult?.filing_references);
  if (rawRefs.length > 0) return rawRefs;

  const contextSnapshot = isRecord(details?.contextSnapshot) ? details?.contextSnapshot : undefined;
  const filings = (
    readNestedRecord(contextSnapshot, ['fundamentalContext', 'earnings', 'data', 'filings'])
    || readNestedRecord(contextSnapshot, ['fundamental_context', 'earnings', 'data', 'filings'])
  );
  return normalizeFilingReferences(filings?.filingReferences || filings?.filing_references);
};

const extractSnapshotRecord = (
  details: ReportDetailsType | undefined,
  directKey: keyof ReportDetailsType,
  rawKeys: string[],
  contextPaths: string[][],
): UnknownRecord | undefined => {
  const direct = details?.[directKey];
  if (isRecord(direct)) return direct;

  const rawResult = isRecord(details?.rawResult) ? details?.rawResult : undefined;
  for (const key of rawKeys) {
    const value = rawResult?.[key];
    if (isRecord(value)) return value;
  }

  const contextSnapshot = isRecord(details?.contextSnapshot) ? details?.contextSnapshot : undefined;
  for (const path of contextPaths) {
    const value = readNestedRecord(contextSnapshot, path);
    if (value) return value;
  }
  return undefined;
};

const extractNewsContextSnapshot = (details?: ReportDetailsType): string => {
  if (typeof details?.newsContextSnapshot === 'string' && details.newsContextSnapshot.trim()) {
    return details.newsContextSnapshot;
  }
  const rawResult = isRecord(details?.rawResult) ? details?.rawResult : undefined;
  const rawValue = pickValue(rawResult, 'newsContextSnapshot', 'news_context_snapshot');
  if (typeof rawValue === 'string' && rawValue.trim()) return rawValue;
  return '';
};

const parseSearchContextSections = (content: string): SearchContextSection[] => {
  const sections: SearchContextSection[] = [];
  let current: SearchContextSection | undefined;

  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const header = line.match(/^(.*?)\s*\(来源[:：]\s*([^)]+)\):$/);
    if (header) {
      const title = stripLeadingSearchIcon(cleanSearchContextText(header[1]));
      if (shouldSkipSearchContextSection(title)) {
        current = undefined;
        continue;
      }
      current = {
        title,
        source: cleanSearchContextText(header[2]),
        items: [],
      };
      sections.push(current);
      continue;
    }
    if (!current) continue;
    const itemMatch = line.match(/^\d+[.)]\s+(.+)$/) || line.match(/^[-*]\s+(.+)$/);
    if (!itemMatch) continue;
    const normalized = cleanSearchContextText(itemMatch[1]);
    if (!normalized.startsWith('【') && !isLowSignalSearchItem(normalized)) {
      current.items.push(normalized);
    }
  }

  return sections
    .map((section) => ({
      ...section,
      items: Array.from(new Set(section.items)).slice(0, 3),
    }))
    .filter((section) => section.title && section.items.length > 0)
    .slice(0, 8);
};

const getTrendRows = (financialReport: UnknownRecord | undefined, key: 'quarterlyTrend' | 'annualTrend'): UnknownRecord[] => (
  asRecordArray(pickValue(
    financialReport,
    key,
    key === 'quarterlyTrend' ? 'quarterly_trend' : 'annual_trend',
  ))
);

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

const trendMetricValue = (row: UnknownRecord, camelKey: string, snakeKey: string): unknown => (
  pickValue(row, camelKey, snakeKey)
);

const MiniTrendLine: React.FC<{
  rows: UnknownRecord[];
  metric: string;
  snakeMetric: string;
  label: string;
}> = ({
  rows,
  metric,
  snakeMetric,
  label,
}) => {
  const [hoveredPoint, setHoveredPoint] = useState<number | null>(null);
  const dataPoints = rows
    .slice()
    .reverse()
    .map((row) => ({
      period: displayValue(pickValue(row, 'period'), ''),
      value: toNumber(trendMetricValue(row, metric, snakeMetric)),
    }))
    .filter((point): point is { period: string; value: number } => point.value !== undefined);
  const values = dataPoints.map((point) => point.value);

  if (values.length < 2) return null;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const plot = {
    left: 64,
    top: 22,
    right: 356,
    bottom: 142,
  };
  const plotWidth = plot.right - plot.left;
  const plotHeight = plot.bottom - plot.top;
  const pointPositions = values.map((value, index) => {
    const x = values.length === 1 ? plot.left + plotWidth / 2 : plot.left + (index / (values.length - 1)) * plotWidth;
    const y = plot.bottom - ((value - min) / range) * plotHeight;
    return { x, y };
  });
  const points = pointPositions.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    ratio,
    y: plot.bottom - ratio * plotHeight,
    value: min + ratio * range,
  }));
  const hoveredData = hoveredPoint !== null ? dataPoints[hoveredPoint] : null;
  const hoveredPosition = hoveredPoint !== null ? pointPositions[hoveredPoint] : null;

  return (
    <div className="w-full text-[11px] text-muted-text" aria-label={`${label}：左旧右新，纵轴为数值区间`}>
      <div className="mb-1.5 text-left">{label}</div>
      <svg viewBox="0 0 380 170" className="h-40 w-full max-w-[28rem] overflow-visible" role="img" aria-label={`${label}趋势图`}>
        <rect
          x={plot.left}
          y={plot.top}
          width={plotWidth}
          height={plotHeight}
          rx="8"
          fill="currentColor"
          opacity="0.03"
          className="text-cyan"
        />
        <line x1={plot.left} y1={plot.top} x2={plot.left} y2={plot.bottom} stroke="currentColor" strokeWidth="1" strokeOpacity="0.4" className="text-muted-text" />
        <line x1={plot.left} y1={plot.bottom} x2={plot.right} y2={plot.bottom} stroke="currentColor" strokeWidth="1" strokeOpacity="0.4" className="text-muted-text" />
        {yTicks.map((tick) => (
          <g key={tick.ratio}>
            <line
              x1={plot.left}
              y1={tick.y}
              x2={plot.right}
              y2={tick.y}
              stroke="currentColor"
              strokeWidth="0.6"
              strokeOpacity="0.24"
              strokeDasharray="3 4"
              className="text-muted-text"
            />
            <text
              x={plot.left - 10}
              y={tick.y + 4}
              textAnchor="end"
              className="fill-current text-[10px] text-muted-text"
            >
              {formatCompactNumber(tick.value)}
            </text>
          </g>
        ))}
        {dataPoints.map((point, index) => {
          const position = pointPositions[index];
          return (
            <g key={`${point.period}-${index}`}>
              <line
                x1={position.x}
                y1={plot.top}
                x2={position.x}
                y2={plot.bottom}
                stroke="currentColor"
                strokeWidth="0.5"
                strokeOpacity="0.16"
                strokeDasharray="2 6"
                className="text-muted-text"
              />
              <text
                x={position.x}
                y={160}
                textAnchor="middle"
                className="fill-current text-[10px] text-muted-text"
              >
                {point.period}
              </text>
            </g>
          );
        })}
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth="4"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="text-cyan"
        />
        {hoveredPosition && hoveredData ? (
          <g pointerEvents="none">
            <line
              x1={hoveredPosition.x}
              y1={plot.top}
              x2={hoveredPosition.x}
              y2={plot.bottom}
              stroke="currentColor"
              strokeWidth="1"
              strokeOpacity="0.4"
              className="text-cyan"
            />
            <rect
              x={Math.min(Math.max(hoveredPosition.x - 52, plot.left), plot.right - 104)}
              y={Math.max(hoveredPosition.y - 34, 2)}
              width="104"
              height="24"
              rx="6"
              className="fill-card stroke-cyan"
              strokeOpacity="0.6"
            />
            <text
              x={Math.min(Math.max(hoveredPosition.x, plot.left + 52), plot.right - 52)}
              y={Math.max(hoveredPosition.y - 18, 18)}
              textAnchor="middle"
              className="fill-current text-[11px] font-medium text-foreground"
            >
              {hoveredData.period}: {formatCompactNumber(hoveredData.value)}
            </text>
          </g>
        ) : null}
        {dataPoints.map((point, index) => {
          const { x, y } = pointPositions[index];
          const isHovered = hoveredPoint === index;
          return (
            <circle
              key={`${point.period}-${point.value}-${index}`}
              cx={x}
              cy={y}
              r={isHovered ? 7 : 4.5}
              className="fill-current text-cyan transition-all duration-150"
              stroke="currentColor"
              strokeWidth={isHovered ? 4 : 2}
              strokeOpacity={isHovered ? 0.28 : 0.18}
              onMouseEnter={() => setHoveredPoint(index)}
              onMouseLeave={() => setHoveredPoint(null)}
            />
          );
        })}
      </svg>
    </div>
  );
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
  const [expandedTextItem, setExpandedTextItem] = useState<ExpandedTextItem | null>(null);
  const copyResetTimerRef = useRef<Partial<Record<JsonPanel, number>>>({});
  const rawResult = isRecord(details?.rawResult) ? details?.rawResult : undefined;
  const financialReport = isRecord(details?.financialReport) ? details?.financialReport : undefined;
  const dividendMetrics = isRecord(details?.dividendMetrics) ? details?.dividendMetrics : undefined;
  const filingReferences = extractFilingReferences(details);
  const quarterlyTrendRows = getTrendRows(financialReport, 'quarterlyTrend');
  const annualTrendRows = getTrendRows(financialReport, 'annualTrend');
  const financialExtraSections = getFinancialExtraSections(financialReport);
  const technicalSnapshot = extractSnapshotRecord(
    details,
    'technicalIndicatorSnapshot',
    ['technicalIndicatorSnapshot', 'technical_indicator_snapshot'],
    [
      ['enhancedContext', 'trendAnalysis'],
      ['enhanced_context', 'trend_analysis'],
    ],
  );
  const macroSnapshot = extractSnapshotRecord(
    details,
    'macroSnapshot',
    ['macroSnapshot', 'macro_snapshot'],
    [
      ['enhancedContext', 'fundamentalContext', 'macro'],
      ['enhanced_context', 'fundamental_context', 'macro'],
    ],
  );
  const peerValuationSnapshot = extractSnapshotRecord(
    details,
    'peerValuationSnapshot',
    ['peerValuationSnapshot', 'peer_valuation_snapshot'],
    [
      ['enhancedContext', 'fundamentalContext', 'peerValuation'],
      ['enhanced_context', 'fundamental_context', 'peer_valuation'],
    ],
  );
  const newsContextSnapshot = extractNewsContextSnapshot(details);
  const searchContextSections = parseSearchContextSections(newsContextSnapshot);
  const sourceChainItems = [
    ...asRecordArray(readNestedRecord(isRecord(details?.contextSnapshot) ? details?.contextSnapshot : undefined, ['enhanced_context', 'fundamental_context', 'source_chain'])),
    ...asRecordArray(readNestedRecord(isRecord(details?.contextSnapshot) ? details?.contextSnapshot : undefined, ['enhancedContext', 'fundamentalContext', 'sourceChain'])),
    ...asRecordArray(pickValue(rawResult, 'sourceChain', 'source_chain')),
  ].slice(0, 6);
  const riskSummary = pickValue(rawResult, 'riskWarning', 'risk_warning', 'risk', 'riskAnalysis', 'risk_analysis');
  const dataCoverage = isRecord(readNestedRecord(isRecord(details?.contextSnapshot) ? details?.contextSnapshot : undefined, ['enhanced_context', 'fundamental_context', 'coverage']))
    ? readNestedRecord(isRecord(details?.contextSnapshot) ? details?.contextSnapshot : undefined, ['enhanced_context', 'fundamental_context', 'coverage'])
    : readNestedRecord(isRecord(details?.contextSnapshot) ? details?.contextSnapshot : undefined, ['enhancedContext', 'fundamentalContext', 'coverage']);
  const technicalRows = technicalSnapshot ? [
    ['EMA10', pickValue(technicalSnapshot, 'ema10'), '短线趋势与动量'],
    ['MA50', pickValue(technicalSnapshot, 'ma50'), '中期趋势参考'],
    ['MA200', pickValue(technicalSnapshot, 'ma200'), '长期趋势/牛熊分界'],
    ['Bollinger 中轨', pickValue(technicalSnapshot, 'bollMid', 'boll_mid'), '20 日均值'],
    ['Bollinger 上轨', pickValue(technicalSnapshot, 'bollUpper', 'boll_upper'), '波动上沿'],
    ['Bollinger 下轨', pickValue(technicalSnapshot, 'bollLower', 'boll_lower'), '波动下沿'],
    ['ATR14', pickValue(technicalSnapshot, 'atr14'), '近 14 日真实波幅'],
    ['VWMA20', pickValue(technicalSnapshot, 'vwma20'), '20 日成交量加权均价'],
    ['MFI14', pickValue(technicalSnapshot, 'mfi14'), '资金流指标，0-100'],
  ].filter(([, value]) => value !== undefined && value !== null && value !== '') : [];
  const peerRows = asRecordArray(pickValue(peerValuationSnapshot, 'rows'));
  const peerSummary = isRecord(pickValue(peerValuationSnapshot, 'summary'))
    ? pickValue(peerValuationSnapshot, 'summary') as UnknownRecord
    : undefined;
  const macroIndicators = asRecordArray(pickValue(macroSnapshot, 'indicators'));
  const aiDetailItems = [
    ['技术面综合', pickValue(rawResult, 'technicalAnalysis', 'technical_analysis')],
    ['均线与量能', [
      pickValue(rawResult, 'maAnalysis', 'ma_analysis'),
      pickValue(rawResult, 'volumeAnalysis', 'volume_analysis'),
    ].filter(Boolean).join('；')],
    ['基本面分析', pickValue(rawResult, 'fundamentalAnalysis', 'fundamental_analysis')],
    ['板块/行业位置', pickValue(rawResult, 'sectorPosition', 'sector_position')],
    ['公司亮点/风险', pickValue(rawResult, 'companyHighlights', 'company_highlights')],
    ['市场情绪', pickValue(rawResult, 'marketSentiment', 'market_sentiment')],
    ['核心看点', pickValue(rawResult, 'keyPoints', 'key_points')],
    ['风险提示', pickValue(rawResult, 'riskWarning', 'risk_warning')],
    ['买卖理由', pickValue(rawResult, 'buyReason', 'buy_reason')],
  ].filter(([, value]) => typeof value === 'string' && value.trim());
  const hasFinancialSection = Boolean(
    financialReport
    || dividendMetrics
    || filingReferences.length > 0
    || quarterlyTrendRows.length > 0
    || annualTrendRows.length > 0
  );
  const hasStructuredResearchSection = Boolean(
    aiDetailItems.length > 0
    || technicalRows.length > 0
    || peerRows.length > 0
    || macroIndicators.length > 0
    || searchContextSections.length > 0
    || newsContextSnapshot
    || sourceChainItems.length > 0
    || riskSummary
    || dataCoverage
  );

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

  if (!details?.rawResult && !details?.contextSnapshot && !recordId && !hasFinancialSection && !hasStructuredResearchSection) {
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
    <>
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

      {(riskSummary || sourceChainItems.length > 0 || dataCoverage) && (
        <div className="home-subpanel mb-3 px-3 py-3">
          <div className="mb-3">
            <h3 className="text-sm font-medium text-foreground">风险与数据来源透明度</h3>
            <p className="mt-0.5 text-xs text-muted-text">突出本次诊股的风险提示、数据覆盖和来源链路，便于判断报告可信度。</p>
          </div>
          <div className="grid gap-2 md:grid-cols-3">
            {riskSummary ? (
              <button
                type="button"
                onClick={() => setExpandedTextItem({ title: '风险提示', body: String(riskSummary), subtitle: '风险与数据来源透明度' })}
                className="rounded-md bg-rose-500/10 px-3 py-2.5 text-left transition-colors hover:bg-rose-500/15 focus:outline-none focus:ring-2 focus:ring-rose-400/40"
              >
                <div className="mb-1 text-xs font-medium text-rose-300">核心风险</div>
                <p className="line-clamp-4 text-sm leading-6 text-secondary-text">{truncateText(String(riskSummary), 180)}</p>
                <span className="mt-2 inline-flex text-[11px] text-rose-300/80">点击查看全文</span>
              </button>
            ) : null}
            {dataCoverage ? (
              <div className="rounded-md bg-white/5 px-3 py-2.5">
                <div className="mb-1 text-xs font-medium text-muted-text">数据覆盖</div>
                <div className="space-y-1 text-xs text-secondary-text">
                  {Object.entries(dataCoverage).slice(0, 6).map(([key, value]) => (
                    <div key={key} className="flex justify-between gap-3"><span>{key}</span><span>{displayValue(value)}</span></div>
                  ))}
                </div>
              </div>
            ) : null}
            {sourceChainItems.length > 0 ? (
              <div className="rounded-md bg-white/5 px-3 py-2.5">
                <div className="mb-1 text-xs font-medium text-muted-text">来源链路</div>
                <div className="space-y-1 text-xs text-secondary-text">
                  {sourceChainItems.map((item, index) => (
                    <div key={index} className="flex justify-between gap-3">
                      <span>{displayValue(pickValue(item, 'provider', 'source', 'name'), `source-${index + 1}`)}</span>
                      <span>{displayValue(pickValue(item, 'result', 'status'), '-')}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </div>
      )}

      {aiDetailItems.length > 0 && (
        <div className="home-subpanel mb-3 px-3 py-3">
          <div className="mb-3">
            <h3 className="text-sm font-medium text-foreground">AI 分析正文拆解</h3>
            <p className="mt-0.5 text-xs text-muted-text">这些内容来自本次 LLM 诊股结果，和完整 Markdown 报告保持同源。</p>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {aiDetailItems.map(([label, value]) => (
              <button
                key={String(label)}
                type="button"
                onClick={() => setExpandedTextItem({
                  title: String(label),
                  body: String(value),
                  subtitle: 'AI 分析正文拆解',
                })}
                className="rounded-md bg-white/5 px-3 py-2.5 text-left transition-colors hover:bg-white/10 focus:outline-none focus:ring-2 focus:ring-cyan/50"
                aria-label={`展开${String(label)}`}
              >
                <div className="mb-1 text-xs font-medium text-muted-text">{String(label)}</div>
                <p className="line-clamp-4 text-sm leading-6 text-secondary-text">{truncateText(String(value), 220)}</p>
                <span className="mt-2 inline-flex text-[11px] text-cyan/80">点击查看全文</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {hasFinancialSection && (
        <div className="home-subpanel mb-3 space-y-3 px-3 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-medium text-foreground">财报与原文</h3>
              <p className="mt-0.5 text-xs text-muted-text">结构化指标来自 SEC EDGAR / AkShare / Baostock 等可用数据源</p>
            </div>
            {pickString(financialReport, 'filingUrl') || pickString(financialReport, 'filing_url') ? (
              <a
                href={pickString(financialReport, 'filingUrl') || pickString(financialReport, 'filing_url')}
                target="_blank"
                rel="noreferrer"
                className="home-accent-link text-xs"
              >
                打开最新财报
              </a>
            ) : null}
          </div>

          {financialReport && (
            <div className="grid gap-2 text-xs sm:grid-cols-2">
              {[
                ['类型', pickValue(financialReport, 'form')],
                ['报告期', pickValue(financialReport, 'reportDate', 'report_date')],
                ['提交日期', pickValue(financialReport, 'filingDate', 'filing_date')],
                ['营业收入', pickValue(financialReport, 'revenue')],
                ['归母净利润', pickValue(financialReport, 'netProfitParent', 'net_profit_parent')],
                ['净利率', pickValue(financialReport, 'netMarginPct', 'net_margin_pct')],
                ['经营现金流', pickValue(financialReport, 'operatingCashFlow', 'operating_cash_flow')],
                ['自由现金流', pickValue(financialReport, 'freeCashFlow', 'free_cash_flow')],
                ['ROE', pickValue(financialReport, 'roe')],
                ['资产负债率', pickValue(financialReport, 'debtToAssetsPct', 'debt_to_assets_pct')],
                ['权益比率', pickValue(financialReport, 'equityRatioPct', 'equity_ratio_pct')],
                ['稀释 EPS', pickValue(financialReport, 'epsDiluted', 'eps_diluted')],
              ].map(([label, value]) => (
                <div key={String(label)} className="flex items-center justify-between gap-3 rounded-md bg-white/5 px-2.5 py-2">
                  <span className="text-muted-text">{String(label)}</span>
                  <span className="text-right text-foreground">{displayValue(value)}</span>
                </div>
              ))}
            </div>
          )}

          {financialExtraSections.length > 0 && (
            <div className="rounded-md bg-white/5 px-3 py-3">
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

          {dividendMetrics && (
            <div className="grid gap-2 text-xs sm:grid-cols-3">
              {[
                ['TTM 分红/股', pickString(dividendMetrics, 'ttmCashDividendPerShare') || pickString(dividendMetrics, 'ttm_cash_dividend_per_share')],
                ['TTM 股息率', pickString(dividendMetrics, 'ttmDividendYieldPct') || pickString(dividendMetrics, 'ttm_dividend_yield_pct')],
                ['分红事件数', pickString(dividendMetrics, 'ttmEventCount') || pickString(dividendMetrics, 'ttm_event_count')],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-md bg-white/5 px-2.5 py-2">
                  <div className="text-muted-text">{String(label)}</div>
                  <div className="mt-1 text-foreground">{displayValue(value)}</div>
                </div>
              ))}
            </div>
          )}

          {quarterlyTrendRows.length > 0 && (
            <div className="rounded-md bg-white/5 px-3 py-3">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h4 className="text-xs font-medium text-foreground">最近季度财务趋势</h4>
                  <p className="mt-0.5 text-[11px] text-muted-text">
                    较前值为环比变化，YoY 为同比变化；缺数据表示数据源未提供该季度可比口径，带 * 的期间为累计值拆分或年报减 YTD 推算。
                  </p>
                </div>
                <MiniTrendLine rows={quarterlyTrendRows} metric="revenueValue" snakeMetric="revenue_value" label="收入趋势" />
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[56rem] text-left text-xs">
                  <thead className="text-muted-text">
                    <tr className="border-b border-subtle">
                      <th className="py-2 pr-3 font-medium">期间</th>
                      <th className="py-2 pr-3 font-medium">收入</th>
                      <th className="py-2 pr-3 font-medium">较前值</th>
                      <th className="py-2 pr-3 font-medium">YoY</th>
                      <th className="py-2 pr-3 font-medium">净利润</th>
                      <th className="py-2 pr-3 font-medium">较前值</th>
                      <th className="py-2 pr-3 font-medium">YoY</th>
                      <th className="py-2 pr-3 font-medium">净利率</th>
                      <th className="py-2 pr-3 font-medium">FCF</th>
                      <th className="py-2 font-medium">EPS</th>
                    </tr>
                  </thead>
                  <tbody>
                    {quarterlyTrendRows.map((row, index) => (
                      <tr key={`${displayValue(pickValue(row, 'period'))}-${index}`} className="border-b border-subtle last:border-b-0">
                        <td className="py-2 pr-3 text-foreground">
                          {displayValue(pickValue(row, 'period'))}{pickValue(row, 'derived') ? '*' : ''}
                        </td>
                        <td className="py-2 pr-3 text-secondary-text">{displayValue(pickValue(row, 'revenue'), '缺数据')}</td>
                        <td className="py-2 pr-3 text-secondary-text">{formatSignedPct(pickValue(row, 'revenueValueChangePct', 'revenue_value_change_pct'))}</td>
                        <td className="py-2 pr-3 text-secondary-text">{formatSignedPct(pickValue(row, 'revenueValueYoyPct', 'revenue_value_yoy_pct'))}</td>
                        <td className="py-2 pr-3 text-secondary-text">{displayValue(pickValue(row, 'netProfitParent', 'net_profit_parent'), '缺数据')}</td>
                        <td className="py-2 pr-3 text-secondary-text">{formatSignedPct(pickValue(row, 'netProfitParentValueChangePct', 'net_profit_parent_value_change_pct'))}</td>
                        <td className="py-2 pr-3 text-secondary-text">{formatSignedPct(pickValue(row, 'netProfitParentValueYoyPct', 'net_profit_parent_value_yoy_pct'))}</td>
                        <td className="py-2 pr-3 text-secondary-text">{formatSignedPct(pickValue(row, 'netMarginPct', 'net_margin_pct'))}</td>
                        <td className="py-2 pr-3 text-secondary-text">{displayValue(pickValue(row, 'freeCashFlow', 'free_cash_flow'), '缺数据')}</td>
                        <td className="py-2 text-secondary-text">{displayValue(pickValue(row, 'epsDiluted', 'eps_diluted'), '缺数据')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {annualTrendRows.length > 0 && (
            <div className="rounded-md bg-white/5 px-3 py-3">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h4 className="text-xs font-medium text-foreground">最近年度财务趋势</h4>
                  <p className="mt-0.5 text-[11px] text-muted-text">用于观察完整年度收入、利润与现金流周期。</p>
                </div>
                <MiniTrendLine rows={annualTrendRows} metric="netProfitParentValue" snakeMetric="net_profit_parent_value" label="净利润趋势" />
              </div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {annualTrendRows.map((row, index) => (
                  <div key={`${displayValue(pickValue(row, 'period'))}-${index}`} className="rounded-md border border-subtle px-3 py-2 text-xs">
                    <div className="mb-1 font-medium text-foreground">{displayValue(pickValue(row, 'period'))}</div>
                    <div className="space-y-1 text-secondary-text">
                      <div>收入：{displayValue(pickValue(row, 'revenue'))}（{formatSignedPct(pickValue(row, 'revenueValueChangePct', 'revenue_value_change_pct'))}）</div>
                      <div>净利润：{displayValue(pickValue(row, 'netProfitParent', 'net_profit_parent'))}（{formatSignedPct(pickValue(row, 'netProfitParentValueChangePct', 'net_profit_parent_value_change_pct'))}）</div>
                      <div>FCF：{displayValue(pickValue(row, 'freeCashFlow', 'free_cash_flow'))}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {filingReferences.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[34rem] text-left text-xs">
                <thead className="text-muted-text">
                  <tr className="border-b border-subtle">
                    <th className="py-2 pr-3 font-medium">类型</th>
                    <th className="py-2 pr-3 font-medium">报告期</th>
                    <th className="py-2 pr-3 font-medium">提交日期</th>
                    <th className="py-2 font-medium">链接</th>
                  </tr>
                </thead>
                <tbody>
                  {filingReferences.map((filing, index) => (
                    <tr key={`${filing.form || 'filing'}-${filing.reportDate || index}`} className="border-b border-subtle last:border-b-0">
                      <td className="py-2 pr-3 text-foreground">{displayValue(filing.form)}</td>
                      <td className="py-2 pr-3 text-secondary-text">{displayValue(filing.reportDate)}</td>
                      <td className="py-2 pr-3 text-secondary-text">{displayValue(filing.filingDate)}</td>
                      <td className="py-2">
                        {filing.url ? (
                          <a href={filing.url} target="_blank" rel="noreferrer" className="home-accent-link">
                            原文
                          </a>
                        ) : (
                          <span className="text-muted-text">N/A</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {peerRows.length > 0 && (
        <div className="home-subpanel mb-3 space-y-3 px-3 py-3">
          <div>
            <h3 className="text-sm font-medium text-foreground">同类型估值对比</h3>
            <p className="mt-0.5 text-xs text-muted-text">
              {displayValue(pickValue(peerValuationSnapshot, 'comparisonBasis', 'comparison_basis'))}
            </p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[36rem] text-left text-xs">
              <thead className="text-muted-text">
                <tr className="border-b border-subtle">
                  <th className="py-2 pr-3 font-medium">公司</th>
                  <th className="py-2 pr-3 font-medium">当前价</th>
                  <th className="py-2 pr-3 font-medium">PE</th>
                  <th className="py-2 pr-3 font-medium">PB</th>
                  <th className="py-2 font-medium">市值</th>
                </tr>
              </thead>
              <tbody>
                {peerRows.slice(0, 6).map((row, index) => {
                  const isTarget = Boolean(pickValue(row, 'isTarget', 'is_target'));
                  return (
                    <tr key={`${displayValue(pickValue(row, 'symbol'))}-${index}`} className="border-b border-subtle last:border-b-0">
                      <td className={`py-2 pr-3 ${isTarget ? 'font-semibold text-foreground' : 'text-secondary-text'}`}>
                        {displayValue(pickValue(row, 'symbol'))} {displayValue(pickValue(row, 'name')) !== 'N/A' ? displayValue(pickValue(row, 'name')) : ''}
                      </td>
                      <td className="py-2 pr-3 text-secondary-text">{formatNumber(pickValue(row, 'price'))}</td>
                      <td className="py-2 pr-3 text-secondary-text">{formatNumber(pickValue(row, 'peRatio', 'pe_ratio'))}</td>
                      <td className="py-2 pr-3 text-secondary-text">{formatNumber(pickValue(row, 'pbRatio', 'pb_ratio'))}</td>
                      <td className="py-2 text-secondary-text">{displayValue(pickValue(row, 'marketCapText', 'market_cap_text'))}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {peerSummary && (
            <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
              {[
                ['Peer PE 中位数', formatNumber(pickValue(peerSummary, 'peerMedianPeRatio', 'peer_median_pe_ratio'))],
                ['Peer PB 中位数', formatNumber(pickValue(peerSummary, 'peerMedianPbRatio', 'peer_median_pb_ratio'))],
                ['标的 PE 相对中位数', formatSignedPct(pickValue(peerSummary, 'peRatioVsPeerMedianPct', 'pe_ratio_vs_peer_median_pct'))],
                ['标的 PB 相对中位数', formatSignedPct(pickValue(peerSummary, 'pbRatioVsPeerMedianPct', 'pb_ratio_vs_peer_median_pct'))],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-md bg-white/5 px-2.5 py-2">
                  <div className="text-muted-text">{String(label)}</div>
                  <div className="mt-1 text-foreground">{value}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {macroIndicators.length > 0 && (
        <div className="home-subpanel mb-3 px-3 py-3">
          <div className="mb-3">
            <h3 className="text-sm font-medium text-foreground">FRED 宏观指标</h3>
            <p className="mt-0.5 text-xs text-muted-text">用于判断利率、通胀和就业背景对估值折现率的影响。</p>
          </div>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {macroIndicators.map((item, index) => (
              <div key={`${displayValue(pickValue(item, 'seriesId', 'series_id'))}-${index}`} className="rounded-md bg-white/5 px-3 py-2 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-foreground">{displayValue(pickValue(item, 'label', 'seriesId', 'series_id'))}</span>
                  <span className="text-muted-text">{displayValue(pickValue(item, 'date'))}</span>
                </div>
                <div className="mt-1 text-lg font-semibold text-foreground">
                  {displayValue(pickValue(item, 'value'))}{displayValue(pickValue(item, 'unit')) !== 'N/A' ? ` ${displayValue(pickValue(item, 'unit'))}` : ''}
                </div>
                {pickValue(item, 'note') ? (
                  <p className="mt-1 leading-5 text-muted-text">{truncateText(String(pickValue(item, 'note')), 120)}</p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      )}

      {technicalRows.length > 0 && (
        <div className="home-subpanel mb-3 px-3 py-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-medium text-foreground">扩展技术指标</h3>
              <p className="mt-0.5 text-xs text-muted-text">补充趋势、波动率和资金流读数，辅助判断追高/回撤风险。</p>
            </div>
            <div className="text-xs text-muted-text">
              来源：{displayValue(pickValue(technicalSnapshot, 'source'))}
            </div>
          </div>
          <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
            {technicalRows.map(([label, value, note]) => (
              <div key={String(label)} className="rounded-md bg-white/5 px-2.5 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-text">{String(label)}</span>
                  <span className="font-mono text-foreground">{formatNumber(value)}</span>
                </div>
                <div className="mt-1 text-[11px] leading-4 text-muted-text">{String(note)}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {(searchContextSections.length > 0 || newsContextSnapshot) && (
        <div className="home-subpanel mb-3 px-3 py-3">
          <div className="mb-3">
            <h3 className="text-sm font-medium text-foreground">搜索情报摘要</h3>
            <p className="mt-0.5 text-xs text-muted-text">按搜索维度展示核心命中，便于追溯 AI 看到的外部信息。</p>
          </div>
          {searchContextSections.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[56rem] text-left text-xs">
                <thead className="text-muted-text">
                  <tr className="border-b border-subtle">
                    <th className="w-40 py-2 pr-4 font-medium">模块</th>
                    <th className="w-32 py-2 pr-4 font-medium">来源</th>
                    <th className="py-2 font-medium">核心命中</th>
                  </tr>
                </thead>
                <tbody>
                  {searchContextSections.map((section) => (
                    <tr key={`${section.title}-${section.source}`} className="border-b border-subtle last:border-b-0">
                      <td className="whitespace-nowrap py-2 pr-4 text-foreground">{section.title}</td>
                      <td className="whitespace-nowrap py-2 pr-4 text-secondary-text">{section.source}</td>
                      <td className="py-2 text-secondary-text">
                        <ol className="list-decimal space-y-1 pl-4">
                          {section.items.map((item, index) => (
                            <li key={`${section.title}-${index}`}>{truncateText(item, 180)}</li>
                          ))}
                        </ol>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <pre className="home-trace-pre home-trace-pre-content max-h-56 overflow-auto rounded-lg bg-base p-3 text-left text-xs text-foreground">
              {truncateText(newsContextSnapshot, 1600)}
            </pre>
          )}
        </div>
      )}

      {/* 折叠区域 */}
      <div className="space-y-2">
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
    <Drawer
      isOpen={expandedTextItem !== null}
      onClose={() => setExpandedTextItem(null)}
      title={expandedTextItem?.title || ''}
      width="max-w-2xl"
      zIndex={70}
    >
      {expandedTextItem ? (
        <div className="text-left">
          {expandedTextItem.subtitle ? (
            <div className="label-uppercase mb-3 text-muted-text">{expandedTextItem.subtitle}</div>
          ) : null}
          <p className="whitespace-pre-wrap text-sm leading-8 text-secondary-text">
            {expandedTextItem.body}
          </p>
        </div>
      ) : null}
    </Drawer>
    </>
  );
};
