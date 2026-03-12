// Playwright test: verify tool-use (inter-model routing)
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  const errors = [];
  const consoleLogs = [];
  page.on('console', (msg) => {
    consoleLogs.push(`[${msg.type()}] ${msg.text()}`);
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

  console.log('2. Sending inter-model message...');
  const input = page.locator('input[type="text"]');
  await input.fill('@haiku Tell sonnet to say hello and report back what it said');
  await page.locator('button:has-text("Send")').click();

  console.log('3. Waiting for stream + tool-use...');

  // Watch for tool-related phase text
  let sawCallingTool = false;
  const checkInterval = setInterval(async () => {
    try {
      const text = await page.locator('.space-y-4').innerText();
      if (text.includes('Calling')) sawCallingTool = true;
    } catch {}
  }, 500);

  try {
    await page.waitForFunction(() => {
      const text = document.body.innerText;
      return !text.includes('Parsing message') &&
             !text.includes('Classifying intent') &&
             !text.includes('Generating response') &&
             !text.includes('Calling') &&
             !text.includes('Extracting ideas') &&
             !text.includes('Assembling context');
    }, { timeout: 90000 });
  } catch (e) {
    console.log(`   Timeout: ${e.message}`);
  }
  clearInterval(checkInterval);

  await page.waitForTimeout(2000);

  // Check results
  console.log('4. Checking final state...');
  const messageElements = await page.locator('.space-y-4 > div').all();
  console.log(`   Message elements: ${messageElements.length}`);

  for (let i = 0; i < Math.min(messageElements.length, 5); i++) {
    const text = await messageElements[i].innerText();
    const truncated = text.replace(/\n/g, ' ').substring(0, 150);
    console.log(`   [${i}] ${truncated}`);
  }

  const allText = await page.locator('.space-y-4').innerText();
  const mentionsSonnet = allText.toLowerCase().includes('sonnet') || allText.toLowerCase().includes('hello');

  console.log(`\n--- RESULTS ---`);
  console.log(`Saw "Calling tool" phase: ${sawCallingTool ? 'YES' : 'NO'}`);
  console.log(`Response mentions sonnet/hello: ${mentionsSonnet ? 'YES' : 'NO'}`);
  console.log(`Messages: ${messageElements.length} (expect >= 2)`);
  console.log(`JS errors: ${errors.length}`);
  errors.forEach(e => console.log(`   ERROR: ${e.substring(0, 200)}`));

  const pass = messageElements.length >= 2 && errors.length === 0;
  console.log(`\nOVERALL: ${pass ? 'PASS' : 'FAIL'}`);

  await browser.close();
})();
