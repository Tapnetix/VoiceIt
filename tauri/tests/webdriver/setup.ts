/**
 * Tauri WebDriver E2E setup — spawns tauri-driver, hands tests a configured
 * webdriverio session pointed at the packaged voiceit binary.
 *
 * See ./README.md for the dependency contract.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { remote, type Browser } from 'webdriverio';

const TAURI_DRIVER_PORT = Number.parseInt(process.env.TAURI_DRIVER_PORT ?? '4444', 10);

const WORKTREE_ROOT = path.resolve(__dirname, '..', '..', '..');
const DEFAULT_BIN = path.join(WORKTREE_ROOT, 'tauri', 'src-tauri', 'target', 'release', 'voiceit');

function locate(...candidates: Array<string | undefined>): string | null {
  for (const c of candidates) {
    if (c && existsSync(c)) return c;
  }
  return null;
}

export function locateWebKitWebDriver(): string | null {
  return locate(
    process.env.WEBKITWEBDRIVER_PATH,
    '/usr/bin/WebKitWebDriver',
    '/usr/local/bin/WebKitWebDriver',
  );
}

export function locateTauriDriver(): string | null {
  const home = process.env.HOME ?? '/root';
  return locate(
    process.env.TAURI_DRIVER_PATH,
    path.join(home, '.cargo', 'bin', 'tauri-driver'),
    '/usr/local/cargo/bin/tauri-driver',
  );
}

export function locateTauriBinary(): string | null {
  return locate(process.env.TAURI_BIN, DEFAULT_BIN);
}

export function assertSetup(): { wkwd: string; driver: string; binary: string } {
  const wkwd = locateWebKitWebDriver();
  const driver = locateTauriDriver();
  const binary = locateTauriBinary();
  if (!wkwd || !driver || !binary) {
    const missing = [
      !wkwd ? 'WebKitWebDriver (apt install webkit2gtk-driver)' : null,
      !driver ? 'tauri-driver (cargo install tauri-driver --locked)' : null,
      !binary ? `packaged voiceit binary (tried ${process.env.TAURI_BIN ?? DEFAULT_BIN})` : null,
    ].filter(Boolean).join('; ');
    throw new Error(
      `Tauri WebDriver setup incomplete — missing: ${missing}. ` +
      'See tauri/tests/webdriver/README.md for install instructions.',
    );
  }
  return { wkwd, driver, binary };
}

/**
 * Spawn tauri-driver and return both the process and a configured
 * webdriverio browser session. Caller is responsible for calling `cleanup()`
 * to terminate the driver and end the session.
 */
export async function startSession(): Promise<{
  browser: Browser;
  cleanup: () => Promise<void>;
}> {
  const { wkwd, driver, binary } = assertSetup();

  const driverProc: ChildProcess = spawn(
    driver,
    ['--port', String(TAURI_DRIVER_PORT), '--native-driver', wkwd],
    {
      env: { ...process.env, TAURI_BIN: binary },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );

  // Wait for tauri-driver to bind the port (it logs to stderr on bind).
  await new Promise<void>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('tauri-driver did not bind within 5s')), 5_000);
    const done = () => { clearTimeout(t); resolve(); };
    driverProc.stderr?.once('data', done);
    driverProc.stdout?.once('data', done);
    // Belt-and-suspenders timeout fallback — bind is fast even if no output.
    setTimeout(done, 1_000);
  });

  const browser = await remote({
    hostname: '127.0.0.1',
    port: TAURI_DRIVER_PORT,
    capabilities: {
      browserName: 'wry',
      'tauri:options': { application: binary },
    } as never, // wry/tauri caps not in webdriverio's stock types
    logLevel: 'warn',
  });

  return {
    browser,
    cleanup: async () => {
      try { await browser.deleteSession(); } catch { /* ignore */ }
      driverProc.kill('SIGTERM');
      await new Promise((r) => setTimeout(r, 200));
    },
  };
}
