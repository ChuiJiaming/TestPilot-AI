import { defineConfig } from "@playwright/test";

const executablePath = process.env.TESTPILOT_CHROMIUM_EXECUTABLE;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  reporter: "list",
  use: {
    baseURL: process.env.TESTPILOT_E2E_BASE_URL ?? "http://localhost:5173",
    browserName: "chromium",
    launchOptions: executablePath ? { executablePath } : {},
  },
});
