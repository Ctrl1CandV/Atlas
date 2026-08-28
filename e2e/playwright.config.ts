// E-4 冒烟:浏览器矩阵第一步(chromium 单项目)。
// 独立于主双绿门(ci.yml);workflow_dispatch/release 触发的 e2e-smoke.yml 才跑。
// flake 制度:retries=0,红了修根因,不允许重试蒙混;截图容差调整必须附
// 人眼可见差异理由。
import { defineConfig, devices } from '@playwright/test';

const port = Number(process.env.ATLAS_E2E_PORT ?? 8977);

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
    viewport: { width: 1280, height: 800 },
    // 跨环境确定性:语言/时区锁死(收官时刻的 toLocaleTimeString 才可比),
    // reduce-motion 抑制动效(完整矩阵阶段同样沿用)。
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'uv run python helpers/server.py',
    url: `http://127.0.0.1:${port}/api/workflows`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  snapshotPathTemplate: '__screenshots__/{arg}{ext}',
});
