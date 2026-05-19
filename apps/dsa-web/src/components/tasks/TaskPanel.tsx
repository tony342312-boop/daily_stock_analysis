import type React from 'react';
import { useEffect, useState } from 'react';
import { Badge, Card, StatusDot } from '../common';
import { DashboardPanelHeader } from '../dashboard';
import type { NewsIntelItem, TaskInfo } from '../../types/analysis';
import { historyApi } from '../../api/history';

/**
 * 任务项组件属性
 */
interface TaskItemProps {
  task: TaskInfo;
}

interface NewsPreviewState {
  taskId: string;
  items: NewsIntelItem[];
}

/**
 * 单个任务项
 */
const TaskItem: React.FC<TaskItemProps> = ({ task }) => {
  const isPending = task.status === 'pending';
  const isProcessing = task.status === 'processing';
  const statusLabel = isProcessing ? '分析中' : '等待中';
  const statusVariant = isProcessing ? 'info' : 'default';
  const statusTone = isProcessing ? 'info' : 'neutral';
  const progress = Math.max(0, Math.min(100, task.progress || 0));
  const shouldLoadNewsPreview = isProcessing && progress >= 46;
  const [newsPreview, setNewsPreview] = useState<NewsPreviewState>({
    taskId: '',
    items: [],
  });
  const newsItems = (
    shouldLoadNewsPreview && newsPreview.taskId === task.taskId ? newsPreview.items : []
  );

  useEffect(() => {
    if (!shouldLoadNewsPreview) {
      return undefined;
    }

    let cancelled = false;

    const loadNewsPreview = async () => {
      try {
        const response = await historyApi.getNews(task.taskId, 4);
        if (!cancelled) {
          setNewsPreview({
            taskId: task.taskId,
            items: response.items || [],
          });
        }
      } catch {
        if (!cancelled) {
          setNewsPreview({
            taskId: task.taskId,
            items: [],
          });
        }
      }
    };

    void loadNewsPreview();
    const timerId = window.setInterval(loadNewsPreview, 12000);

    return () => {
      cancelled = true;
      window.clearInterval(timerId);
    };
  }, [shouldLoadNewsPreview, task.taskId]);

  return (
    <div className="home-subpanel px-3 py-2.5">
      <div className="flex items-center gap-3">
        {/* 状态图标 */}
        <div className="shrink-0">
          {isProcessing ? (
            <StatusDot tone="info" pulse className="h-2.5 w-2.5" aria-label="任务进行中" />
          ) : isPending ? (
            <StatusDot tone="neutral" className="h-2.5 w-2.5" aria-label="任务等待中" />
          ) : null}
        </div>

        {/* 任务信息 */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-foreground truncate">
              {task.stockName || task.stockCode}
            </span>
            <span className="text-xs text-muted-text">
              {task.stockCode}
            </span>
          </div>
          {task.message && (
            <p className="text-xs text-secondary-text truncate mt-0.5">
              {task.message}
            </p>
          )}
          <div className="mt-2 flex items-center gap-2">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-white/8">
              <div
                className="h-full rounded-full bg-cyan transition-[width] duration-300 ease-out"
                style={{ width: `${progress}%` }}
              />
            </div>
            <span className="shrink-0 text-[11px] text-muted-text tabular-nums">
              {progress}%
            </span>
          </div>
        </div>

        {/* 状态标签 */}
        <div className="flex-shrink-0">
          <Badge
            variant={statusVariant}
            className="min-w-[4.75rem] justify-center gap-1.5 shadow-none"
            aria-label={`任务状态：${statusLabel}`}
          >
            <StatusDot tone={statusTone} pulse={isProcessing} className="h-1.5 w-1.5" />
            {statusLabel}
          </Badge>
        </div>
      </div>

      {newsItems.length > 0 && (
        <div className="mt-3 border-t border-subtle pt-2">
          <div className="mb-1.5 text-[11px] font-medium text-muted-text">已检索内容</div>
          <div className="space-y-1.5">
            {newsItems.map((item, index) => (
              <a
                key={`${item.url || item.title}-${index}`}
                href={item.url}
                target="_blank"
                rel="noreferrer"
                className="block rounded-md bg-white/5 px-2.5 py-2 text-xs hover:bg-white/8"
              >
                <span className="block truncate text-foreground">{item.title}</span>
                {item.snippet ? (
                  <span className="mt-0.5 block line-clamp-2 text-muted-text">{item.snippet}</span>
                ) : null}
              </a>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

/**
 * 任务面板属性
 */
interface TaskPanelProps {
  /** 任务列表 */
  tasks: TaskInfo[];
  /** 是否显示 */
  visible?: boolean;
  /** 标题 */
  title?: string;
  /** 自定义类名 */
  className?: string;
}

/**
 * 任务面板组件
 * 显示进行中的分析任务列表
 */
export const TaskPanel: React.FC<TaskPanelProps> = ({
  tasks,
  visible = true,
  title = '分析任务',
  className = '',
}) => {
  // 筛选活跃任务（pending 和 processing）
  const activeTasks = tasks.filter(
    (t) => t.status === 'pending' || t.status === 'processing'
  );

  // 无任务或不可见时不渲染
  if (!visible || activeTasks.length === 0) {
    return null;
  }

  const pendingCount = activeTasks.filter((t) => t.status === 'pending').length;
  const processingCount = activeTasks.filter((t) => t.status === 'processing').length;

  return (
    <Card
      variant="bordered"
      padding="none"
      className={`home-panel-card overflow-hidden ${className}`}
    >
      <div className="border-b border-subtle px-3 py-3">
        <DashboardPanelHeader
          className="mb-0"
          title={title}
          titleClassName="text-sm font-medium"
          leading={(
            <svg className="h-4 w-4 text-cyan" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
              />
            </svg>
          )}
          headingClassName="items-center"
          actions={(
            <div className="flex items-center gap-2 text-xs text-muted-text">
              {processingCount > 0 && (
                <span className="flex items-center gap-1">
                  <StatusDot tone="info" pulse className="h-1.5 w-1.5" aria-label="进行中任务" />
                  {processingCount} 进行中
                </span>
              )}
              {pendingCount > 0 ? (
                <span className="flex items-center gap-1">
                  <StatusDot tone="neutral" className="h-1.5 w-1.5" aria-label="等待中任务" />
                  {pendingCount} 等待中
                </span>
              ) : null}
            </div>
          )}
        />
      </div>

      <div className="max-h-64 overflow-y-auto p-2">
        <div className="space-y-2">
          {activeTasks.map((task) => (
            <TaskItem key={task.taskId} task={task} />
          ))}
        </div>
      </div>
    </Card>
  );
};

export default TaskPanel;
