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
      env: {
        ...process.env,
        TAURI_BIN: binary,
        // Spec: any panic on the Rust side (in tauri-driver itself, in the
        // wry/tao stack, or in our crate) should print a backtrace so we
        // can actually debug crashes — without this `tauri-driver` swallows
        // the binary's stderr silently.
        RUST_BACKTRACE: '1',
        // WebKitGTK telemetry / a11y bus probes can hang the WebView in
        // headless containers. Skip them.
        WEBKIT_DISABLE_COMPOSITING_MODE: '1',
        WEBKIT_DISABLE_DMABUF_RENDERER: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );

  // Tee tauri-driver's stderr/stdout to our own so panics + GTK warnings
  // from the launched binary surface in the vitest log. Without this the
  // failure modes are "session creation hangs" / "invalid session id" with
  // zero diagnostic — which is exactly what made the first few Jenkins
  // iterations expensive.
  driverProc.stdout?.on('data', (b: Buffer) => process.stdout.write(`[tauri-driver] ${b}`));
  driverProc.stderr?.on('data', (b: Buffer) => process.stderr.write(`[tauri-driver] ${b}`));

  // Wait for tauri-driver to bind the port (it logs to stderr on bind).
  await new Promise<void>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('tauri-driver did not bind within 5s')), 5_000);
    const done = () => { clearTimeout(t); resolve(); };
    driverProc.stderr?.once('data', done);
    driverProc.stdout?.once('data', done);
    // Belt-and-suspenders timeout fallback — bind is fast even if no output.
    setTimeout(done, 1_000);
  });

  // tauri-driver capability shape per upstream Tauri docs:
  // https://tauri.app/develop/tests/webdriver/example/webdriverio/
  // browserName is intentionally OMITTED — the driver picks the right
  // WebKit-side driver based on tauri:options.application. The webdriverio
  // v9 type bundle doesn't model tauri-specific caps; the cast bypasses it.
  const browser = await remote({
    hostname: '127.0.0.1',
    port: TAURI_DRIVER_PORT,
    capabilities: {
      'tauri:options': { application: binary },
    } as never,
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
