import type { RunCleanupResult } from './api';

export interface DeletedRunActions {
  cancelSubscription: () => void;
  clearRunId: () => void;
  clearSummary: () => void;
  clearEvents: () => void;
  clearDetail: () => void;
  clearSelectedNode: () => void;
  clearWorkspace: () => void;
  clearRunParameters: () => void;
  navigateToRuns: () => void;
  refreshRuns: () => void | Promise<void>;
}

/** Apply deletion side effects in a stable order that can be verified without a DOM. */
export async function applyDeletedRuns(
  currentRunId: string | null,
  deletedIds: string[],
  actions: DeletedRunActions,
): Promise<boolean> {
  const deletedCurrentRun = currentRunId !== null && deletedIds.includes(currentRunId);
  if (deletedCurrentRun) {
    actions.cancelSubscription();
    actions.clearRunId();
    actions.clearSummary();
    actions.clearEvents();
    actions.clearDetail();
    actions.clearSelectedNode();
    actions.clearWorkspace();
    actions.clearRunParameters();
    actions.navigateToRuns();
  }
  await actions.refreshRuns();
  return deletedCurrentRun;
}

export interface RunCleanupSummary {
  kind: 'ok' | 'err';
  text: string;
}

/** Preserve every per-run failure in the batch cleanup message. */
export function summarizeRunCleanup(result: RunCleanupResult): RunCleanupSummary {
  if (result.failed.length === 0) {
    return { kind: 'ok', text: `已清理 ${result.deleted.length} 条运行记录。` };
  }
  const failures = result.failed.map((item) => `${item.runId}: ${item.error}`).join('；');
  return {
    kind: 'err',
    text: `已清理 ${result.deleted.length} 条，失败 ${result.failed.length} 条：${failures}`,
  };
}
