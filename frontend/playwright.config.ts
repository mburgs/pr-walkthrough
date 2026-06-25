import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for pr-walkthrough e2e.
 *
 * Tests run against the Vite dev server with MSW intercepting all backend
 * calls — no Python backend / Claude / TTS needed. CI-friendly.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,           // single dev server; tests share state in MSW
  workers: 1,
  retries: 0,
  reporter: [["list"]],

  use: {
    baseURL: "http://localhost:5191",
    trace: "retain-on-failure",
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
  },

  webServer: {
    command: "npm run dev -- --port 5191 --strictPort",
    url: "http://localhost:5191/",
    reuseExistingServer: false,
    timeout: 60_000,
    // No VITE_BACKEND_URL → MSW activates and intercepts all /sessions/* calls.
  },

  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
