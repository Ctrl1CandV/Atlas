export interface AgentExecutionPresentation {
  runnerLabel: string;
  boundaryNote: string | null;
  allowWebLabel: string;
  allowWebNote: string;
}

/** User-visible agent boundary text, kept pure so wording and semantics are testable. */
export function formatAgentExecution(
  runner: string | undefined,
  allowWeb: boolean | null | undefined,
): AgentExecutionPresentation {
  const local = runner === 'local_cli';
  return {
    runnerLabel: local ? '本机受控执行（目录隔离）' : (runner ?? '运行前确定'),
    boundaryNote: local
      ? '同一用户身份运行，非 OS 沙箱；目录隔离不阻止访问当前用户可访问的其他宿主路径。'
      : null,
    allowWebLabel: allowWeb === true
      ? '开启（WebSearch / WebFetch）'
      : '关闭（默认）',
    allowWebNote: allowWeb === true
      ? '允许 agent 使用网络搜索工具；不构成网络隔离。'
      : '禁用 WebSearch / WebFetch；不阻止 Bash 等工具自行联网。',
  };
}
