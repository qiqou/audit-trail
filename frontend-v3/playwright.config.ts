import { defineConfig } from "@playwright/test";
import path from "node:path";

const projectRoot = path.resolve(process.cwd(), "..");
const python = process.env.E2E_PYTHON ?? "python";
const port = process.env.E2E_PORT ?? "18100";
const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    headless: true,
    screenshot: "only-on-failure",
    ...(executablePath ? { launchOptions: { executablePath } } : {}),
  },
  webServer: {
    // 端到端测试直接启动 ASGI 应用，不占用桌面版的单实例锁，避免与使用中的
    // 本地审计项目互相影响；仍使用与正式版相同的 V3 静态资源和 API 路由。
    command: `"${python}" -m uvicorn backend.main:app --host 127.0.0.1 --port ${port}`,
    cwd: projectRoot,
    url: `http://127.0.0.1:${port}/`,
    timeout: 30_000,
    reuseExistingServer: false,
    env: {
      ...process.env,
      AUDIT_ASSISTANT_PORT: port,
      AUDIT_ASSISTANT_WEBVIEW: "0",
      AUDIT_ASSISTANT_FRONTEND: "v3",
    },
  },
});
