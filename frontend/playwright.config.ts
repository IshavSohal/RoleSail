import { defineConfig } from '@playwright/test'

const python = process.env.APPLYPILOT_E2E_PYTHON ?? 'python'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:8766',
    browserName: 'chromium',
    headless: true,
    launchOptions: {
      executablePath: process.env.CHROME_PATH,
      args: process.env.CHROME_PATH ? ['--no-sandbox'] : [],
    },
  },
  webServer: {
    command: `${python} -m applypilot dashboard --no-open --port 8766`,
    url: 'http://127.0.0.1:8766',
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      APPLYPILOT_DIR: '/tmp/applypilot-playwright',
    },
  },
})
