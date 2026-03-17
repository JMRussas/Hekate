// Tests for the main app layout: header, panels, view switching

import { test, expect } from '@playwright/test';
import { mockBaseAPIs } from './helpers';

test.beforeEach(async ({ page }) => {
  await mockBaseAPIs(page);
  // Clear localStorage so we start fresh (no saved conversation)
  await page.addInitScript(() => localStorage.clear());
});

test('renders header with title and view switcher', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Ideation Assistant' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Chat' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Workspace' })).toBeVisible();
});

test('shows empty state with model hints', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Start a conversation')).toBeVisible();
  await expect(page.getByText('Type @ to pick a model')).toBeVisible();
});

test('shows New Conversation button in chat view', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'New Conversation' })).toBeVisible();
});

test('can switch between Chat and Workspace views', async ({ page }) => {
  // Mock workspace API
  await page.route('**/api/nodes/roots', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );

  await page.goto('/');

  // Start in chat view
  await expect(page.getByText('Start a conversation')).toBeVisible();

  // Switch to workspace
  await page.getByRole('button', { name: 'Workspace' }).click();
  await expect(page.getByText('Start a conversation')).not.toBeVisible();

  // Switch back
  await page.getByRole('button', { name: 'Chat' }).click();
  await expect(page.getByText('Start a conversation')).toBeVisible();
});

test('right panel has Context, Debug, and Planner tabs', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Context' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Debug' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Planner' })).toBeVisible();
});
