import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { feedbackApi, type FeedbackCategory } from '../api/feedback';
import { ApiErrorAlert, ConfirmDialog, Button, EmptyState, InlineAlert } from '../components/common';
import { DashboardStateBlock } from '../components/dashboard';
import { StockAutocomplete } from '../components/StockAutocomplete';
import { HistoryList } from '../components/history';
import { ReportMarkdown, ReportSummary } from '../components/report';
import { TaskPanel } from '../components/tasks';
import { useDashboardLifecycle, useHomeDashboardState } from '../hooks';
import { getReportText, normalizeReportLanguage } from '../utils/reportLanguage';

const HomePage: React.FC = () => {
  const navigate = useNavigate();
  const { currentUser } = useAuth();
  const isAdmin = currentUser?.role === 'admin';
  const historyScope = useMemo(() => ({ allUsers: isAdmin }), [isAdmin]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackCategory, setFeedbackCategory] = useState<FeedbackCategory>('bug');
  const [feedbackCategoryOpen, setFeedbackCategoryOpen] = useState(false);
  const [feedbackContent, setFeedbackContent] = useState('');
  const [feedbackContact, setFeedbackContact] = useState('');
  const [feedbackStatus, setFeedbackStatus] = useState<{ type: 'success' | 'warning' | 'danger'; message: string } | null>(null);
  const [isSubmittingFeedback, setIsSubmittingFeedback] = useState(false);

  const {
    query,
    inputError,
    duplicateError,
    error,
    isAnalyzing,
    historyItems,
    historyRetentionDays,
    historyAutoCleanupEnabled,
    selectedHistoryIds,
    isDeletingHistory,
    isLoadingHistory,
    isLoadingMore,
    hasMore,
    selectedReport,
    isLoadingReport,
    activeTasks,
    markdownDrawerOpen,
    setQuery,
    clearError,
    loadInitialHistory,
    refreshHistory,
    loadMoreHistory,
    selectHistoryItem,
    toggleHistorySelection,
    toggleSelectAllVisible,
    deleteSelectedHistory,
    submitAnalysis,
    notify,
    setNotify,
    syncTaskCreated,
    syncTaskUpdated,
    syncTaskFailed,
    removeTask,
    openMarkdownDrawer,
    closeMarkdownDrawer,
    selectedIds,
  } = useHomeDashboardState();

  useEffect(() => {
    document.title = '每日选股分析 - DSA';
  }, []);
  const reportLanguage = normalizeReportLanguage(selectedReport?.meta.reportLanguage);
  const reportText = getReportText(reportLanguage);

  const loadInitialHistoryForScope = useCallback(() => loadInitialHistory(historyScope), [historyScope, loadInitialHistory]);
  const refreshHistoryForScope = useCallback((silent?: boolean) => refreshHistory(silent, historyScope), [historyScope, refreshHistory]);

  useDashboardLifecycle({
    loadInitialHistory: loadInitialHistoryForScope,
    refreshHistory: refreshHistoryForScope,
    syncTaskCreated,
    syncTaskUpdated,
    syncTaskFailed,
    removeTask,
  });

  const handleHistoryItemClick = useCallback((recordId: number) => {
    void selectHistoryItem(recordId);
    setSidebarOpen(false);
  }, [selectHistoryItem]);

  const handleSubmitAnalysis = useCallback(
    (
      stockCode?: string,
      stockName?: string,
      selectionSource?: 'manual' | 'autocomplete' | 'import' | 'image',
    ) => {
      void submitAnalysis({
        stockCode,
        stockName,
        originalQuery: query,
        selectionSource: selectionSource ?? 'manual',
      });
    },
    [query, submitAnalysis],
  );

  const handleAskFollowUp = useCallback(() => {
    if (selectedReport?.meta.id === undefined) {
      return;
    }

    const code = selectedReport.meta.stockCode;
    const name = selectedReport.meta.stockName;
    const rid = selectedReport.meta.id;
    navigate(`/chat?stock=${encodeURIComponent(code)}&name=${encodeURIComponent(name)}&recordId=${rid}`);
  }, [navigate, selectedReport]);

  const handleReanalyze = useCallback(() => {
    if (!selectedReport) {
      return;
    }

    void submitAnalysis({
      stockCode: selectedReport.meta.stockCode,
      stockName: selectedReport.meta.stockName,
      originalQuery: selectedReport.meta.stockCode,
      selectionSource: 'manual',
      forceRefresh: true,
    });
  }, [selectedReport, submitAnalysis]);

  const handleDeleteSelectedHistory = useCallback(() => {
    void deleteSelectedHistory();
    setShowDeleteConfirm(false);
  }, [deleteSelectedHistory]);

  const handleOpenFeedback = useCallback(() => {
    setFeedbackStatus(null);
    setFeedbackCategoryOpen(false);
    setFeedbackOpen(true);
  }, []);

  const handleCloseFeedback = useCallback(() => {
    if (isSubmittingFeedback) {
      return;
    }
    setFeedbackCategoryOpen(false);
    setFeedbackOpen(false);
  }, [isSubmittingFeedback]);

  const handleSubmitFeedback = useCallback(async () => {
    const content = feedbackContent.trim();
    if (!content) {
      setFeedbackStatus({ type: 'danger', message: '请先填写问题描述。' });
      return;
    }

    setIsSubmittingFeedback(true);
    setFeedbackStatus(null);
    try {
      await feedbackApi.submit({
        category: feedbackCategory,
        content,
        contact: feedbackContact.trim() || undefined,
        pageUrl: window.location.href,
      });
      setFeedbackContent('');
      setFeedbackContact('');
      setFeedbackStatus({
        type: 'success',
        message: '已接收，反馈已提交。',
      });
    } catch {
      setFeedbackStatus({ type: 'danger', message: '反馈提交失败，请稍后重试。' });
    } finally {
      setIsSubmittingFeedback(false);
    }
  }, [feedbackCategory, feedbackContact, feedbackContent]);

  const sidebarContent = useMemo(
    () => (
      <div className="flex min-h-0 h-full flex-col gap-3 overflow-hidden">
        <TaskPanel tasks={activeTasks} />
        <HistoryList
          items={historyItems}
          isLoading={isLoadingHistory}
          isLoadingMore={isLoadingMore}
          hasMore={hasMore}
          selectedId={selectedReport?.meta.id}
          selectedIds={selectedIds}
          isDeleting={isDeletingHistory}
          onItemClick={handleHistoryItemClick}
          isAdminView={isAdmin}
          retentionDays={historyRetentionDays}
          autoCleanupEnabled={historyAutoCleanupEnabled}
          onLoadMore={() => void loadMoreHistory(historyScope)}
          onToggleItemSelection={toggleHistorySelection}
          onToggleSelectAll={toggleSelectAllVisible}
          onDeleteSelected={() => setShowDeleteConfirm(true)}
          className="flex-1 overflow-hidden"
        />
      </div>
    ),
    [
      activeTasks,
      hasMore,
      historyItems,
      historyRetentionDays,
      historyAutoCleanupEnabled,
      isDeletingHistory,
      isLoadingHistory,
      isLoadingMore,
      handleHistoryItemClick,
      isAdmin,
      historyScope,
      loadMoreHistory,
      selectedIds,
      selectedReport?.meta.id,
      toggleHistorySelection,
      toggleSelectAllVisible,
    ],
  );

  return (
    <div
      data-testid="home-dashboard"
      className="flex h-[calc(100vh-5rem)] w-full flex-col overflow-hidden md:flex-row sm:h-[calc(100vh-5.5rem)] lg:h-[calc(100vh-2rem)]"
    >
      <div className="flex-1 flex flex-col min-h-0 min-w-0 max-w-full lg:max-w-6xl mx-auto w-full">
        <header className="flex min-w-0 flex-shrink-0 items-center overflow-hidden px-3 py-3 md:px-4 md:py-4">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2.5 md:flex-nowrap">
            <button
              onClick={() => setSidebarOpen(true)}
              className="md:hidden -ml-1 flex-shrink-0 rounded-lg p-1.5 text-secondary-text transition-colors hover:bg-hover hover:text-foreground"
              aria-label="历史记录"
            >
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
            <div className="relative min-w-0 flex-1">
              <StockAutocomplete
                value={query}
                onChange={setQuery}
                onSubmit={(stockCode, stockName, selectionSource) => {
                  handleSubmitAnalysis(stockCode, stockName, selectionSource);
                }}
                placeholder="输入股票代码或名称，如 600519、贵州茅台、AAPL"
                disabled={isAnalyzing}
                className={inputError ? 'border-danger/50' : undefined}
              />
            </div>
            <label className="flex h-10 flex-shrink-0 cursor-pointer items-center gap-1.5 rounded-xl border border-subtle bg-surface/60 px-3 text-xs text-secondary-text select-none transition-colors hover:border-subtle-hover hover:text-foreground">
              <input
                type="checkbox"
                checked={notify}
                onChange={(e) => setNotify(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-border accent-primary"
              />
              推送通知
            </label>
            <button
              type="button"
              onClick={handleOpenFeedback}
              className="flex h-10 flex-shrink-0 items-center gap-1.5 whitespace-nowrap rounded-xl border border-amber-400/30 bg-amber-400/10 px-3 text-xs font-medium text-amber-200 transition-colors hover:bg-amber-400/15"
            >
              反馈问题
            </button>
            <button
              type="button"
              onClick={() => handleSubmitAnalysis()}
              disabled={!query || isAnalyzing}
              className="btn-primary flex h-10 flex-shrink-0 items-center gap-1.5 whitespace-nowrap"
            >
              {isAnalyzing ? (
                <>
                  <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                  </svg>
                  分析中
                </>
              ) : (
                '分析'
              )}
            </button>
          </div>
        </header>

        {inputError || duplicateError ? (
          <div className="px-3 pb-2 md:px-4">
            {inputError ? (
              <InlineAlert
                variant="danger"
                title="输入有误"
                message={inputError}
                className="rounded-xl px-3 py-2 text-xs shadow-none"
              />
            ) : null}
            {!inputError && duplicateError ? (
              <InlineAlert
                variant="warning"
                title="任务已存在"
                message={duplicateError}
                className="rounded-xl px-3 py-2 text-xs shadow-none"
              />
            ) : null}
          </div>
        ) : null}

        <div className="flex-1 flex min-h-0 overflow-hidden">
          <div className="hidden min-h-0 w-64 shrink-0 flex-col overflow-hidden pl-4 pb-4 md:flex lg:w-72">
            {sidebarContent}
          </div>

          {sidebarOpen ? (
            <div className="fixed inset-0 z-40 md:hidden" onClick={() => setSidebarOpen(false)}>
              <div className="page-drawer-overlay absolute inset-0" />
              <div
                className="dashboard-card absolute left-0 top-0 flex h-[100dvh] max-h-[100dvh] w-[min(22rem,calc(100vw-1rem))] flex-col !overflow-x-hidden !overflow-y-auto overscroll-contain touch-pan-y [-webkit-overflow-scrolling:touch] !rounded-none !rounded-r-xl p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] shadow-2xl"
                onClick={(event) => event.stopPropagation()}
              >
                {sidebarContent}
              </div>
            </div>
          ) : null}

          <section className="flex-1 min-w-0 min-h-0 overflow-x-auto overflow-y-auto px-3 pb-4 md:px-6 touch-pan-y">
            {error ? (
              <ApiErrorAlert
                error={error}
                className="mb-3"
                onDismiss={clearError}
              />
            ) : null}
            {isLoadingReport ? (
              <div className="flex h-full flex-col items-center justify-center">
                <DashboardStateBlock title="加载报告中..." loading />
              </div>
            ) : selectedReport ? (
              <div className="max-w-4xl space-y-4 pb-8">
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <Button
                    variant="home-action-ai"
                    size="sm"
                    disabled={isAnalyzing || selectedReport.meta.id === undefined}
                    onClick={handleReanalyze}
                  >
                    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                    {reportText.reanalyze}
                  </Button>
                  <Button
                    variant="home-action-ai"
                    size="sm"
                    disabled={selectedReport.meta.id === undefined}
                    onClick={handleAskFollowUp}
                  >
                    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                    </svg>
                    追问 AI
                  </Button>
                  <Button
                    variant="home-action-ai"
                    size="sm"
                    disabled={selectedReport.meta.id === undefined}
                    onClick={openMarkdownDrawer}
                  >
                    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    {reportText.fullReport}
                  </Button>
                </div>
                <ReportSummary data={selectedReport} isHistory />
              </div>
            ) : (
              <div className="flex h-full items-center justify-center">
                <EmptyState
                  title="开始分析"
                  description="输入股票代码进行分析，或从左侧选择历史报告查看。"
                  className="max-w-xl border-dashed"
                  icon={(
                    <svg className="h-6 w-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                    </svg>
                  )}
                />
              </div>
            )}
          </section>
        </div>
      </div>

      {markdownDrawerOpen && selectedReport?.meta.id ? (
        <ReportMarkdown
          recordId={selectedReport.meta.id}
          stockName={selectedReport.meta.stockName || ''}
          stockCode={selectedReport.meta.stockCode}
          reportLanguage={reportLanguage}
          onClose={closeMarkdownDrawer}
        />
      ) : null}

      {feedbackOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4" role="dialog" aria-modal="true" aria-label="反馈问题">
          <div className="dashboard-card w-full max-w-lg space-y-4 p-5 shadow-2xl">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-foreground">反馈问题</h2>
              </div>
              <button
                type="button"
                onClick={handleCloseFeedback}
                className="rounded-lg px-2 py-1 text-secondary-text hover:bg-hover hover:text-foreground"
                aria-label="关闭反馈"
              >
                ×
              </button>
            </div>

            {feedbackStatus ? (
              <InlineAlert
                variant={feedbackStatus.type}
                title={feedbackStatus.type === 'success' ? '提交成功' : feedbackStatus.type === 'warning' ? '已接收' : '提交失败'}
                message={feedbackStatus.message}
                className="rounded-xl px-3 py-2 text-xs shadow-none"
              />
            ) : null}

            <div className="relative block space-y-1.5 text-sm text-secondary-text">
              <span id="feedback-category-label">问题类型</span>
              <button
                type="button"
                aria-haspopup="listbox"
                aria-expanded={feedbackCategoryOpen}
                aria-labelledby="feedback-category-label feedback-category-selected"
                onClick={() => setFeedbackCategoryOpen((open) => !open)}
                className="flex w-full items-center justify-between rounded-xl border border-subtle bg-surface px-3 py-2 text-left text-foreground outline-none transition-colors hover:border-cyan/40 focus:border-cyan/60"
              >
                <span id="feedback-category-selected">
                  {feedbackCategory === 'bug' ? 'Bug / 功能异常' : feedbackCategory === 'iteration' ? '迭代建议' : '其他'}
                </span>
                <span className="text-secondary-text">⌄</span>
              </button>
              {feedbackCategoryOpen ? (
                <div
                  role="listbox"
                  aria-labelledby="feedback-category-label"
                  className="absolute z-10 mt-1 w-full overflow-hidden rounded-xl border border-subtle bg-[#101827] shadow-2xl shadow-black/40"
                >
                  {([
                    ['bug', 'Bug / 功能异常'],
                    ['iteration', '迭代建议'],
                    ['other', '其他'],
                  ] as const).map(([value, label]) => {
                    const active = feedbackCategory === value;
                    return (
                      <button
                        key={value}
                        type="button"
                        role="option"
                        aria-selected={active}
                        onClick={() => {
                          setFeedbackCategory(value);
                          setFeedbackCategoryOpen(false);
                        }}
                        className={active
                          ? 'block w-full bg-cyan/20 px-3 py-2 text-left text-cyan'
                          : 'block w-full px-3 py-2 text-left text-foreground hover:bg-hover'}
                      >
                        {label}
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </div>

            <label className="block space-y-1.5 text-sm text-secondary-text">
              <span>问题描述</span>
              <textarea
                aria-label="问题描述"
                value={feedbackContent}
                onChange={(event) => setFeedbackContent(event.target.value)}
                placeholder="请描述你遇到的问题、操作步骤、股票代码或页面现象。"
                rows={5}
                maxLength={2000}
                className="w-full resize-none rounded-xl border border-subtle bg-surface px-3 py-2 text-foreground outline-none focus:border-cyan/60"
              />
            </label>

            <label className="block space-y-1.5 text-sm text-secondary-text">
              <span>联系方式（选填）</span>
              <input
                aria-label="联系方式（选填）"
                value={feedbackContact}
                onChange={(event) => setFeedbackContact(event.target.value)}
                placeholder={currentUser?.username ? `当前用户：${currentUser.username}` : '邮箱 / 微信 / 备注'}
                maxLength={200}
                className="w-full rounded-xl border border-subtle bg-surface px-3 py-2 text-foreground outline-none focus:border-cyan/60"
              />
            </label>

            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={handleCloseFeedback} disabled={isSubmittingFeedback}>
                取消
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={handleSubmitFeedback}
                isLoading={isSubmittingFeedback}
                loadingText="提交中..."
              >
                提交反馈
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      <ConfirmDialog
        isOpen={showDeleteConfirm}
        title="删除历史记录"
        message={
          selectedHistoryIds.length === 1
            ? '确认删除这条历史记录吗？删除后将不可恢复。'
            : `确认删除选中的 ${selectedHistoryIds.length} 条历史记录吗？删除后将不可恢复。`
        }
        confirmText={isDeletingHistory ? '删除中...' : '确认删除'}
        cancelText="取消"
        isDanger={true}
        onConfirm={handleDeleteSelectedHistory}
        onCancel={() => setShowDeleteConfirm(false)}
      />
    </div>
  );
};

export default HomePage;
