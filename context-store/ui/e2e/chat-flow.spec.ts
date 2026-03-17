// Tests for the core chat flow: sending messages, receiving SSE responses, model selection

import { test, expect } from '@playwright/test';
import { mockBaseAPIs, mockChatAPI, mockConversationAPIs } from './helpers';

test.beforeEach(async ({ page }) => {
  await mockBaseAPIs(page);
  await page.addInitScript(() => localStorage.clear());
});

test('can send a message and see the response', async ({ page }) => {
  const convId = 'e2e-conv-1';
  await mockChatAPI(page, 'Hello! How can I help you today?', { conversationId: convId });
  await mockConversationAPIs(page, convId);

  await page.goto('/');

  const input = page.getByPlaceholder('Type @ to pick a model');
  await input.fill('hi');
  await page.getByRole('button', { name: 'Send' }).click();

  // User message appears
  await expect(page.getByText('hi').first()).toBeVisible();

  // AI response appears (wait for streaming to complete)
  await expect(page.getByText('Hello! How can I help you today?')).toBeVisible({ timeout: 5000 });
});

test('send button is disabled when input is empty', async ({ page }) => {
  await page.goto('/');

  const sendButton = page.getByRole('button', { name: 'Send' });
  await expect(sendButton).toBeDisabled();
});

test('Enter key sends the message', async ({ page }) => {
  const convId = 'e2e-conv-2';
  await mockChatAPI(page, 'Got it.', { conversationId: convId });
  await mockConversationAPIs(page, convId);

  await page.goto('/');

  const input = page.getByPlaceholder('Type @ to pick a model');
  await input.fill('test message');
  await input.press('Enter');

  // User message should appear
  await expect(page.getByText('test message').first()).toBeVisible();
});

test('shows model autocomplete when typing @', async ({ page }) => {
  await page.goto('/');

  const input = page.getByPlaceholder('Type @ to pick a model');
  await input.fill('@');

  // Autocomplete dropdown should appear — check for the hint text at the bottom
  await expect(page.getByText('Tab or Enter to select')).toBeVisible();
  // Check specific model entries in the dropdown buttons
  await expect(page.getByRole('button', { name: /Sonnet.*@sonnet/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Opus.*@opus/ })).toBeVisible();
});

test('autocomplete filters as you type', async ({ page }) => {
  await page.goto('/');

  const input = page.getByPlaceholder('Type @ to pick a model');
  await input.fill('@son');

  // Should show sonnet, not opus
  await expect(page.getByRole('button', { name: /Sonnet.*@sonnet/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Opus.*@opus/ })).not.toBeVisible();
});

test('Tab selects autocomplete option', async ({ page }) => {
  await page.goto('/');

  const input = page.getByPlaceholder('Type @ to pick a model');
  await input.fill('@son');
  await input.press('Tab');

  // Input should now contain the full @sonnet mention
  await expect(input).toHaveValue(/@sonnet /);
  // Autocomplete should close
  await expect(page.getByText('Tab or Enter to select')).not.toBeVisible();
});

test('Escape closes autocomplete', async ({ page }) => {
  await page.goto('/');

  const input = page.getByPlaceholder('Type @ to pick a model');
  await input.fill('@');
  await expect(page.getByText('Tab or Enter to select')).toBeVisible();

  await input.press('Escape');
  await expect(page.getByText('Tab or Enter to select')).not.toBeVisible();
});

test('shows speaker badges for user and model messages', async ({ page }) => {
  const convId = 'e2e-conv-3';
  await mockChatAPI(page, 'Response text', { conversationId: convId, model: 'sonnet' });
  await mockConversationAPIs(page, convId);

  await page.goto('/');

  const input = page.getByPlaceholder('Type @ to pick a model');
  await input.fill('hello');
  await page.getByRole('button', { name: 'Send' }).click();

  // User badge
  await expect(page.locator('span.bg-blue-600:has-text("user")')).toBeVisible();

  // Model badge appears after response
  await expect(page.locator('span.bg-purple-600:has-text("sonnet")')).toBeVisible({ timeout: 5000 });
});

test('shows streaming phase indicator while generating', async ({ page }) => {
  // Use a route handler that delays the response to catch the streaming state
  await page.route('**/api/chat', async route => {
    // Delay to let the test observe the streaming UI
    await new Promise(r => setTimeout(r, 500));
    route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
      body: [
        'event: conversation_id\ndata: {"id":"slow-conv"}\n\n',
        'event: model\ndata: {"provider":"anthropic","model":"sonnet","display":"sonnet"}\n\n',
        'event: token\ndata: {"text":"thinking..."}\n\n',
        'event: done\ndata: {}\n\n',
      ].join(''),
    });
  });
  await mockConversationAPIs(page, 'slow-conv');

  await page.goto('/');

  const input = page.getByPlaceholder('Type @ to pick a model');
  await input.fill('what is this?');
  await page.getByRole('button', { name: 'Send' }).click();

  // Should eventually show the response
  await expect(page.getByText('thinking...')).toBeVisible({ timeout: 5000 });
});
