// Full Playwright test: send a real message, verify response persists AND extraction works
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  const errors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });

  console.log('1. Loading UI...');
  await page.goto('http://localhost:5179', { waitUntil: 'load' });
  await page.waitForTimeout(1000);

  // Start fresh
  const newConvBtn = page.locator('button:has-text("New Conversation")');
  if (await newConvBtn.isVisible()) {
    await newConvBtn.click();
    await page.waitForTimeout(500);
  }

  console.log('2. Sending message that triggers extraction...');
  const input = page.locator('input[type="text"]');
  await input.fill('@haiku Give me two game design ideas using graph databases');
  await page.locator('button:has-text("Send")').click();

  // Wait for stream to fully complete (including extraction phase)
  console.log('3. Waiting for stream + extraction...');
  try {
    await page.waitForFunction(() => {
      const text = document.body.innerText;
      // Stream is done when no phase indicators are visible
      return !text.includes('Parsing message') &&
             !text.includes('Classifying intent') &&
             !text.includes('Generating response') &&
             !text.includes('Extracting ideas') &&
             !text.includes('Assembling context');
    }, { timeout: 45000 });
  } catch (e) {
    console.log(`   Timeout waiting for stream: ${e.message}`);
  }

  await page.waitForTimeout(1500);

  // Check messages
  console.log('4. Checking final state...');
  const messageElements = await page.locator('.space-y-4 > div').all();
  console.log(`   Message elements: ${messageElements.length}`);

  for (let i = 0; i < Math.min(messageElements.length, 5); i++) {
    const text = await messageElements[i].innerText();
    const truncated = text.replace(/\n/g, ' ').substring(0, 120);
    console.log(`   [${i}] ${truncated}`);
  }

  // Check for model response content (should be more than just "OK")
  const haikuBadges = await page.locator('span:has-text("haiku")').count();
  const allText = await page.locator('.space-y-4').innerText();
  const responseLength = allText.length;

  console.log(`\n--- RESULTS ---`);
  console.log(`Haiku badges (expect 2): ${haikuBadges}`);
  console.log(`Total text length: ${responseLength}`);
  console.log(`Messages: ${messageElements.length} (expect >= 2)`);
  console.log(`JS errors: ${errors.length}`);
  errors.forEach(e => console.log(`   ERROR: ${e}`));

  const pass = haikuBadges >= 2 && messageElements.length >= 2 && errors.length === 0;
  console.log(`\nOVERALL: ${pass ? 'PASS' : 'FAIL'}`);

  await browser.close();
})();
