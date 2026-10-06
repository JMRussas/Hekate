// Managed-plan browser (plan 021): read-only views over mocked plan-contract responses.
import { expect, test, type Page } from '@playwright/test';
import {
  L1, NA, NB, NC, ND, PHASE, PHASE_DONE, ROOT_A, ROOT_B, ROOT_BAD, ROOT_FUTURE, V, X,
  event, eventsBody, held, listBody, listItem, mockContract, openPlans, planA, planB, planBad, planWide,
} from './managed-helpers';

const LIST = listBody([
  listItem(ROOT_A, 'Plan A'), listItem(ROOT_B, 'Plan B'), listItem(ROOT_BAD, 'Bad plan'),
  listItem(ROOT_FUTURE, 'Future plan', { contractVersion: 'plan-contract/v9', supported: false }),
]);
const EMPTY_EVENTS = eventsBody([], null, null);

function standard(extra: Record<string, Parameters<typeof mockContract>[1][string]> = {}) {
  return {
    '/plans?': { body: LIST },
    [`/plans/${ROOT_A}`]: { body: planA() },
    [`/plans/${ROOT_A}/events`]: { body: eventsBody([event(1, NA), event(2, NA, 'attempt_finished')], null) },
    [`/plans/${ROOT_B}`]: { body: planB() },
    [`/plans/${ROOT_B}/events`]: { body: EMPTY_EVENTS },
    [`/plans/${ROOT_BAD}`]: { body: planBad() },
    [`/plans/${ROOT_BAD}/events`]: { body: EMPTY_EVENTS },
    [`/nodes/${L1}/events`]: { body: EMPTY_EVENTS },
    [`/nodes/${NA}/events`]: { body: eventsBody([event(1, NA)], null) },
    ...extra,
  };
}

let guard: { nonGet: string[] } | null = null;
test.afterEach(() => {
  // Read-only guard: no test may cause a non-GET API request.
  expect(guard?.nonGet ?? []).toEqual([]);
  guard = null;
});

async function setup(page: Page, routes: Parameters<typeof mockContract>[1]) {
  const m = await mockContract(page, routes);
  guard = m;
  await openPlans(page);
  return m;
}

test('lists managed plans and shows the unsupported one as metadata only', async ({ page }) => {
  const m = await setup(page, standard());
  await expect(page.getByTestId(`plan-item-${ROOT_A}`)).toContainText('Plan A');
  await expect(page.getByTestId(`plan-item-${ROOT_FUTURE}`)).toHaveAttribute('data-supported', 'false');
  await page.getByTestId(`plan-item-${ROOT_FUTURE}`).click();
  await expect(page.getByTestId('managed-plan-unsupported')).toContainText('plan-contract/v9');
  await expect(page.getByTestId('plan-tree')).toHaveCount(0);
  expect(m.requests.filter(r => r.url.includes(ROOT_FUTURE))).toEqual([]);   // no plan GET for it
});

test('a 404 list means the contract is disabled; a 500 is an error, never an empty list', async ({ page }) => {
  await setup(page, { '/plans?': { status: 404, body: '' } });
  await expect(page.getByTestId('managed-list-error')).toHaveAttribute('data-code', 'not_found');
  await expect(page.getByTestId('managed-list-error')).toContainText('not enabled');
  await expect(page.getByTestId('managed-list-empty')).toHaveCount(0);
});

test('a 500 list shows its error code', async ({ page }) => {
  await setup(page, { '/plans?': { status: 500, body: '{"code":"plan_store_error","message":"x"}' } });
  await expect(page.getByTestId('managed-list-error')).toHaveAttribute('data-code', 'plan_store_error');
  await expect(page.getByTestId('managed-list-empty')).toHaveCount(0);
});

test('list paging uses the uuid cursor and Refresh resets it', async ({ page }) => {
  const m = await setup(page, {
    '/plans?limit=100&afterRootId=': { body: listBody([listItem(ROOT_B, 'Plan B')]) },
    '/plans?limit=100': { body: listBody([listItem(ROOT_A, 'Plan A')], ROOT_A) },
  });
  await page.getByTestId('managed-list-more').click();
  await expect(page.getByTestId(`plan-item-${ROOT_B}`)).toBeVisible();
  expect(m.requests.map(r => r.url)).toContain(`/api/plan-contract/v1/plans?limit=100&afterRootId=${ROOT_A}`);
  await page.getByTestId('managed-refresh').click();
  await expect(page.getByTestId(`plan-item-${ROOT_B}`)).toHaveCount(0);
  await expect(page.getByTestId(`plan-item-${ROOT_A}`)).toBeVisible();
  expect(m.requests.filter(r => r.url === '/api/plan-contract/v1/plans?limit=100').length).toBe(2);
});

test('the tree shows API statuses verbatim, highlights stale acceptance, and expands/collapses', async ({ page }) => {
  await setup(page, standard());
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await expect(page.getByTestId(`tree-node-${NA}`)).toHaveAttribute('data-effective', 'accepted');
  await expect(page.getByTestId(`tree-node-${NA}`)).toHaveAttribute('data-work', 'done');
  await expect(page.getByTestId(`tree-node-${NB}`)).toHaveAttribute('data-ready', 'true');
  await expect(page.getByTestId(`tree-node-${NC}`)).toHaveAttribute('data-effective', 'stale');
  await expect(page.getByTestId(`tree-node-${PHASE}`)).toHaveAttribute('data-completion', 'incomplete');
  await expect(page.getByTestId(`tree-node-${L1}`)).toBeVisible();
  await page.getByTestId(`tree-toggle-${PHASE}`).click();
  await expect(page.getByTestId(`tree-node-${L1}`)).toHaveCount(0);
  await page.getByTestId(`tree-toggle-${PHASE}`).click();
  await expect(page.getByTestId(`tree-node-${L1}`)).toBeVisible();
});

test('node detail keeps blockers through the leaf and its ancestor distinct from declared edges', async ({ page }) => {
  await setup(page, standard());
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId(`tree-node-${L1}`).click();
  const rows = page.getByTestId('blocker-row');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toHaveAttribute('data-blocker', `${L1}|${X}|completed|predecessor_not_completed`);
  await expect(rows.nth(1)).toHaveAttribute('data-blocker', `${PHASE}|${X}|accepted|predecessor_not_completed`);
  await expect(rows.nth(1)).toContainText('(inherited)');
  await expect(page.getByTestId('dep-in')).toHaveCount(1);   // only X -> L1 is declared on L1 itself

  // The map draws BOTH declared edges (X -> L1 and X -> P), each blocking.
  await page.getByTestId('managed-tab-map').click();
  const edges = page.getByTestId('dependency-map').locator('[data-edge]');
  await expect(edges).toHaveCount(3);   // the fixture declares exactly 3 edges
  await expect(page.locator(`[data-edge="${X}|${L1}"]`)).toHaveAttribute('data-blocked', 'true');
  await expect(page.locator(`[data-edge="${X}|${PHASE}"]`)).toHaveAttribute('data-blocked', 'true');
  await expect(page.locator(`[data-edge="${NA}|${NB}"]`)).toHaveAttribute('data-blocked', 'false');
});

test('node detail shows raw and effective acceptance and pins', async ({ page }) => {
  await setup(page, standard());
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId(`tree-node-${NA}`).click();
  const detail = page.getByTestId('node-detail');
  await expect(detail).toHaveAttribute('data-node-id', NA);
  await expect(page.getByTestId('effective-acceptance')).toContainText('accepted');
  await expect(page.getByTestId('raw-acceptance')).toContainText('ev-1');
  await expect(detail).toContainText('d'.repeat(64));
  await expect(detail).toContainText('green');
  await expect(page.getByTestId('node-events').getByTestId('event-row')).toHaveCount(1);
});

test('map and detail label containers by their API roll-up and leaves by work and effective acceptance', async ({ page }) => {
  await setup(page, standard());
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId('managed-tab-map').click();
  await expect(page.getByTestId('map-legend')).toBeVisible();
  const line = (n: string) => page.locator(`[data-map-node="${n}"] [data-status-line]`);
  await expect(line(PHASE_DONE)).toHaveAttribute('data-status-line', 'complete · accepted');   // not the stored "todo"
  await expect(line(PHASE)).toHaveAttribute('data-status-line', 'incomplete · pending · gates do not hold');
  await expect(line(NC)).toHaveAttribute('data-status-line', 'done · stale');                 // stale is explicit, not green
  await expect(line(ND)).toHaveAttribute('data-status-line', 'done · accepted');
  await expect(line(NC)).toHaveAttribute('fill', '#fbbf24');

  await page.locator(`[data-map-node="${PHASE_DONE}"]`).click();
  await expect(page.getByTestId('container-status')).toHaveAttribute('data-completion', 'complete');
  await expect(page.getByTestId('container-status')).toHaveAttribute('data-acceptance', 'accepted');
  await expect(page.getByTestId('node-detail')).not.toContainText('Work');
  await expect(page.getByTestId('effective-acceptance')).toHaveCount(0);

  await page.locator(`[data-map-node="${NC}"]`).click();
  await expect(page.getByTestId('effective-acceptance')).toContainText('stale');
  await expect(page.getByTestId('raw-acceptance')).toContainText('accepted');   // the record itself is unchanged
  await expect(page.getByTestId('node-detail')).toContainText('e'.repeat(64));
  await expect(page.getByTestId('container-status')).toHaveCount(0);
});

test('the map layout is deterministic across loads', async ({ page }) => {
  await setup(page, standard());
  const snapshot = async () => {
    await page.getByTestId(`plan-item-${ROOT_A}`).click();
    await page.getByTestId('managed-tab-map').click();
    const nodes = await page.locator('[data-map-node]').evaluateAll(els => els.map(e => `${e.getAttribute('data-map-node')}@${e.getAttribute('data-layer')}`));
    const edges = await page.locator('[data-edge]').evaluateAll(els => els.map(e => e.getAttribute('data-edge')));
    return { nodes, edges };
  };
  const first = await snapshot();
  await page.reload();
  await page.getByRole('button', { name: 'Plans', exact: true }).click();
  expect(await snapshot()).toEqual(first);
  expect(first.nodes).toContain(`${NB}@1`);   // B is one layer after A
});

test('above 200 nodes the map falls back to the edge table', async ({ page }) => {
  await setup(page, standard({ [`/plans/${ROOT_A}`]: { body: planWide(201) } }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId('managed-tab-map').click();
  await expect(page.getByTestId('dependency-table')).toBeVisible();
  await expect(page.getByTestId('dependency-map')).toHaveCount(0);
  await expect(page.getByTestId('dependency-table').locator('[data-edge]')).toHaveCount(1);
});

test('events "load more" sends the exact checked cursor text', async ({ page }) => {
  const m = await setup(page, standard({
    [`/plans/${ROOT_A}/events?limit=100&afterSeq=`]: { body: eventsBody([event(9007199254740991, NA)], null) },
    [`/plans/${ROOT_A}/events?limit=100`]: { body: eventsBody([event(1, NA), event(2, NA)], '9007199254740990') },
  }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId('managed-tab-history').click();
  await expect(page.getByTestId('plan-events').getByTestId('event-row')).toHaveCount(2);
  await page.getByTestId('plan-events-more').click();
  await expect(page.getByTestId('plan-events').getByTestId('event-row')).toHaveCount(3);
  expect(m.requests.map(r => r.url)).toContain(`/api/plan-contract/v1/plans/${ROOT_A}/events?limit=100&afterSeq=9007199254740990`);
  await expect(page.getByTestId('plan-events-more')).toHaveCount(0);
});

test('unsafe integers fail closed in the list, a plan, and an event cursor, with no follow-up request', async ({ page }) => {
  const m = await setup(page, standard({
    '/plans?': { body: listBody([listItem(ROOT_A, 'Plan A')]).replace('"eventSeq":3', '"eventSeq":3e0') },
  }));
  await expect(page.getByTestId('managed-list-error')).toHaveAttribute('data-code', 'unsupported_number');
  await expect(page.getByTestId(`plan-item-${ROOT_A}`)).toHaveCount(0);

  await page.unrouteAll({ behavior: 'ignoreErrors' });
  const m2 = await mockContract(page, standard({
    [`/plans/${ROOT_A}`]: { body: planA({ stateRevisionOfA: '9007199254740993' }) },
    [`/plans/${ROOT_A}/events`]: { body: eventsBody([event(1, NA)], '9007199254740993') },
  }));
  guard = { nonGet: [...m.nonGet, ...m2.nonGet] };
  await page.getByTestId('managed-refresh').click();
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await expect(page.getByTestId('managed-plan-error')).toHaveAttribute('data-code', 'unsafe_integer');
  await expect(page.getByTestId('plan-tree')).toHaveCount(0);
  await expect(page.getByTestId('dependency-map')).toHaveCount(0);
  const eventRequests = m2.requests.filter(r => r.url.includes('/events'));
  expect(eventRequests.length).toBe(1);   // the unsafe cursor is never followed
});

test('an unsafe event cursor shows an error and no rows', async ({ page }) => {
  await setup(page, standard({ [`/plans/${ROOT_A}/events`]: { body: eventsBody([event(1, NA)], '9007199254740993') } }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId('managed-tab-history').click();
  await expect(page.getByTestId('plan-events-error')).toHaveAttribute('data-code', 'unsafe_integer');
  await expect(page.getByTestId('event-row')).toHaveCount(0);
});

test('switching from a good plan to an error-bearing or failing plan clears the old view', async ({ page }) => {
  await setup(page, standard({ [`/plans/${ROOT_B}`]: { status: 500, body: '{"code":"plan_store_error","message":"boom"}' } }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await expect(page.getByTestId('plan-tree')).toBeVisible();
  await page.getByTestId(`tree-node-${NA}`).click();
  await expect(page.getByTestId('node-detail')).toBeVisible();

  await page.getByTestId(`plan-item-${ROOT_BAD}`).click();
  await expect(page.getByTestId('managed-plan-error')).toHaveAttribute('data-code', 'invalid_graph');
  await expect(page.getByTestId('managed-plan-error-detail')).toContainText('invalid_state');
  await expect(page.getByTestId('plan-tree')).toHaveCount(0);
  await expect(page.getByTestId('node-detail')).toHaveCount(0);

  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await expect(page.getByTestId('plan-tree')).toBeVisible();
  await page.getByTestId(`plan-item-${ROOT_B}`).click();
  await expect(page.getByTestId('managed-plan-error')).toHaveAttribute('data-code', 'plan_store_error');
  await expect(page.getByTestId('plan-tree')).toHaveCount(0);
});

test('a contract version mismatch in a plan response is refused', async ({ page }) => {
  await setup(page, standard({ [`/plans/${ROOT_A}`]: { body: planA().split(`"contractVersion":"${V}"`).join('"contractVersion":"plan-contract/v9"') } }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await expect(page.getByTestId('managed-plan-error')).toHaveAttribute('data-code', 'unsupported_contract_version');
  await expect(page.getByTestId('plan-tree')).toHaveCount(0);
});

test('wrong-shape, wrong-root and unknown-enum plan responses fail closed and clear the old view', async ({ page }) => {
  const missingField = planA().replace('"name":"Leaf B",', '');          // a consumed field is absent
  const wrongRoot = planB();                                              // body for plan B under A's url
  const unknownReason = planA().replace('"reason":"predecessor_not_completed"', '"reason":"predecessor_on_holiday"');
  const danglingEdge = planA().replace(`"successorId":"${NB}"`, '"successorId":"00000000-0000-4000-8000-999999999999"');
  for (const [body, why] of [[missingField, 'node.name'], [wrongRoot, 'different plan'], [unknownReason, 'blocker.reason'], [danglingEdge, 'unknown node']]) {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    const m = await mockContract(page, standard({ [`/plans/${ROOT_A}`]: { body } }));
    guard = { nonGet: [...(guard?.nonGet ?? []), ...m.nonGet] };
    await openPlans(page);
    await page.getByTestId(`plan-item-${ROOT_B}`).click();
    await expect(page.getByTestId('plan-tree')).toContainText('Only leaf B');
    await page.getByTestId(`plan-item-${ROOT_A}`).click();
    await expect(page.getByTestId('managed-plan-error')).toHaveAttribute('data-code', 'unexpected_shape');
    await expect(page.getByTestId('managed-plan-error')).toContainText(why);
    await expect(page.getByTestId('plan-tree')).toHaveCount(0);
  }
});

test('node history containing another node\'s event fails closed', async ({ page }) => {
  await setup(page, standard({ [`/nodes/${NA}/events`]: { body: eventsBody([event(1, NB)], null) } }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId(`tree-node-${NA}`).click();
  await expect(page.getByTestId('node-events-error')).toHaveAttribute('data-code', 'unexpected_shape');
  await expect(page.getByTestId('node-detail').getByTestId('event-row')).toHaveCount(0);
});

for (const [kind, body] of [['unsafe', planA({ stateRevisionOfA: '9007199254740993' })], ['malformed', '{broken']] as const) {
test(`a slow ${kind} response for plan A never paints into plan B`, async ({ page }) => {
  const slowA = held({ body });
  await setup(page, standard({ [`/plans/${ROOT_A}`]: slowA.reply }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await expect(page.getByTestId('managed-plan-loading')).toBeVisible();
  await page.getByTestId(`plan-item-${ROOT_B}`).click();
  await expect(page.getByTestId('managed-plan')).toHaveAttribute('data-root-id', ROOT_B);
  slowA.release();
  await page.waitForTimeout(300);
  await expect(page.getByTestId('managed-plan')).toHaveAttribute('data-root-id', ROOT_B);
  await expect(page.getByTestId('managed-plan-error')).toHaveCount(0);
  await expect(page.getByTestId('plan-tree')).toContainText('Only leaf B');
});

}

test('a slow node history never appears under another node', async ({ page }) => {
  const slowL1 = held({ body: eventsBody([event(77, L1)], null) });
  await setup(page, standard({ [`/nodes/${L1}/events`]: slowL1.reply }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId(`tree-node-${L1}`).click();
  await page.getByTestId(`tree-node-${NA}`).click();
  await expect(page.getByTestId('node-detail')).toHaveAttribute('data-node-id', NA);
  await expect(page.getByTestId('node-events').getByTestId('event-row')).toHaveCount(1);
  slowL1.release();
  await page.waitForTimeout(300);
  await expect(page.getByTestId('node-events').locator('[data-seq="77"]')).toHaveCount(0);
  await expect(page.getByTestId('node-events').getByTestId('event-row')).toHaveCount(1);
});

for (const [kind, body] of [['unsafe', planA({ stateRevisionOfA: '9007199254740993' })], ['malformed', '{broken']] as const) {
test(`the client drops a superseded ${kind} response before parsing it (guard beyond network abort)`, async ({ page }) => {
  await setup(page, standard({ [`/plans/${ROOT_A}`]: { body } }));
  const outcome = await page.evaluate(async (root) => {
    const api = await import('/src/planContract/api.ts');
    const live = new AbortController();   // never aborted: only the acceptance check can drop it
    try {
      await api.getPlan(root, { signal: live.signal, isCurrent: () => false });
      return 'delivered';
    } catch (e) {
      return (e as Error).name + ':' + ((e as { code?: string }).code ?? '');
    }
  }, ROOT_A);
  expect(outcome).toBe('StaleResponseError:');   // not ContractApiError:unsafe_integer
});

}

test('Refresh cancels held plan and history requests; their late results are ignored', async ({ page }) => {
  const slowPlan = held({ body: '{malformed' });
  const slowEvents = held({ body: eventsBody([event(5, NA)], null) });
  await setup(page, standard({ [`/plans/${ROOT_A}`]: slowPlan.reply, [`/plans/${ROOT_A}/events`]: slowEvents.reply }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await expect(page.getByTestId('managed-plan-loading')).toBeVisible();
  await page.getByTestId('managed-refresh').click();
  await expect(page.getByText('Select a managed plan')).toBeVisible();
  slowPlan.release();
  slowEvents.release();
  await page.waitForTimeout(300);
  await expect(page.getByTestId('managed-plan')).toHaveCount(0);
  await expect(page.getByTestId('managed-plan-error')).toHaveCount(0);
  await expect(page.getByText('Select a managed plan')).toBeVisible();
  await expect(page.getByTestId(`plan-item-${ROOT_A}`)).toBeVisible();
});

test('list paging is pending while a page loads: rows kept, no second request, page appended once', async ({ page }) => {
  const page2 = held({ body: listBody([listItem(ROOT_B, 'Plan B')]) });
  const m = await setup(page, {
    '/plans?limit=100&afterRootId=': page2.reply,
    '/plans?limit=100': { body: listBody([listItem(ROOT_A, 'Plan A')], ROOT_A) },
  });
  await page.getByTestId('managed-list-more').click();
  await expect(page.getByTestId('managed-list-more')).toHaveCount(0);   // hidden while pending
  await expect(page.getByTestId(`plan-item-${ROOT_A}`)).toBeVisible();   // existing rows preserved
  page2.release();
  await expect(page.getByTestId(`plan-item-${ROOT_B}`)).toBeVisible();
  await expect(page.locator('[data-testid^="plan-item-"]')).toHaveCount(2);
  expect(m.requests.filter(r => r.url.includes('afterRootId=')).length).toBe(1);
});

test('events paging is pending while a page loads and appends exactly once', async ({ page }) => {
  const more = held({ body: eventsBody([event(3, NA)], null) });
  const m = await setup(page, standard({
    [`/plans/${ROOT_A}/events?limit=100&afterSeq=`]: more.reply,
    [`/plans/${ROOT_A}/events?limit=100`]: { body: eventsBody([event(1, NA), event(2, NA)], '2') },
  }));
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId('managed-tab-history').click();
  await page.getByTestId('plan-events-more').click();
  await expect(page.getByTestId('plan-events-more')).toHaveCount(0);
  await expect(page.getByTestId('plan-events').getByTestId('event-row')).toHaveCount(2);
  more.release();
  await expect(page.getByTestId('plan-events').getByTestId('event-row')).toHaveCount(3);
  expect(m.requests.filter(r => r.url.includes('afterSeq=')).length).toBe(1);
});

test('a failed next page clears prior rows and shows the error (list and events)', async ({ page }) => {
  await setup(page, {
    ...standard(),
    '/plans?limit=100&afterRootId=': { status: 500, body: '{"code":"plan_store_error","message":"x"}' },
    '/plans?limit=100': { body: listBody([listItem(ROOT_A, 'Plan A')], ROOT_A) },
    [`/plans/${ROOT_A}/events?limit=100&afterSeq=`]: { status: 500, body: '{"code":"plan_store_error","message":"y"}' },
    [`/plans/${ROOT_A}/events?limit=100`]: { body: eventsBody([event(1, NA)], '1') },
  });
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  await page.getByTestId('managed-tab-history').click();
  await expect(page.getByTestId('plan-events').getByTestId('event-row')).toHaveCount(1);
  await page.getByTestId('plan-events-more').click();
  await expect(page.getByTestId('plan-events-error')).toHaveAttribute('data-code', 'plan_store_error');
  await expect(page.getByTestId('event-row')).toHaveCount(0);

  await page.getByTestId('managed-list-more').click();
  await expect(page.getByTestId('managed-list-error')).toHaveAttribute('data-code', 'plan_store_error');
  await expect(page.locator('[data-testid^="plan-item-"]')).toHaveCount(0);
});

test('every managed view is read-only (GET only)', async ({ page }) => {
  const m = await setup(page, standard());
  await page.getByTestId(`plan-item-${ROOT_A}`).click();
  for (const n of [L1, NA, NB, NC, X, PHASE]) await page.getByTestId(`tree-node-${n}`).click();
  await page.getByTestId('managed-tab-map').click();
  await page.locator(`[data-map-node="${NB}"]`).click();
  await page.getByTestId('managed-tab-history').click();
  await page.getByTestId(`plan-item-${ROOT_B}`).click();
  await page.getByTestId(`plan-item-${ROOT_FUTURE}`).click();
  await page.getByTestId('managed-refresh').click();
  await expect(page.getByTestId(`plan-item-${ROOT_A}`)).toBeVisible();
  expect(m.requests.filter(r => r.url.startsWith('/api/plan-contract/')).every(r => r.method === 'GET')).toBe(true);
  expect(m.requests.filter(r => r.url.startsWith('/api/plan-contract/')).length).toBeGreaterThan(5);
});
