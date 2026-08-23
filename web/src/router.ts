// 极简 hash 路由(PLAN-v3 §6.3)。不引 react-router:视图少,
// 而且挂载是 FastAPI 静态目录,hash 路由刷新不需要 SPA fallback。
//
// 路由形状:
//   #/observe                    观测台
//   #/observe/w/:workflowId      观测台 + 选中工作流
//   #/runs/:rid                  观测台 + 选中运行
//   #/runs/:rid/n/:nodeId        观测台 + 选中运行与节点
//   #/guide                      使用指南(默认章)
//   #/guide/:chapter             指定章节
//   #/settings                   设置

import { useEffect, useState } from 'react';

export interface Route {
  view: 'observe' | 'guide' | 'settings';
  workflowId: string | null;
  runId: string | null;
  nodeId: string | null;
  chapter: string;
}

const GUIDE_CHAPTERS = [
  'overview', 'quickstart', 'concepts', 'examples',
  'models', 'results', 'safety', 'mcp-human',
  'troubleshooting', 'development',
] as const;

export function isGuideChapter(c: string): boolean {
  return (GUIDE_CHAPTERS as readonly string[]).includes(c);
}

export function parseHash(hash: string): Route {
  const h = hash.replace(/^#/, '');
  const parts = h.split('/').filter(Boolean);
  const route: Route = {
    view: 'observe', workflowId: null, runId: null, nodeId: null,
    chapter: 'overview',
  };
  if (parts[0] === 'settings') {
    route.view = 'settings';
    return route;
  }
  if (parts[0] === 'guide') {
    route.view = 'guide';
    const c = parts[1] ?? 'overview';
    route.chapter = isGuideChapter(c) ? c : 'overview';
    return route;
  }
  if (parts[0] === 'runs' && parts[1]) {
    route.runId = parts[1];
    if (parts[2] === 'n' && parts[3]) route.nodeId = parts[3];
    return route;
  }
  if (parts[0] === 'observe' && parts[1] === 'w' && parts[2]) {
    route.workflowId = parts[2];
    if (parts[3] === 'n' && parts[4]) route.nodeId = parts[4];
  }
  return route;
}

export function hrefFor(route: Partial<Route>): string {
  const r = { ...parseHash(location.hash), ...route };
  if (r.view === 'settings') return '#/settings';
  if (r.view === 'guide') return `#/guide/${r.chapter}`;
  if (r.runId) {
    return r.nodeId ? `#/runs/${r.runId}/n/${r.nodeId}` : `#/runs/${r.runId}`;
  }
  if (r.workflowId) {
    return r.nodeId
      ? `#/observe/w/${r.workflowId}/n/${r.nodeId}`
      : `#/observe/w/${r.workflowId}`;
  }
  return '#/observe';
}

/** 订阅 hash 变化;navigate 用 replaceState(观测台内部选择
 *  不该堆满历史栈,指南章节间才用 push 语义)。 */
export function useRoute(): [Route, (hash: string, push?: boolean) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(location.hash));
  useEffect(() => {
    const apply = () => setRoute(parseHash(location.hash));
    window.addEventListener('hashchange', apply);
    window.addEventListener('popstate', apply);
    return () => {
      window.removeEventListener('hashchange', apply);
      window.removeEventListener('popstate', apply);
    };
  }, []);
  const navigate = (hash: string, push = false) => {
    const url = `${location.pathname}${location.search}${hash}`;
    if (push) history.pushState(null, '', url);
    else history.replaceState(null, '', url);
    setRoute(parseHash(hash));
  };
  return [route, navigate];
}
