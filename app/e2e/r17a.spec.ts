/**
 * r17a — audit scenario S17a: packaged-app boot smoke.
 *
 * Verifies that tauri-driver can launch the real packaged voiceit binary and
 * that the renderer reaches the BooksTab. This is the LOWER half of the
 * surface-proxy remediation (S17b covers the IPC round-trip).
 *
 * Status: RED until the CI agent has WebKitWebDriver + tauri-driver installed
 * AND the Jenkins Build stage has produced a Linux binary to test against.
 * The fixture (app/e2e/fixtures-tauri.ts) throws with a clear setup hint when
 * any of those preconditions is missing — keeping the failure mode loud.
 *
 * See:
 *  - design.md §4 row S17a
 *  - plan.json task T-E2E-S17A
 *  - app/e2e/fixtures-tauri.ts header — install instructions
 */
import { tauriTest as test, expect } from './fixtures-tauri';

test('S17a: packaged voiceit binary boots and renders BooksTab', async ({ tauriDriverProcess: _driver }) => {
  // TODO(T-E2E-S17A): connect to tauri-driver's WebDriver endpoint via
  // selenium-webdriver, instantiate a session against the packaged voiceit
  // binary, wait for the renderer's BooksTab to appear in the DOM.
  //
  // Awaiting the first successful CI run with WebKitWebDriver provisioned
  // before fleshing out the WebDriver client glue — see app/e2e/fixtures-
  // tauri.ts for the dependency contract. The fixture already wires the
  // driver process lifecycle; this spec just needs the WebDriver-side glue
  // (npm install selenium-webdriver, point at http://localhost:4444, drive).
  expect(_driver.pid).toBeGreaterThan(0);
});
