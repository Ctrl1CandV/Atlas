import test from 'node:test';
import assert from 'node:assert/strict';

import { subscribeRun } from './api.ts';

class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
    this.closed = false;
    this.listeners = new Map();
    FakeEventSource.instances.push(this);
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  close() {
    this.closed = true;
  }

  message(event) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }

  emit(type) {
    this.listeners.get(type)?.({ type });
  }

  fail() {
    this.onerror?.(new Event('error'));
  }
}

test('interrupted control signal preserves cursor, stops reconnects, and allows resubscribe', () => {
  const originalEventSource = globalThis.EventSource;
  const originalSetTimeout = globalThis.setTimeout;
  const scheduled = [];
  const signals = [];
  let ended = 0;

  FakeEventSource.instances = [];
  globalThis.EventSource = FakeEventSource;
  globalThis.setTimeout = (callback, delay) => {
    scheduled.push({ callback, delay });
    return scheduled.length;
  };

  try {
    const cancel = subscribeRun('same-run', (event) => signals.push(event), () => { ended += 1; });
    const first = FakeEventSource.instances[0];
    assert.equal(first.url, '/api/runs/same-run/events?after=0');

    first.message({ seq: 7, ts: 't', type: 'node_done', node: 'a' });
    first.fail();
    assert.equal(scheduled.length, 1);
    scheduled.shift().callback();

    const resumedConnection = FakeEventSource.instances[1];
    assert.equal(resumedConnection.url, '/api/runs/same-run/events?after=7');
    first.message({ seq: 99, ts: 'late', type: 'node_done', node: 'stale' });
    resumedConnection.message({ seq: 8, ts: 't', type: 'node_started', node: 'b' });
    resumedConnection.message({ seq: 8, ts: 'duplicate', type: 'node_started', node: 'b' });
    resumedConnection.message({ seq: 6, ts: 'old', type: 'node_done', node: 'old' });
    resumedConnection.emit('run_interrupted');

    assert.equal(resumedConnection.closed, true);
    assert.deepEqual(signals.map(({ seq, type }) => ({ seq, type })), [
      { seq: 7, type: 'node_done' },
      { seq: 8, type: 'node_started' },
      { seq: 8, type: 'run_interrupted' },
    ]);
    assert.equal(ended, 0, 'interrupted is a refresh signal, not a terminal ledger event');

    resumedConnection.fail();
    assert.equal(scheduled.length, 0, 'an interrupted subscription must not reconnect');
    assert.ok(FakeEventSource.instances.every((source) => !source.url.includes('after=-1')));
    cancel();

    const cancelNextGeneration = subscribeRun('same-run', () => {}, () => {});
    assert.equal(FakeEventSource.instances[2].url, '/api/runs/same-run/events?after=0');
    cancelNextGeneration();
  } finally {
    globalThis.EventSource = originalEventSource;
    globalThis.setTimeout = originalSetTimeout;
  }
});
