// E-4 浏览器矩阵:chromium 全量四组合截图 + Edge/Firefox 键盘流冒烟。
// 独立于主双绿门(ci.yml);workflow_dispatch/release 触发的 e2e-smoke.yml 才跑。
// flake 制度:retries=0,红了修根因,不允许重试蒙混;截图容差调整必须附
// 人眼可见差异理由。
// 已裁定(2026-08-28):「200% 缩放」= CSS 视口减半(浏览器缩放的布局效果);
// 字体本地锁定降级为条件触发——冒烟首跑已证本机/CI 渲染逐字节一致,
// 出现漂移才引入开源字体。
import { defineConfig, devices } from '@playwright/test';

const port = Number(process.env.ATLAS_E2E_PORT ?? 8977);
const viewport = { width: 1280, height: 800 };

export default defineConfig({
  testDir: '.',
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  outputDir: 'test-results',
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    headless: true,
    // 跨环境确定性:语言/时区锁死(收官时刻的 toLocaleTimeString 才可比),
    // reduce-motion 抑制动效(完整矩阵阶段同样沿用)。
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], viewport } },
    // Edge 走系统安装的浏览器(Desktop Edge 预设);Firefox 用 Playwright 自带
    // 构建。两者只跑键盘审批流(冒烟遍历),截图基线仅维护在 chromium——
    // testIgnore 在收集期过滤(test.skip 回调拿不到 project 信息;且 Edge 的
    // browserName 也是 'chromium',按引擎名判会漏)。
    { name: 'msedge', use: { ...devices['Desktop Edge'], viewport },
      testIgnore: /finale-matrix/ },
    { name: 'firefox', use: { ...devices['Desktop Firefox'], viewport },
      testIgnore: /finale-matrix/ },
  ],
  webServer: {
    command: 'uv run python helpers/server.py',
    url: `http://127.0.0.1:${port}/api/workflows`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  snapshotPathTemplate: '__screenshots__/{arg}{ext}',
});
