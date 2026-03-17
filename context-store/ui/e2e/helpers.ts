// Shared helpers for mocking the context-store API in Playwright tests

import { type Page } from '@playwright/test';

/** Build an SSE response body from a sequence of event/data pairs */
export function buildSSE(events: { event: string; data: unknown }[]): string {
  return events.map(e => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join('');
}

/** Standard SSE sequence for a complete chat response */
export function chatResponseSSE(opts: {
  conversationId?: string;
  model?: string;
  provider?: string;
  intent?: string;
  responseText: string;
}) {
  const convId = opts.conversationId ?? 'test-conv-1';
  const events: { event: string; data: unknown }[] = [
    { event: 'conversation_id', data: { id: convId } },
    { event: 'debug_parse', data: { originalMessage: 'test', mention: opts.model ?? '@sonnet', cleanedMessage: 'test', model: { provider: opts.provider ?? 'anthropic', model: opts.model ?? 'sonnet', display: opts.model ?? 'sonnet' }, durationMs: 1 } },
    { event: 'intent', data: { intent: opts.intent ?? 'general_chat', confidence: 'high', pattern: null } },
    { event: 'model', data: { provider: opts.provider ?? 'anthropic', model: opts.model ?? 'sonnet', display: opts.model ?? 'sonnet' } },
  ];

  // Emit tokens word by word
  for (const word of opts.responseText.split(' ')) {
    events.push({ event: 'token', data: { text: word + ' ' } });
  }

  events.push({ event: 'done', data: {} });
  return buildSSE(events);
}

/** Mock all the API endpoints that fire on page load / conversation select */
export async function mockBaseAPIs(page: Page) {
  // Conversations list
  await page.route('**/api/conversations', route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    }),
  );

  // SSE events endpoint — return empty stream that stays open
  await page.route('**/api/events', route =>
    route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
      body: 'event: ping\ndata: {}\n\n',
    }),
  );
}

/** Mock the chat endpoint to return a canned SSE response */
export async function mockChatAPI(page: Page, responseText: string, opts?: {
  conversationId?: string;
  model?: string;
  provider?: string;
}) {
  await page.route('**/api/chat', route =>
    route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
      body: chatResponseSSE({ responseText, ...opts }),
    }),
  );
}

/** Mock conversation load APIs (for when a conversation ID exists) */
export async function mockConversationAPIs(page: Page, conversationId: string) {
  await page.route(`**/api/conversation/${conversationId}`, route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: conversationId, name: 'Test Conversation', turns: [] }),
    }),
  );

  await page.route(`**/api/threads/${conversationId}`, route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [] }),
    }),
  );

  await page.route(`**/api/stats/${conversationId}`, route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ conversationId, turnCount: 0, ideaCount: 0, questionCount: 0, parkedCount: 0 }),
    }),
  );

  await page.route(`**/api/conversation/${conversationId}/debug`, route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(null),
    }),
  );

  await page.route(`**/api/permissions/${conversationId}`, route => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ defaultLevel: 2, defaultLabel: 'Assist', modelOverrides: {}, systemDefault: 2 }),
      });
    }
    return route.continue();
  });
}
