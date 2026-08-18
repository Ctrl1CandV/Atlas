import test from 'node:test';
import assert from 'node:assert/strict';

import { deleteCompletedRuns } from './api.ts';

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
      return new Response(JSON.stringify({ detail: 'RUN.lock is fresh' }), {
        status: 409,
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
      failed: [{ runId: 'failed-run', error: 'RUN.lock is fresh' }],
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
