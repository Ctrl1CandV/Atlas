import test from 'node:test';
import assert from 'node:assert/strict';

import { formatAgentExecution } from './nodeDetailPresentation.ts';

test('local agent presentation states runner, web access, and same-user boundary', () => {
  assert.deepEqual(formatAgentExecution('local_cli', true), {
    runnerLabel: '本机受控执行（目录隔离）',
    boundaryNote: '同一用户身份运行，非 OS 沙箱；目录隔离不阻止访问当前用户可访问的其他宿主路径。',
    allowWebLabel: '开启（WebSearch / WebFetch）',
    allowWebNote: '允许 agent 使用网络搜索工具；不构成网络隔离。',
  });
});

test('allow_web defaults off without overstating network isolation', () => {
  assert.deepEqual(formatAgentExecution(undefined, undefined), {
    runnerLabel: '运行前确定',
    boundaryNote: null,
    allowWebLabel: '关闭（默认）',
    allowWebNote: '禁用 WebSearch / WebFetch；不阻止 Bash 等工具自行联网。',
  });
});
