// Attempt traces in the managed-plan browser (trace contract v0, rev 2), over mocked responses.
import { expect, test, type Page } from '@playwright/test';
import { NA, NB, ROOT_A, V, event, eventsBody, held, listBody, listItem, mockContract, openPlans, planA } from './managed-helpers';

const T = (attemptId: string, query = 'limit=200') => `/nodes/${NA}/attempts/${attemptId}/trace?${query}`;

function traceBody(o: Record<string, unknown> = {}) {
  return JSON.stringify({
    contractVersion: V, nodeId: NA, attemptId: 'a1', attemptEpoch: 1, claimKey: 'a1', status: 'exited', reason: null,
    integrity: 'verified', executionKind: 'claude-cli', exit: { code: 0, killReason: null },
    prompt: { text: 'Edit README.md only.', bytes: 20 }, records: [], nextAfterSeq: null, capped: false, ...o,
  });
}
const rec = (seq: number, line: object | string, stream = 'stdout') =>
  ({ seq, tMs: seq * 5, stream, text: typeof line === 'string' ? line : JSON.stringify(line), cut: false, redacted: false });

const CLAUDE = [
  rec(0, { type: 'system', subtype: 'init', model: 'sonnet' }),
  rec(1, { type: 'assistant', message: { content: [{ type: 'text', text: 'Adding the section.' }, { type: 'tool_use', id: 't1', name: 'Edit', input: { file_path: 'README.md' } }] } }),
  rec(2, { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: 'ok' }] } }),
  rec(3, { type: 'result', subtype: 'success', is_error: false, num_turns: 3 }),
];

const NODE_EVENTS = eventsBody([
  event(1, NA), { ...event(2, NA, 'attempt_finished'), workFrom: 'in_progress', workTo: 'done' },
  { ...event(3, NA, 'decision_recorded'), decision: 'accepted' },
], null);

function routes(extra: Parameters<typeof mockContract>[1] = {}) {
  return {
    '/plans?': { body: listBody([listItem(ROOT_A, 'Plan A')]) },
    [`/plans/${ROOT_A}`]: { body: planA() },
    [`/plans/${ROOT_A}/events`]: { body: eventsBody([], null, null) },
    [`/nodes/${NA}/events`]: { body: NODE_EVENTS },
    [`/nodes/${NB}/events`]: { body: eventsBody([], null, null) },
    ...extra,
  };
}

let guard: { nonGet: string[] } | null = null;
test.afterEach(() => {
  expect(guard?.nonGet ?? []).toEqual([]);   // read-only: no non-GET API request
  guard = null;
});

async function openAttempt(page: Page, r: Parameters<typeof mockContract>[1], attemptId = 'a1') {
  const m = await mockContract(page, r);
  guard = m;
  await openPlans(page);
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId(`tree-node-${NA}`).click();
  await page.locator(`[data-testid="attempt-row"][data-attempt-id="${attemptId}"]`).click();
  return m;
}

test('a finished, verified trace shows prompt and normalized items; acceptance stays the node decision', async ({ page }) => {
  const m = await openAttempt(page, routes({ [T('a1')]: { body: traceBody({ records: CLAUDE }) } }));
  const trace = page.getByTestId('attempt-trace');
  await expect(page.getByTestId('trace-observation-limits')).toContainText('Worker liveness: unknown');
  await expect(page.getByTestId('trace-observation-limits')).toContainText('Useful progress: unknown');
  await expect(trace).toHaveAttribute('data-status', 'exited');
  await expect(trace).toHaveAttribute('data-integrity', 'verified');
  await expect(page.getByTestId('trace-status')).toHaveText('Attempt finished; trace matches its recorded hash.');
  await expect(page.getByTestId('trace-prompt')).toContainText('Prompt (20 bytes)');
  const items = page.getByTestId('trace-item');
  await expect(items).toHaveCount(5);
  expect(await items.evaluateAll(els => els.map(e => e.getAttribute('data-role')))).toEqual(['system', 'assistant', 'tool_call', 'tool_result', 'result']);
  await expect(items.nth(3)).toContainText('tool result · Edit');
  await expect(page.getByTestId('trace-header')).not.toContainText('accepted');
  await expect(page.getByTestId('effective-acceptance')).toContainText('accepted');
  await expect(page.getByTestId('attempt-row')).toHaveAttribute('data-selected', 'true');
  expect(m.requests.map(r => r.url)).toContain(`/api/plan-contract/v1${T('a1')}`);
  // Layout: the conversation is in the wide central workspace; the sidebar keeps only the selector.
  await expect(page.getByTestId('trace-workspace').getByTestId('attempt-trace')).toBeVisible();
  await expect(page.getByTestId('node-detail').getByTestId('attempt-trace')).toHaveCount(0);
  await expect(page.getByTestId('node-detail').getByTestId('attempt-row')).toHaveCount(1);
  const [main, side] = await Promise.all([page.getByTestId('trace-workspace').boundingBox(), page.getByTestId('node-detail').boundingBox()]);
  expect(main!.width).toBeGreaterThan(side!.width);
});

test('not_captured states say no trace was recorded, with the reason, and show no items', async ({ page }) => {
  await openAttempt(page, routes({ [T('a1')]: { body: traceBody({ status: 'not_captured', reason: 'no_trace_block', integrity: 'none', exit: null, prompt: null, claimKey: null, executionKind: null }) } }));
  await expect(page.getByTestId('trace-status')).toHaveText('No trace was recorded for this attempt.');
  await expect(page.getByTestId('trace-item')).toHaveCount(0);
  await expect(page.getByTestId('trace-prompt')).toHaveCount(0);
});

test('a compacted journal is reported as such', async ({ page }) => {
  await openAttempt(page, routes({ [T('a1')]: { body: traceBody({ status: 'not_captured', reason: 'journal_compacted', integrity: 'none', exit: null }) } }));
  await expect(page.getByTestId('trace-status')).toHaveText('No trace is available: the attempt’s journal records were compacted.');
});

test('an attempt in progress keeps its cursor at the last record and can be checked again', async ({ page }) => {
  const running = traceBody({ status: 'running', integrity: 'unverified', exit: null, records: CLAUDE.slice(0, 2) });
  const later = traceBody({ prompt: null, records: CLAUDE.slice(2) });
  const m = await openAttempt(page, routes({ [T('a1')]: { body: running }, [T('a1', 'limit=200&afterSeq=1')]: { body: later } }));
  await expect(page.getByTestId('trace-status')).toHaveText('Attempt in progress; trace unverified.');
  await expect(page.getByTestId('trace-observation-limits')).toContainText('Worker liveness: unknown');
  await expect(page.getByTestId('trace-more')).toHaveCount(0);
  await page.getByTestId('trace-check').click();
  await expect(page.getByTestId('attempt-trace')).toHaveAttribute('data-status', 'exited');
  await expect(page.getByTestId('trace-item')).toHaveCount(5);
  await expect(page.getByTestId('trace-prompt')).toContainText('Edit README.md only.');   // kept from the first page
  await expect(page.getByTestId('trace-check')).toHaveCount(0);
  expect(m.requests.map(r => r.url)).toContain(`/api/plan-contract/v1${T('a1', 'limit=200&afterSeq=1')}`);
});

test('an empty follow-up page keeps the cursor: no restart, no repeated prompt or records', async ({ page }) => {
  const running = traceBody({ status: 'running', integrity: 'unverified', exit: null, records: CLAUDE.slice(0, 2) });
  const nothingYet = traceBody({ status: 'running', integrity: 'unverified', exit: null, prompt: null, records: [] });
  const m = await openAttempt(page, routes({ [T('a1')]: { body: running }, [T('a1', 'limit=200&afterSeq=1')]: { body: nothingYet } }));
  await expect(page.getByTestId('trace-item')).toHaveCount(3);
  await page.getByTestId('trace-check').click();
  await expect(page.getByTestId('trace-check')).toBeEnabled();
  await page.getByTestId('trace-check').click();
  await expect(page.getByTestId('trace-check')).toBeVisible();
  await expect(page.getByTestId('trace-item')).toHaveCount(3);
  await expect(page.getByTestId('trace-prompt')).toContainText('Edit README.md only.');
  const urls = m.requests.map(r => r.url);
  expect(urls.filter(u => u === `/api/plan-contract/v1${T('a1')}`).length).toBe(1);   // never restarted
  expect(urls.filter(u => u === `/api/plan-contract/v1${T('a1', 'limit=200&afterSeq=1')}`).length).toBe(2);
});

test('Load more follows the page cursor', async ({ page }) => {
  const first = traceBody({ records: CLAUDE.slice(0, 1), nextAfterSeq: 0 });
  const rest = traceBody({ prompt: null, records: CLAUDE.slice(1) });
  await openAttempt(page, routes({ [T('a1')]: { body: first }, [T('a1', 'limit=200&afterSeq=0')]: { body: rest } }));
  await page.getByTestId('trace-more').click();
  await expect(page.getByTestId('trace-item')).toHaveCount(5);
  await expect(page.getByTestId('trace-more')).toHaveCount(0);
});

test('an integrity mismatch is an explicit error with no trace content', async ({ page }) => {
  await openAttempt(page, routes({ [T('a1')]: { status: 409, body: '{"code":"TRACE_INTEGRITY_MISMATCH","message":"sha differs"}' } }));
  await expect(page.getByTestId('attempt-trace-error')).toHaveAttribute('data-code', 'TRACE_INTEGRITY_MISMATCH');
  await expect(page.getByTestId('attempt-trace-error')).toContainText('does not match its recorded hash');
  await expect(page.getByTestId('trace-item')).toHaveCount(0);
});

test('responses that contradict the contract fail closed', async ({ page }) => {
  await openAttempt(page, routes({ [T('a1')]: { body: traceBody({ status: 'running', integrity: 'verified', exit: null }) } }));
  await expect(page.getByTestId('attempt-trace-error')).toHaveAttribute('data-code', 'unexpected_shape');
  await expect(page.getByTestId('trace-item')).toHaveCount(0);
});

test('a trace for a different attempt is refused', async ({ page }) => {
  await openAttempt(page, routes({ [T('a1')]: { body: traceBody({ attemptId: 'other' }) } }));
  await expect(page.getByTestId('attempt-trace-error')).toHaveAttribute('data-code', 'unexpected_shape');
});

test('worker text is rendered as text, never as HTML; long items expand on request', async ({ page }) => {
  const html = '<img src=x onerror="window.__traceXss=1"><b>bold</b>';
  const long = 'y'.repeat(25_000);
  await openAttempt(page, routes({ [T('a1')]: { body: traceBody({ records: [
    rec(0, { type: 'assistant', message: { content: [{ type: 'text', text: html }] } }),
    rec(1, long, 'stderr'),
  ] }) } }));
  const trace = page.getByTestId('attempt-trace');
  await expect(trace).toContainText('<b>bold</b>');
  await expect(trace.locator('img')).toHaveCount(0);
  await expect(trace.locator('b')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __traceXss?: number }).__traceXss)).toBeUndefined();
  await expect(page.getByTestId('trace-expand')).toHaveText('Show all (25000 characters)');
  await page.getByTestId('trace-expand').click();
  await expect(page.getByTestId('trace-expand')).toHaveText('Show less');
});

test('Reload trace refetches the selected attempt from the start; changing node clears it', async ({ page }) => {
  let n = 0;
  const m = await openAttempt(page, routes({
    [T('a1')]: async () => ({ body: traceBody({ records: n++ === 0 ? CLAUDE.slice(0, 1) : CLAUDE }) }),
  }));
  await expect(page.getByTestId('trace-item')).toHaveCount(1);
  await page.getByTestId('trace-reload').click();
  await expect(page.getByTestId('trace-item')).toHaveCount(5);
  await expect(page.getByTestId('attempt-row')).toHaveAttribute('data-selected', 'true');
  expect(m.requests.filter(r => r.url === `/api/plan-contract/v1${T('a1')}`).length).toBe(2);
  await page.getByTestId(`tree-node-${NB}`).click();
  await expect(page.getByTestId('attempt-trace')).toHaveCount(0);
});

test('a superseded attempt selection never paints', async ({ page }) => {
  const slow = held({ body: traceBody({ records: CLAUDE.slice(0, 1) }) });
  const events = eventsBody([event(1, NA), { ...event(2, NA), attemptId: 'a2', attemptEpoch: 2 }], null);
  await openAttempt(page, routes({
    [`/nodes/${NA}/events`]: { body: events },
    [T('a1')]: slow.reply,
    [T('a2')]: { body: traceBody({ attemptId: 'a2', attemptEpoch: 2, records: CLAUDE }) },
  }));
  await page.locator('[data-testid="attempt-row"][data-attempt-id="a2"]').click();
  await expect(page.getByTestId('attempt-trace')).toHaveAttribute('data-attempt-id', 'a2');
  slow.release();
  await page.waitForTimeout(300);
  await expect(page.getByTestId('attempt-trace')).toHaveAttribute('data-attempt-id', 'a2');
  await expect(page.getByTestId('trace-item')).toHaveCount(5);
});

test('Refresh clears the selected attempt with every other selection', async ({ page }) => {
  await openAttempt(page, routes({ [T('a1')]: { body: traceBody({ records: CLAUDE }) } }));
  await expect(page.getByTestId('attempt-trace')).toBeVisible();
  await page.getByTestId('managed-refresh').click();
  await expect(page.getByTestId('attempt-trace')).toHaveCount(0);
  await expect(page.getByTestId('node-detail')).toHaveCount(0);
});
