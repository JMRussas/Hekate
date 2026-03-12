// Playwright test: Planner tab renders plan list and tree
// Usage: node tools/test_ui_planner.js
//
// Verifies:
//   1. Planner tab exists and is clickable
//   2. Plan list loads with at least one plan
//   3. Plan cards show name, type badge, status badge
//   4. Clicking a plan loads the tree view
//   5. Tree view shows phases and steps with expand/collapse
//   6. Back button returns to list
//
// Depends on: API running on 5102, UI on 5179, seeded DB with Plan 005

const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const results = [];

  const pass = (name) => { results.push({ name, ok: true }); console.log(`  PASS: ${name}`); };
  const fail = (name, detail) => { results.push({ name, ok: false, detail }); console.log(`  FAIL: ${name} — ${detail}`); };

  console.log('--- Planner Tab Tests ---\n');

  // 1. Load UI
  console.log('1. Loading UI...');
  await page.goto('http://localhost:5179', { waitUntil: 'load' });
  await page.waitForTimeout(500);

  // 2. Click Planner tab
  console.log('2. Clicking Planner tab...');
  const plannerTab = page.locator('button:has-text("Planner")');
  if (await plannerTab.isVisible()) {
    await plannerTab.click();
    await page.waitForTimeout(300);
    pass('Planner tab visible and clickable');
  } else {
    fail('Planner tab visible and clickable', 'Tab not found');
    await browser.close();
    return;
  }

  // 3. Wait for plan list to load
  console.log('3. Waiting for plans to load...');
  try {
    await page.waitForSelector('text=Planner View', { timeout: 5000 });
    pass('Plan 005 "Planner View" appears in list');
  } catch {
    fail('Plan 005 "Planner View" appears in list', 'Not found within 5s');
    // Dump what we see
    const text = await page.locator('aside').last().innerText();
    console.log(`   Sidebar content: ${text.substring(0, 200)}`);
    await browser.close();
    return;
  }

  // 4. Check plan card has badges
  console.log('4. Checking plan card badges...');
  const featureBadge = await page.locator('span:has-text("feature")').count();
  const statusBadge = await page.locator('span:has-text("completed")').count();
  if (featureBadge > 0) pass('Feature type badge present');
  else fail('Feature type badge present', `Found ${featureBadge}`);
  if (statusBadge > 0) pass('Status badge present');
  else fail('Status badge present', `Found ${statusBadge}`);

  // 5. Click into Plan 005
  console.log('5. Clicking Plan 005...');
  await page.locator('button:has-text("Planner View")').click();
  await page.waitForTimeout(1000);

  // 6. Check tree view loaded
  console.log('6. Checking tree view...');
  const backButton = page.getByRole('button', { name: '← Plans' });
  if (await backButton.isVisible()) pass('Back button visible');
  else fail('Back button visible', 'Not found');

  // Check for phases
  const executePhase = await page.locator('text=EXECUTE').count();
  const gatherPhase = await page.locator('text=GATHER').count();
  if (executePhase > 0 && gatherPhase > 0) pass('Phase nodes rendered (EXECUTE, GATHER)');
  else fail('Phase nodes rendered', `EXECUTE: ${executePhase}, GATHER: ${gatherPhase}`);

  // Check for steps (under EXECUTE, should be expanded by default at depth < 2)
  const stepText = await page.locator('text=Add new node types').count();
  if (stepText > 0) pass('Plan steps visible (depth 1 expanded)');
  else fail('Plan steps visible', 'Step nodes not found — may not be expanded');

  // Check for milestone
  const milestone = await page.locator('text=Plan contract validated').count();
  if (milestone > 0) pass('Milestone node visible');
  else fail('Milestone node visible', 'Not found');

  // Check for risk
  const risk = await page.locator('text=Scope creep').count();
  if (risk > 0) pass('Risk node visible');
  else fail('Risk node visible', 'Not found');

  // 7. Back to list
  console.log('7. Testing back navigation...');
  await backButton.click();
  await page.waitForTimeout(300);
  const plansHeader = await page.locator('text=Plans').first().isVisible();
  if (plansHeader) pass('Back to plan list works');
  else fail('Back to plan list works', 'Plans header not visible');

  // --- Summary ---
  const passed = results.filter(r => r.ok).length;
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n--- RESULTS: ${passed} passed, ${failed} failed ---`);
  console.log(passed === results.length ? '\nOVERALL: PASS' : '\nOVERALL: FAIL');

  await browser.close();
  process.exit(failed > 0 ? 1 : 0);
})();
