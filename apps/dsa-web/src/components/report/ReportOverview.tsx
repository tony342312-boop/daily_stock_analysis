import type React from 'react';
import { useState } from 'react';
import type {
  ReportDetails as ReportDetailsType,
  ReportMeta,
  ReportSummary as ReportSummaryType,
} from '../../types/analysis';
import { Badge, Card, Drawer, ScoreGauge } from '../common';
import { formatDateTime } from '../../utils/format';
import { getReportText, normalizeReportLanguage } from '../../utils/reportLanguage';

interface ReportOverviewProps {
  meta: ReportMeta;
  summary: ReportSummaryType;
  details?: ReportDetailsType;
  isHistory?: boolean;
}

type BoardStatus = 'leading' | 'lagging';

type BoardSignal = {
  status: BoardStatus;
  changePct?: number;
};

type UnknownRecord = Record<string, unknown>;

type ScoreDimension = {
  key: string;
  label: string;
  score: number;
  weight?: number;
  evidence?: string;
};

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

const normalizeBoardName = (value?: string): string =>
  (value || '').trim().replace(/\s+/g, ' ');

const coerceFiniteNumber = (value: unknown): number | undefined => {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : undefined;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim().replace(/%$/, '');
    if (!trimmed) {
      return undefined;
    }
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
};

const buildBoardSignalMap = (details?: ReportDetailsType): Map<string, BoardSignal> => {
  const signalMap = new Map<string, BoardSignal>();
  const topBoards = Array.isArray(details?.sectorRankings?.top) ? details.sectorRankings.top : [];
  const bottomBoards = Array.isArray(details?.sectorRankings?.bottom) ? details.sectorRankings.bottom : [];

  topBoards.forEach((item) => {
    const normalizedName = normalizeBoardName(item?.name);
    if (!normalizedName) {
      return;
    }
    signalMap.set(normalizedName, {
      status: 'leading',
      changePct: coerceFiniteNumber(item.changePct),
    });
  });

  bottomBoards.forEach((item) => {
    const normalizedName = normalizeBoardName(item?.name);
    if (!normalizedName) {
      return;
    }
    signalMap.set(normalizedName, {
      status: 'lagging',
      changePct: coerceFiniteNumber(item.changePct),
    });
  });

  return signalMap;
};

const SCORE_DIMENSION_ORDER: Array<[string, string, string]> = [
  ['technical', 'technical', '技术面'],
  ['fundamental', 'fundamental', '基本面'],
  ['valuation', 'valuation', '估值'],
  ['newsSentiment', 'news_sentiment', '新闻/情绪'],
  ['macroRisk', 'macro_risk', '宏观/风险'],
];

const getDashboardRecord = (details?: ReportDetailsType): UnknownRecord | undefined => {
  const rawResult = isRecord(details?.rawResult) ? details?.rawResult : undefined;
  const dashboard = pickValue(rawResult, 'dashboard');
  return isRecord(dashboard) ? dashboard : undefined;
};

const getScorecardRecord = (details?: ReportDetailsType): UnknownRecord | undefined => {
  const dashboard = getDashboardRecord(details);
  const scorecard = pickValue(dashboard, 'scorecard');
  return isRecord(scorecard) ? scorecard : undefined;
};

const getScoreDimensions = (details?: ReportDetailsType): ScoreDimension[] => {
  const scorecard = getScorecardRecord(details);
  const dimensions = pickValue(scorecard, 'dimensions');
  const dimensionRecord = isRecord(dimensions) ? dimensions : undefined;
  return SCORE_DIMENSION_ORDER.flatMap(([camelKey, snakeKey, fallbackLabel]) => {
    const rawDimension = pickValue(dimensionRecord, camelKey, snakeKey);
    if (!isRecord(rawDimension)) return [];
    const score = coerceFiniteNumber(pickValue(rawDimension, 'score'));
    if (score === undefined) return [];
    return [{
      key: camelKey,
      label: String(pickValue(rawDimension, 'label') || fallbackLabel),
      score: Math.max(0, Math.min(100, score)),
      weight: coerceFiniteNumber(pickValue(rawDimension, 'weight')),
      evidence: String(pickValue(rawDimension, 'evidence') || ''),
    }];
  });
};

/**
 * 报告概览区组件 - 终端风格
 */
export const ReportOverview: React.FC<ReportOverviewProps> = ({
  meta,
  summary,
  details,
}) => {
  const reportLanguage = normalizeReportLanguage(meta.reportLanguage);
  const text = getReportText(reportLanguage);
  const relatedBoards = (Array.isArray(details?.belongBoards) ? details.belongBoards : [])
    .filter((board) => normalizeBoardName(board?.name).length > 0)
    .slice(0, 3);
  const boardSignals = buildBoardSignalMap(details);
  const scorecard = getScorecardRecord(details);
  const scoreDimensions = getScoreDimensions(details);
  const scorecardOverall = coerceFiniteNumber(pickValue(scorecard, 'overallScore', 'overall_score'));
  const overallScore = scorecardOverall ?? summary.sentimentScore;
  const [activeScoreDimension, setActiveScoreDimension] = useState<ScoreDimension | null>(null);
  const scorecardFallbackNote = reportLanguage === 'en'
    ? 'This older report has no saved dimension breakdown. Re-run the analysis to show technical, fundamental, valuation, news, and macro scores.'
    : '这条历史报告未保存维度拆分；重新分析后会显示技术面、基本面、估值、新闻/情绪、宏观/风险分项。';
  const scoreDimensionHint = reportLanguage === 'en' ? 'Click to expand' : '点击展开';

  const getPriceChangeStyle = (changePct: number | undefined): React.CSSProperties | undefined => {
    if (changePct === undefined || changePct === null) {
      return undefined;
    }

    if (changePct > 0) {
      return { color: 'var(--home-price-up)' };
    }

    if (changePct < 0) {
      return { color: 'var(--home-price-down)' };
    }

    return undefined;
  };

  const formatChangePct = (changePct: number | undefined): string => {
    if (changePct === undefined || changePct === null) return '--';
    const sign = changePct > 0 ? '+' : '';
    return `${sign}${changePct.toFixed(2)}%`;
  };

  const getBoardStatusLabel = (status: BoardStatus): string => {
    if (status === 'leading') {
      return text.leadingBoard;
    }
    return text.laggingBoard;
  };

  const getBoardStatusVariant = (status: BoardStatus): 'success' | 'danger' => {
    if (status === 'leading') {
      return 'success';
    }
    return 'danger';
  };

  return (
    <>
    <div className="space-y-5">
      {/* 主信息区 - 两列布局，items-stretch 确保右侧与左侧同高 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 items-stretch">
        {/* 左侧：股票信息与结论 */}
        <div className="lg:col-span-2 space-y-5">
          {/* 股票头部 */}
          <Card variant="gradient" padding="md" className="home-report-hero">
            <div className="flex items-start justify-between mb-5">
              <div className="flex-1">
                <div className="flex items-center gap-3">
                  <h2 className="text-[28px] font-bold leading-tight text-foreground">
                    {meta.stockName || meta.stockCode}
                  </h2>
                  {/* 价格和涨跌幅 */}
                  {meta.currentPrice != null && (
                    <div className="flex items-baseline gap-2">
                      <span className="text-xl font-bold font-mono" style={getPriceChangeStyle(meta.changePct)}>
                        {meta.currentPrice.toFixed(2)}
                      </span>
                      <span className="text-sm font-semibold font-mono" style={getPriceChangeStyle(meta.changePct)}>
                        {formatChangePct(meta.changePct)}
                      </span>
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2 mt-1.5">
                  <span className="home-accent-chip px-2 py-0.5 font-mono text-xs">
                    {meta.stockCode}
                  </span>
                  <span className="text-xs text-muted-text flex items-center gap-1">
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                    </svg>
                    {formatDateTime(meta.createdAt)}
                  </span>
                </div>
              </div>
            </div>

            {/* 关键结论 */}
            <div className="home-divider border-t pt-5">
              <span className="label-uppercase">{text.keyInsights}</span>
              <p className="mt-2 max-w-[62ch] whitespace-pre-wrap text-left text-[15px] leading-7 text-foreground">
                {summary.analysisSummary || text.noAnalysisSummary}
              </p>
            </div>
          </Card>

          {/* 操作建议和趋势预测 */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* 操作建议 */}
            <Card
              variant="bordered"
              padding="sm"
              hoverable
              className="home-panel-card home-insight-card"
              style={{ ['--home-insight-tone' as string]: 'var(--home-strategy-buy)' }}
            >
              <div className="flex items-start gap-3">
                <div className="home-insight-icon w-8 h-8 rounded-lg bg-success/10 flex items-center justify-center flex-shrink-0">
                  <svg className="w-4 h-4 text-success" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
                  </svg>
                </div>
                <div className="space-y-1.5">
                  <h4 className="home-insight-title text-[11px] font-medium uppercase tracking-[0.16em]">{text.actionAdvice}</h4>
                  <p className="home-insight-body text-sm leading-6">
                    {summary.operationAdvice || text.noAdvice}
                  </p>
                </div>
              </div>
            </Card>

            {/* 趋势预测 */}
            <Card
              variant="bordered"
              padding="sm"
              hoverable
              className="home-panel-card home-insight-card"
              style={{ ['--home-insight-tone' as string]: 'var(--home-strategy-take)' }}
            >
              <div className="flex items-start gap-3">
                <div className="home-insight-icon w-8 h-8 rounded-lg bg-warning/10 flex items-center justify-center flex-shrink-0">
                  <svg className="w-4 h-4 text-warning" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
                  </svg>
                </div>
                <div className="space-y-1.5">
                  <h4 className="home-insight-title text-[11px] font-medium uppercase tracking-[0.16em]">{text.trendPrediction}</h4>
                  <p className="home-insight-body text-sm leading-6">
                    {summary.trendPrediction || text.noPrediction}
                  </p>
                </div>
              </div>
            </Card>
          </div>

          {relatedBoards.length > 0 && (
            <Card variant="bordered" padding="sm" className="home-panel-card text-left">
              <div className="mb-3 flex items-baseline gap-2">
                <span className="label-uppercase">{text.boardLinkage}</span>
                <h3 className="mt-0.5 text-base font-semibold text-foreground">{text.relatedBoards}</h3>
              </div>

              <div className="space-y-2.5">
                {relatedBoards.map((board, index) => {
                  const boardName = normalizeBoardName(board.name);
                  const signal = boardSignals.get(boardName);
                  return (
                    <div
                      key={`${boardName}-${board.code || index}`}
                      className="flex flex-wrap items-center gap-2 text-sm"
                    >
                      <span className="home-accent-chip px-2 py-0.5 text-xs font-medium">
                        {boardName}
                      </span>
                      {board.type && (
                        <span className="home-board-pill rounded-full px-2 py-0.5 text-xs">
                          {board.type}
                        </span>
                      )}
                      {signal && (
                        <Badge
                          variant={getBoardStatusVariant(signal.status)}
                          className="home-board-status-badge shadow-none"
                        >
                          {getBoardStatusLabel(signal.status)}
                        </Badge>
                      )}
                      {signal && signal.changePct !== undefined && signal.changePct !== null && (
                        <span
                          className="text-xs font-mono"
                          style={getPriceChangeStyle(signal.changePct)}
                        >
                          {formatChangePct(signal.changePct)}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </Card>
          )}
        </div>

        {/* 右侧：综合评分 - 填满格子高度，消除与 STRATEGY POINTS 之间的空隙 */}
        <div className="flex flex-col self-stretch min-h-full">
          <Card variant="bordered" padding="md" className="home-panel-card home-rail-card !overflow-visible flex-1 flex flex-col min-h-0">
            <div className="flex flex-1 flex-col justify-center">
              <div className="text-center">
                <h3 className="mb-5 text-sm font-medium tracking-wide text-foreground">
                  {text.overallScore}
                </h3>
                <ScoreGauge score={overallScore} size="lg" language={reportLanguage} />
              </div>
              {scoreDimensions.length > 0 && (
                <div className="mt-5 space-y-2.5 text-left">
                  <div className="label-uppercase text-muted-text">{text.scoreBreakdown}</div>
                  {scoreDimensions.map((dimension) => (
                    <button
                      key={dimension.key}
                      type="button"
                      onClick={() => setActiveScoreDimension(dimension)}
                      className="w-full rounded-md bg-white/5 px-2.5 py-2 text-left transition-colors hover:bg-white/10 focus:outline-none focus:ring-2 focus:ring-cyan/50"
                      aria-label={`${dimension.label} ${scoreDimensionHint}`}
                    >
                      <div className="mb-1.5 flex items-center justify-between gap-2 text-xs">
                        <span className="font-medium text-secondary-text">{dimension.label}</span>
                        <span className="font-mono text-foreground">
                          {Math.round(dimension.score)}
                          {dimension.weight !== undefined ? <span className="text-muted-text"> · {dimension.weight}%</span> : null}
                        </span>
                      </div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-white/10">
                        <div
                          className="h-full rounded-full bg-cyan transition-[width] duration-500"
                          style={{ width: `${dimension.score}%` }}
                        />
                      </div>
                      {dimension.evidence ? (
                        <p className="mt-1.5 line-clamp-2 text-[11px] leading-4 text-muted-text">
                          {dimension.evidence}
                        </p>
                      ) : null}
                      <span className="mt-1.5 inline-flex text-[10px] text-cyan/80">{scoreDimensionHint}</span>
                    </button>
                  ))}
                </div>
              )}
              {scoreDimensions.length === 0 && (
                <p className="mx-auto mt-4 max-w-[24ch] text-center text-xs leading-5 text-muted-text">
                  {scorecardFallbackNote}
                </p>
              )}
            </div>
          </Card>
        </div>
      </div>
    </div>
    <Drawer
      isOpen={activeScoreDimension !== null}
      onClose={() => setActiveScoreDimension(null)}
      title={activeScoreDimension?.label || text.scoreBreakdown}
      width="max-w-xl"
      zIndex={70}
    >
      {activeScoreDimension ? (
        <div className="space-y-5 text-left">
          <div className="rounded-lg border border-border/70 bg-white/5 p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="text-sm font-medium text-secondary-text">{activeScoreDimension.label}</span>
              <span className="font-mono text-xl font-semibold text-foreground">
                {Math.round(activeScoreDimension.score)}
                {activeScoreDimension.weight !== undefined ? <span className="ml-2 text-sm text-muted-text">/ {activeScoreDimension.weight}%</span> : null}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-white/10">
              <div
                className="h-full rounded-full bg-cyan"
                style={{ width: `${activeScoreDimension.score}%` }}
              />
            </div>
          </div>
          <div>
            <div className="label-uppercase mb-2 text-muted-text">{reportLanguage === 'en' ? 'Evidence' : '评分依据'}</div>
            <p className="whitespace-pre-wrap text-sm leading-7 text-secondary-text">
              {activeScoreDimension.evidence || (reportLanguage === 'en' ? 'No evidence text available.' : '暂无详细依据。')}
            </p>
          </div>
        </div>
      ) : null}
    </Drawer>
    </>
  );
};
