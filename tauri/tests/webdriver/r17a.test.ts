/**
 * r17a — audit scenario S17a: packaged voiceit binary boot smoke.
 *
 * Drives the REAL packaged voiceit binary through tauri-driver and asserts
 * the renderer reaches a state where #root contains the app shell. This is
 * the LOWER half of the surface-proxy remediation; S17b (r17b.test.ts,
 * follow-up) covers the IPC round-trip.
 *
 * Prerequisites verified by setup.ts before each test — fails loud with a
 * clear install-hint if anything is missing.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { startSession } from './setup';

describe('S17a — packaged voiceit binary boot smoke', () => {
  let cleanup: (() => Promise<void>) | null = null;

  afterEach(async () => {
    if (cleanup) {
      const c = cleanup;
      cleanup = null;
      await c();
    }
  });

  it('launches and renders the app shell into #root', async () => {
    const session = await startSession();
    cleanup = session.cleanup;
    const { browser } = session;

    // Wait for the renderer's #root to exist and contain at least one child.
    // The Tauri webview boots, loads the bundled index.html, runs main.tsx
    // which mounts <App> via createRoot(document.getElementById('root')!).
    // 20s gives the OS time to bring up webkit, then loading the bundled
    // JS (~2MB) and the first React render — comfortable headroom over the
    // ~3-5s real-world boot on the pockeo-linux agents.
    const root = await browser.$('#root');
    await root.waitForExist({ timeout: 20_000 });
    // getHTML returns the element's inner HTML. Non-empty proves React
    // mounted at least one component into #root — a renderer that crashed
    // during init would leave #root empty.
    const html = await root.getHTML({ includeSelectorTag: false });
    expect(html.length).toBeGreaterThan(0);
  });
});
