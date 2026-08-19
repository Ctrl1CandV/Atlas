import test from 'node:test';
import assert from 'node:assert/strict';

import { deleteCompletedRuns } from './api.ts';
import { applyDeletedRuns, summarizeRunCleanup } from './runCleanup.ts';

test('deleteCompletedRuns filters terminal runs and reports partial failures', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  let active = 0;
  let maxActive = 0;
  globalThis.fetch = async (url, options) => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    calls.push({ url, options });
    await new Promise((resolve) => setTimeout(resolve, 5));
    active -= 1;
    if (String(url).endsWith('/failed-run')) {
      return new Response(JSON.stringify({
        detail: 'run is locked by runs/.locks/failed-run.lock; retry later',
      }), {
        status: 423,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ deleted: 'done-run' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const result = await deleteCompletedRuns([
      { run_id: 'done-run', status: 'done' },
      { run_id: 'paused-run', status: 'paused' },
      { run_id: 'failed-run', status: 'failed' },
      { run_id: 'running-run', status: 'running' },
    ]);

    assert.equal(maxActive, 1, 'deletions must be sequential');
    assert.deepEqual(calls.map((call) => call.url), [
      '/api/runs/done-run',
      '/api/runs/failed-run',
    ]);
    for (const call of calls) {
      assert.equal(call.options.method, 'DELETE');
      assert.equal(call.options.headers['X-Atlas-Request'], '1');
    }
    assert.deepEqual(result, {
      eligible: 2,
      deleted: ['done-run'],
      failed: [{
        runId: 'failed-run',
        error: 'run is locked by runs/.locks/failed-run.lock; retry later',
      }],
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('applyDeletedRuns cancels and clears the current run before refreshing', async () => {
  const calls = [];
  const action = (name) => () => { calls.push(name); };

  const deletedCurrentRun = await applyDeletedRuns('current-run', ['other-run', 'current-run'], {
    cancelSubscription: action('cancelSubscription'),
    clearRunId: action('clearRunId'),
    clearSummary: action('clearSummary'),
    clearEvents: action('clearEvents'),
    clearDetail: action('clearDetail'),
    clearSelectedNode: action('clearSelectedNode'),
    clearWorkspace: action('clearWorkspace'),
    clearRunParameters: action('clearRunParameters'),
    navigateToRuns: action('navigateToRuns'),
    refreshRuns: async () => {
      calls.push('refreshRuns:start');
      await Promise.resolve();
      calls.push('refreshRuns:end');
    },
  });

  assert.equal(deletedCurrentRun, true);
  assert.deepEqual(calls, [
    'cancelSubscription',
    'clearRunId',
    'clearSummary',
    'clearEvents',
    'clearDetail',
    'clearSelectedNode',
    'clearWorkspace',
    'clearRunParameters',
    'navigateToRuns',
    'refreshRuns:start',
    'refreshRuns:end',
  ]);
});

test('applyDeletedRuns only refreshes when another run was deleted', async () => {
  const calls = [];
  const unexpected = (name) => () => { calls.push(name); };

  const deletedCurrentRun = await applyDeletedRuns('current-run', ['other-run'], {
    cancelSubscription: unexpected('cancelSubscription'),
    clearRunId: unexpected('clearRunId'),
    clearSummary: unexpected('clearSummary'),
    clearEvents: unexpected('clearEvents'),
    clearDetail: unexpected('clearDetail'),
    clearSelectedNode: unexpected('clearSelectedNode'),
    clearWorkspace: unexpected('clearWorkspace'),
    clearRunParameters: unexpected('clearRunParameters'),
    navigateToRuns: unexpected('navigateToRuns'),
    refreshRuns: unexpected('refreshRuns'),
  });

  assert.equal(deletedCurrentRun, false);
  assert.deepEqual(calls, ['refreshRuns']);
});

test('summarizeRunCleanup preserves every partial failure', () => {
  assert.deepEqual(summarizeRunCleanup({
    eligible: 3,
    deleted: ['done-run'],
    failed: [
      { runId: 'locked-run', error: '423: held by runs/.locks/locked-run.lock' },
      { runId: 'busy-run', error: '409: sharing violation; retry later' },
    ],
  }), {
    kind: 'err',
    text: '已清理 1 条，失败 2 条：locked-run: 423: held by runs/.locks/locked-run.lock；busy-run: 409: sharing violation; retry later',
  });
});
