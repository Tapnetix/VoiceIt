// NOTE: Phase-C E2E reality — the /books route is not wired into the web build
// until C16, so per-task E2E specs (c6–c15) are authored RED and only go green
// at the phase-end E2E gate (orchestrator boots the live stack via `just dev-web`).
// C1 just needs `bunx playwright test --list` to parse the config without error.
//
// Two distinct test surfaces are configured via the E2E_SURFACE env var:
//   - `web` (default)  — runs c*.spec.ts against the dev web build at :5173.
//                        This is the original Phase-C harness. Surface-proxy
//                        per the audit (tests don't exercise the real Tauri
//                        IPC path) but is what we ship today.
//   - `tauri`          — runs r*.spec.ts (real-Tauri specs S17a/S17b/S18-S21)
//                        against the packaged Tauri binary under tauri-driver.
//                        Linux only (tauri-driver doesn't support macOS as
//                        of 2026-Q2). Requires WebKitWebDriver + tauri-driver
//                        installed on the host — see app/e2e/fixtures-tauri.ts
//                        for the dependency contract.
import { defineConfig } from '@playwright/test';

const SURFACE = process.env.E2E_SURFACE ?? 'web';
const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:5173';

const webConfig = {
  testDir: 'e2e',
  testMatch: /c\d+\.spec\.ts$|s\d+-acceptance\.spec\.ts$|d\d+\.spec\.ts$/,
  globalSetup: './e2e/global-setup.ts',
  use: { baseURL: BASE_URL, trace: 'on-first-retry' as const },
  webServer: {
    command: 'just dev-web',
    url: BASE_URL,
    reuseExistingServer: true,
    timeout: 120_000,
  },
};

// `tauri` surface — runs r*.spec.ts against the packaged Tauri binary via
// tauri-driver. No webServer (the packaged app is launched per-test by
// fixtures-tauri.ts). No baseURL (Tauri's IPC has no http origin).
const tauriConfig = {
  testDir: 'e2e',
  testMatch: /r\d+[a-z]?\.spec\.ts$/,
  use: { trace: 'on-first-retry' as const },
  timeout: 60_000,
};

export default defineConfig(SURFACE === 'tauri' ? tauriConfig : webConfig);
