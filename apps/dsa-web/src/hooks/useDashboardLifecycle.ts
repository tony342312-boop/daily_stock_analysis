import { useCallback, useEffect, useRef } from 'react';
import { analysisApi } from '../api/analysis';
import type { TaskInfo } from '../types/analysis';
import { useTaskStream } from './useTaskStream';

type UseDashboardLifecycleOptions = {
  loadInitialHistory: () => Promise<void>;
  refreshHistory: (silent?: boolean) => Promise<void>;
  syncTaskCreated: (task: TaskInfo) => void;
  syncTaskUpdated: (task: TaskInfo) => void;
  syncTaskFailed: (task: TaskInfo) => void;
  removeTask: (taskId: string) => void;
  enabled?: boolean;
};

export function useDashboardLifecycle({
  loadInitialHistory,
  refreshHistory,
  syncTaskCreated,
  syncTaskUpdated,
  syncTaskFailed,
  removeTask,
  enabled = true,
}: UseDashboardLifecycleOptions): void {
  const removalTimeoutsRef = useRef<number[]>([]);
  const terminalTaskIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!enabled) {
      return;
    }

    void loadInitialHistory();
  }, [enabled, loadInitialHistory]);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const intervalId = window.setInterval(() => {
      void refreshHistory(true);
    }, 30_000);

    return () => window.clearInterval(intervalId);
  }, [enabled, refreshHistory]);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        void refreshHistory(true);
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, [enabled, refreshHistory]);

  useEffect(() => {
    return () => {
      removalTimeoutsRef.current.forEach((timeoutId) => window.clearTimeout(timeoutId));
      removalTimeoutsRef.current = [];
      terminalTaskIdsRef.current.clear();
    };
  }, []);

  const scheduleTaskRemoval = useCallback((taskId: string, delayMs: number) => {
    const timeoutId = window.setTimeout(() => {
      removeTask(taskId);
      removalTimeoutsRef.current = removalTimeoutsRef.current.filter((item) => item !== timeoutId);
    }, delayMs);

    removalTimeoutsRef.current.push(timeoutId);
  }, [removeTask]);

  const handleCompletedTask = useCallback((task: TaskInfo) => {
    terminalTaskIdsRef.current.add(task.taskId);
    syncTaskUpdated(task);
    void refreshHistory(true);
    scheduleTaskRemoval(task.taskId, 2_000);
  }, [refreshHistory, scheduleTaskRemoval, syncTaskUpdated]);

  const handleFailedTask = useCallback((task: TaskInfo) => {
    terminalTaskIdsRef.current.add(task.taskId);
    syncTaskFailed(task);
    scheduleTaskRemoval(task.taskId, 5_000);
  }, [scheduleTaskRemoval, syncTaskFailed]);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    let isCancelled = false;

    const pollTasks = async () => {
      try {
        const response = await analysisApi.getTasks({ limit: 50 });
        if (isCancelled) {
          return;
        }

        for (const task of response.tasks || []) {
          if (task.status === 'pending' || task.status === 'processing') {
            terminalTaskIdsRef.current.delete(task.taskId);
            syncTaskCreated(task);
            syncTaskUpdated(task);
            continue;
          }

          if (terminalTaskIdsRef.current.has(task.taskId)) {
            continue;
          }

          if (task.status === 'completed') {
            handleCompletedTask(task);
          } else if (task.status === 'failed') {
            handleFailedTask(task);
          }
        }
      } catch (error) {
        console.warn('Task polling failed:', error);
      }
    };

    void pollTasks();
    const intervalId = window.setInterval(pollTasks, 3_000);

    return () => {
      isCancelled = true;
      window.clearInterval(intervalId);
    };
  }, [
    enabled,
    handleCompletedTask,
    handleFailedTask,
    syncTaskCreated,
    syncTaskUpdated,
  ]);

  useTaskStream({
    onTaskCreated: syncTaskCreated,
    onTaskStarted: syncTaskUpdated,
    onTaskProgress: syncTaskUpdated,
    onTaskCompleted: handleCompletedTask,
    onTaskFailed: handleFailedTask,
    onError: () => {
      console.warn('SSE connection disconnected, reconnecting...');
    },
    enabled,
  });
}

export default useDashboardLifecycle;
