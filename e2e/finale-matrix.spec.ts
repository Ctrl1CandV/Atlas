// E-4 完整矩阵：Chromium × {dark, light} × {100%, 200%} 四组合终局卡片基线。
// 用户裁定（2026-08-28）：200% 缩放 = CSS 视口减半（640×400），它正是浏览器
// 200% 缩放的布局效果（overflow/可读性等价）；字体锁定条件触发——冒烟 CI 已证
// 本机↔CI 渲染逐字节一致，只有真出现漂移才引入内嵌字体（届时先根因分析）。
// 主题：应用经 prefers-color-scheme + localStorage 选主题，Playwright 的
// colorScheme 上下文选项即可锁定，不需要产品代码改动。
// 截图基线只在 chromium 维护：Firefox/Edge 引擎渲染必然与 Chromium 不同像素，
// 跨引擎逐字节比对是伪需求（flake 制度：不一致必须根因修复，禁止调容差蒙混）。
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const manifestPath = path.join(here, '.seed', 'manifest.json');
if (!existsSync(manifestPath)) {
  throw new Error('缺少 e2e/.seed/manifest.json:种子由 helpers/server.py 在启动时生成');
}
const manifest = JSON.parse(readFileSync(manifestPath, 'utf-8')) as {
  done_run_id: string;
};

// 截图基线只在 chromium 项目跑:过滤在 playwright.config.ts 的 msedge/firefox
// 项目 testIgnore 里做(收集期;test.skip 回调拿不到 project 信息)。
// 注意不能按 browserName 判定:Edge 走 Chromium 内核,browserName 同为 'chromium'。

// slug 不含 '%':Playwright 会清洗快照文件名里的特殊字符(曾把 100% 变成
// 100-),文件名统一用纯字母数字。
const COMBOS = [
  { theme: 'dark', zoom: '100%', slug: '100', viewport: { width: 1280, height: 800 } },
  { theme: 'dark', zoom: '200%', slug: '200', viewport: { width: 640, height: 400 } },
  { theme: 'light', zoom: '100%', slug: '100', viewport: { width: 1280, height: 800 } },
  { theme: 'light', zoom: '200%', slug: '200', viewport: { width: 640, height: 400 } },
] as const;

for (const { theme, zoom, slug, viewport } of COMBOS) {
  // test.use 只能在 describe 层调用:每个组合一个 describe 携带自己的上下文。
  test.describe(`终局卡片基线:${theme}/${zoom}`, () => {
    test.use({ colorScheme: theme, viewport });

    test('渲染与基线逐字节一致', async ({ page }) => {
      await page.goto(`#/runs/${manifest.done_run_id}`);
      const finale = page.getByRole('region', { name: '终局总结' });
      await expect(finale).toBeVisible();
      // 种子把 ts/duration_s 归一为固定值:卡片上的每个数字跨次稳定,截图
      // 不一致就只能是真实回归(flake 制度的前提)。
      await expect(finale).toHaveScreenshot(`finale-${theme}-${slug}.png`,
        { animations: 'disabled' });
    });
  });
}
