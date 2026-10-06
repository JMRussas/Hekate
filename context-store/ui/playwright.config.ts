import { defineConfig, devices } from '@playwright/test';

// HEKATE_UI_TEST_PORT isolates a test run's own Vite server (default 5179); with CI=1 an
// already-running server is never adopted, and --strictPort refuses an occupied port.
const port = Number(process.env.HEKATE_UI_TEST_PORT ?? 5179);

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL: `http://localhost:${port}`,
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: `npm run dev -- --port ${port} --strictPort`,
    url: `http://localhost:${port}`,
    reuseExistingServer: !process.env.CI,
  },
});
