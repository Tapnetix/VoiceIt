# Tauri WebDriver E2E Suite

Real-Tauri-app E2E tests that drive the packaged `voiceit` binary through
`tauri-driver` (WebKit-WebDriver protocol on Linux). This is the audit's
remediation for the HIGH `surface-proxy` finding — the existing
`app/e2e/c*.spec.ts` Playwright suite targets the dev web build at
`localhost:5173`, NOT the packaged Tauri app, so production code paths that
call `@tauri-apps/api/core` invoke / `@tauri-apps/api/event` listen / Tauri
plugin-fs cannot execute in that environment.

## What's here

| File | Purpose |
|---|---|
| `setup.ts` | Spawns `tauri-driver` against `WebKitWebDriver`, returns a configured `webdriverio` browser session pointed at the packaged binary. |
| `vitest.config.ts` | Standalone vitest project (Node env, no jsdom). Doesn't interfere with the app/ vitest suite. |
| `r17a.test.ts` | Scenario S17a — packaged-app boot smoke. |
| `r17b.test.ts` | Scenario S17b — Tauri IPC round-trip (`invoke('ping')` returns `'pong'`). |
| `r18.test.ts` | Scenario S18 — DictateWindow flow. |
| `r19.test.ts` | Scenario S19 — AccessibilityGate flow. |
| `r20.test.ts` | Scenario S20 — InputMonitoringGate flow. |
| `r21.test.ts` | Scenario S21 — CapturesTab flow. |

Only `r17a.test.ts` ships with this commit. Others are follow-up tasks
(see `plan.json`).

## System dependencies (Linux only)

- `WebKitWebDriver` — `/usr/bin/WebKitWebDriver` (provided by the
  `webkit2gtk-driver` apt package). Already installed on the
  `pockeo-linux` Jenkins agents (verified 2026-06-26).
- `tauri-driver` — installed via `cargo install tauri-driver --locked`
  into `$HOME/.cargo/bin/`. The Jenkinsfile installs it lazily.
- A packaged `voiceit` binary at `tauri/src-tauri/target/release/voiceit`.
  Produced by the Build stage (Linux branch). The vitest config picks it
  up from there by default; override via `TAURI_BIN` env.

`tauri-driver` does NOT support macOS as of 2026-Q2 — these specs are
Linux-only. Tauri-side acceptance scenarios on macOS rely on the
unit-test layer (`@tauri-apps` mocked) plus a manual local-dev check at
release time. design.md §4 records this.

## Running locally

```bash
# Build the binary first if it's not already there:
cd tauri && bun run tauri build --bundles deb < /dev/null

# Then run the suite:
just test-e2e-tauri
# OR:
cd tauri/tests/webdriver && bun x vitest run
```

If `WebKitWebDriver` or `tauri-driver` is absent, the setup throws with a
clear install-hint (failing loud rather than silently faking the Tauri
runtime, which would itself be a `surface-proxy` finding).

## CI

Jenkinsfile Build/Linux stage has an appended `Verify: tauri-e2e` step
that runs this suite against the binary it just built. The step is
non-fatal initially (`|| true`) so the first integration run surfaces
its real failure mode in the build log without flipping the whole
pipeline red — once it's known-green, the `|| true` comes out.
