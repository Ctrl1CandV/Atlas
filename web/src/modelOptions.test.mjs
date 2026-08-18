import test from 'node:test';
import assert from 'node:assert/strict';

import { deriveModelOptions } from './modelOptions.ts';

const credential = (configured) => ({ configured, source: configured ? 'file' : 'unset', writable: true });

test('deriveModelOptions separates configured LLM and Anthropic-compatible agent models', () => {
  const result = deriveModelOptions([
    {
      id: 'Both', openaiBaseUrl: 'https://openai.example/v1',
      anthropicBaseUrl: 'https://anthropic.example', models: ['m1'],
      apiKeyEnv: 'BOTH_KEY', preferTransport: null, maxOutputTokens: null,
      credential: credential(true),
    },
    {
      id: 'OpenOnly', openaiBaseUrl: 'https://openai.example/v1',
      anthropicBaseUrl: null, models: ['m2'], apiKeyEnv: 'OPEN_KEY',
      preferTransport: null, maxOutputTokens: null, credential: credential(true),
    },
    {
      id: 'MissingKey', openaiBaseUrl: null,
      anthropicBaseUrl: 'https://anthropic.example', models: ['m3'],
      apiKeyEnv: 'MISSING_KEY', preferTransport: null, maxOutputTokens: null,
      credential: credential(false),
    },
  ]);

  assert.deepEqual(result.llm, ['Both:m1', 'OpenOnly:m2']);
  assert.deepEqual(result.agent, ['Both:m1']);
});

test('deriveModelOptions preserves fully qualified model references', () => {
  const result = deriveModelOptions([{
    id: 'Vendor', openaiBaseUrl: null, anthropicBaseUrl: 'https://a.example',
    models: ['Other:qualified'], apiKeyEnv: 'VENDOR_KEY', preferTransport: null,
    maxOutputTokens: null, credential: credential(true),
  }]);
  assert.deepEqual(result.llm, ['Other:qualified']);
  assert.deepEqual(result.agent, ['Other:qualified']);
});
