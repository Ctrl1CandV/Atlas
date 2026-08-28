// E-4 冒烟:纯键盘审批流 + 终局卡片基线截图。
// 选择器纪律(合同审查重点 1/5):只用 role/text/aria 定位,禁 nth() 与
// 深层 css 链;全程不出现 mouse.* ——审批这条路的等价证据只能来自键盘。
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const manifestPath = path.join(here, '.seed', 'manifest.json');
if (!existsSync(manifestPath)) {
  throw new Error('缺少 e2e/.seed/manifest.json:种子由 helpers/server.py 在启动时生成');
}
const manifest = JSON.parse(readFileSync(manifestPath, 'utf-8')) as {
  gate_run_id: string;
  done_run_id: string;
};

interface FocusSnapshot {
  tag: string;
  label: string;
  outlineStyle: string;
  outlineWidth: string;
  boxShadow: string;
}

/** 当前焦点元素的可访问名与焦点可见性指标(焦点环断言的原料)。 */
function describeFocus(page: Page): Promise<FocusSnapshot | null> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) return null;
    const cs = getComputedStyle(el);
    return {
      tag: el.tagName,
      label: el.getAttribute('aria-label') ?? (el.textContent ?? '').trim(),
      outlineStyle: cs.outlineStyle,
      outlineWidth: cs.outlineWidth,
      boxShadow: cs.boxShadow,
    };
  });
}

test('纯键盘完成一轮批准:Tab 焦点环可达批复控件,Enter 等价点击', async ({ page }) => {
  await page.goto(`#/runs/${manifest.gate_run_id}`);
  await expect(page.getByText('等待人工批准')).toBeVisible();

  // 从页面起点连续 Tab 环游整圈,记录批复控件的可达性与相对顺序。
  let idxComment = -1, idxApprove = -1, idxChanges = -1, idxReject = -1;
  let commentFocus: FocusSnapshot | null = null;
  let approveFocus: FocusSnapshot | null = null;
  let rejectFocus: FocusSnapshot | null = null;
  for (let step = 0; step < 80; step++) {
    const focus = await describeFocus(page);
    if (focus) {
      if (focus.label === '批复说明') { idxComment = step; commentFocus = focus; }
      else if (focus.label === '批准') { idxApprove = step; approveFocus = focus; }
      else if (focus.label === '要求修改') { idxChanges = step; }
      else if (focus.label === '驳回') { idxReject = step; rejectFocus = focus; }
    }
    if (idxComment >= 0 && idxApprove >= 0 && idxChanges >= 0 && idxReject >= 0) break;
    await page.keyboard.press('Tab');
  }
  expect(idxComment, 'Tab 焦点环必须可达批复说明输入框').toBeGreaterThanOrEqual(0);
  expect(idxApprove, 'Tab 焦点环必须可达批准按钮').toBeGreaterThanOrEqual(0);
  expect(idxChanges, 'routed 审批必须可达要求修改按钮').toBeGreaterThanOrEqual(0);
  expect(idxReject, 'Tab 焦点环必须可达驳回按钮').toBeGreaterThanOrEqual(0);
  expect(idxComment, '批复输入框在批准之前').toBeLessThan(idxApprove);
  expect(idxApprove, '批准在要求修改之前(DOM 顺序即 Tab 序)').toBeLessThan(idxChanges);
  expect(idxChanges, '要求修改在驳回之前').toBeLessThan(idxReject);

  // 焦点可见:输入框的焦点环是 box-shadow(样式表对 input 设计性 outline:none),
  // 按钮是 2px outline。断言指标而非具体颜色,主题换代不脆。
  expect(commentFocus!.boxShadow, '批复输入框必须有可见焦点环').not.toBe('none');
  for (const button of [approveFocus!, rejectFocus!]) {
    expect(button.outlineStyle, '按钮焦点必须有 outline').toBe('solid');
    expect(parseFloat(button.outlineWidth), '按钮焦点 outline 至少 2px')
      .toBeGreaterThanOrEqual(2);
  }

  // 纯键盘回位到批复输入框(此时焦点停在驳回),填批复说明,再 Tab 到批准。
  for (let back = 0; back < 10; back++) {
    if ((await describeFocus(page))?.label === '批复说明') break;
    await page.keyboard.press('Shift+Tab');
  }
  expect((await describeFocus(page))?.label).toBe('批复说明');
  await page.keyboard.type('同意:材料完整,批准进入终审。');
  await page.keyboard.press('Tab');
  expect((await describeFocus(page))?.label, 'Tab 应落在批准按钮').toBe('批准');

  // Enter 触发等价点击:批准 → engine 用 FakeProvider 续跑 → 终局卡片。
  await page.keyboard.press('Enter');
  const finale = page.getByRole('region', { name: '终局总结' });
  await expect(finale, '批准后运行应续跑到终态并渲染终局卡片')
    .toBeVisible({ timeout: 60_000 });
});

test('终局卡片基线截图', async ({ page }) => {
  await page.goto(`#/runs/${manifest.done_run_id}`);
  const finale = page.getByRole('region', { name: '终局总结' });
  await expect(finale).toBeVisible();
  // 种子把 ts/duration_s 归一为固定值:卡片上的每个数字跨次稳定,截图
  // 不一致就只能是真实回归(flake 制度的前提)。
  await expect(finale).toHaveScreenshot('finale-card.png', { animations: 'disabled' });
});
