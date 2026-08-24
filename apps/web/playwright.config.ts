import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:3001", browserName: "chromium" },
  webServer: [
    {
      command: "uv run uvicorn agentrank_api.main:create_app --factory --port 8001",
      cwd: "../..",
      url: "http://127.0.0.1:8001/health",
      reuseExistingServer: false,
    },
    {
      command: "AGENTRANK_API_BASE_URL=http://127.0.0.1:8001 pnpm start --port 3001",
      cwd: ".",
      url: "http://127.0.0.1:3001/login",
      reuseExistingServer: false,
    },
  ],
});
