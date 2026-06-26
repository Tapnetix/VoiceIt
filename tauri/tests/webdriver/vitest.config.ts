/**
 * Vitest config for the Tauri WebDriver E2E suite. Standalone — does NOT
 * inherit from app/vitest.config.ts (different env, different scope).
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['r*.test.ts'],
    // Each test launches a real Tauri binary + tauri-driver process. They
    // can't share a session (Tauri is single-window for our use). Run serial.
    fileParallelism: false,
    // 60s per test — boot + driver handshake is ~2-5s, UI work the rest.
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
});
