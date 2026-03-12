// Quick Playwright test: send a message and verify the response doesn't disappear
// Usage: npx playwright test tools/test_ui_message.js (or node with playwright)

const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  // Collect console logs
  const logs = [];
  page.on('console', (msg) => logs.push(`[${msg.type()}] ${msg.text()}`));

  console.log('1. Loading UI...');
  await page.goto('http://localhost:5179', { waitUntil: 'load' });

  // Clear any existing conversation by clicking "New Conversation"
  const newConvBtn = page.locator('button:has-text("New Conversation")');
  if (await newConvBtn.isVisible()) {
    await newConvBtn.click();
    await page.waitForTimeout(500);
  }

  console.log('2. Typing message...');
  const input = page.locator('input[type="text"]');
  await input.fill('@haiku Say OK in one word');

  console.log('3. Sending message...');
  await page.locator('button:has-text("Send")').click();

  // Wait for user message to appear
  await page.waitForTimeout(500);

  // Count messages before streaming completes
  const userMsgCount = await page.locator('text=Say OK in one word').count();
  console.log(`4. User message visible: ${userMsgCount > 0 ? 'YES' : 'NO'}`);

  // Wait for streaming card to appear then disappear (stream complete)
  console.log('5. Waiting for stream to complete...');
  try {
    // Wait for the streaming card (has "Generating response..." or phase text)
    await page.waitForSelector('text=Parsing message', { timeout: 5000 }).catch(() => {});

    // Now wait for it to disappear (stream done)
    await page.waitForFunction(() => {
      const text = document.body.innerText;
      return !text.includes('Parsing message') &&
             !text.includes('Classifying intent') &&
             !text.includes('Generating response') &&
             !text.includes('Extracting ideas');
    }, { timeout: 30000 });
  } catch (e) {
    console.log(`   Stream wait error: ${e.message}`);
  }

  // Give React a moment to re-render
  await page.waitForTimeout(1000);

  // Now check what's visible
  console.log('6. Checking final state...');

  // Get all message containers
  const messageElements = await page.locator('.space-y-4 > div').all();
  console.log(`   Total message elements: ${messageElements.length}`);

  for (let i = 0; i < messageElements.length; i++) {
    const text = await messageElements[i].innerText();
    const truncated = text.replace(/\n/g, ' ').substring(0, 100);
    console.log(`   Message ${i}: ${truncated}`);
  }

  // Specifically check for model response
  const allText = await page.locator('.space-y-4').innerText();
  const hasUserMsg = allText.includes('Say OK in one word');
  const hasModelResponse = allText.includes('OK') && messageElements.length >= 2;

  // Check if any haiku badge exists (model response was added)
  const haikuBadges = await page.locator('span:has-text("haiku")').count();

  console.log(`\n--- RESULTS ---`);
  console.log(`User message present: ${hasUserMsg ? 'PASS' : 'FAIL'}`);
  console.log(`Model response present: ${hasModelResponse ? 'PASS' : 'FAIL'}`);
  console.log(`Haiku badges: ${haikuBadges}`);
  console.log(`Message count: ${messageElements.length} (expected >= 2)`);

  if (hasUserMsg && hasModelResponse) {
    console.log(`\nOVERALL: PASS - Message persists after streaming`);
  } else {
    console.log(`\nOVERALL: FAIL - Message disappeared!`);

    // Dump page content for debugging
    console.log(`\n--- PAGE TEXT ---`);
    console.log(allText.substring(0, 500));
  }

  // Print any relevant console logs from the browser
  const relevantLogs = logs.filter(l => l.includes('Stream') || l.includes('error') || l.includes('Error'));
  if (relevantLogs.length > 0) {
    console.log(`\n--- BROWSER CONSOLE ---`);
    relevantLogs.forEach(l => console.log(`   ${l}`));
  }

  await browser.close();
})();
