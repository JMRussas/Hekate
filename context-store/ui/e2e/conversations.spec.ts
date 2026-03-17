// Tests for conversation management: list, select, new conversation

import { test, expect } from '@playwright/test';
import { mockBaseAPIs, mockConversationAPIs } from './helpers';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.clear());
});

test('shows conversation list from API', async ({ page }) => {
  await page.route('**/api/events', route =>
    route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
      body: 'event: ping\ndata: {}\n\n',
    }),
  );

  await page.route('**/api/conversations', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        { id: 'conv-1', name: 'First chat', createdAt: '2026-03-16T00:00:00Z', turnCount: 5 },
        { id: 'conv-2', name: 'Second chat', createdAt: '2026-03-16T01:00:00Z', turnCount: 3 },
      ]),
    }),
  );

  await page.goto('/');

  await expect(page.getByText('First chat')).toBeVisible();
  await expect(page.getByText('Second chat')).toBeVisible();
});

test('selecting a conversation loads its messages', async ({ page }) => {
  await page.route('**/api/events', route =>
    route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
      body: 'event: ping\ndata: {}\n\n',
    }),
  );

  await page.route('**/api/conversations', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        { id: 'conv-load', name: 'Load me', createdAt: '2026-03-16T00:00:00Z', turnCount: 2 },
      ]),
    }),
  );

  // Mock the conversation detail endpoint with existing messages
  await page.route('**/api/conversation/conv-load', route => {
    if (route.request().url().includes('/debug')) return route.fulfill({ status: 200, body: 'null' });
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 'conv-load',
        name: 'Load me',
        turns: [
          { id: 'msg-1', speaker: 'user', content: 'What is Hekate?', createdAt: '2026-03-16T00:00:00Z' },
          { id: 'msg-2', speaker: 'sonnet', content: 'Hekate is a context store.', createdAt: '2026-03-16T00:00:01Z' },
        ],
      }),
    });
  });

  await page.route('**/api/conversation/conv-load/debug', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: 'null' }),
  );

  await page.route('**/api/threads/conv-load', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }),
  );

  await page.route('**/api/stats/conv-load', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ conversationId: 'conv-load', turnCount: 2, ideaCount: 0, questionCount: 0, parkedCount: 0 }),
    }),
  );

  await page.route('**/api/permissions/conv-load', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ defaultLevel: 2, defaultLabel: 'Assist', modelOverrides: {}, systemDefault: 2 }),
    }),
  );

  await page.goto('/');

  // Click the conversation
  await page.getByText('Load me').click();

  // Should show the loaded messages
  await expect(page.getByText('What is Hekate?')).toBeVisible({ timeout: 5000 });
  await expect(page.getByText('Hekate is a context store.')).toBeVisible();
});

test('New Conversation resets to empty state', async ({ page }) => {
  await mockBaseAPIs(page);

  // Start with a saved conversation ID
  await page.addInitScript(() => localStorage.setItem('conversationId', 'old-conv'));
  await mockConversationAPIs(page, 'old-conv');

  await page.goto('/');

  // Click New Conversation
  await page.getByRole('button', { name: 'New Conversation' }).click();

  // Should show empty state
  await expect(page.getByText('Start a conversation')).toBeVisible();
});
