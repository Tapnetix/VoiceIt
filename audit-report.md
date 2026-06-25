# VoiceIt — Audit Report (re-audit 2026-06-25)

Re-audit using `tcoder:audit-testing-coverage` 1.23.0. The previous audit (1.17.0, June 19) closed coverage gaps inside a curated whitelist of files and produced 91.92% JS / 95.10% Python / 100% Rust-pure-logic with all 12 acceptance scenarios written. This re-audit applies the **new real-surface verification lens** the 1.23.0 skill adds (proxy/contract detection), against `main @ 5cda008`.

The bottom line is that the *measured* numbers look healthy but a meaningful share of user-facing surfaces is verified through proxies or not at all. Concretely:

1. The vitest coverage scope is a 7-pattern allow-list covering ~13% of the app's 193 TS/TSX source files. The 91.92% number is "of the measured files," not "of the app."
2. Five Rust source files (582 of 660 coverable lines, all hardware/OS IO modules) have **zero** coverage. The 100% number was "100% of the curated pure-logic subset."
3. Four `@tauri-apps`–calling components (`DictateWindow`, `AccessibilityGate`, `InputMonitoringGate`, `CapturesTab`) have **no tests at all** — neither unit, nor acceptance, nor E2E.
4. The Playwright E2E specs (c6–c15) run against the dev web build at `localhost:5173`, **not** the packaged Tauri app. Tauri IPC paths are inherently unreachable through that surface.
5. The JS↔Python API client has a generated source of truth (`scripts/generate-api.sh`, openapi-typescript-codegen) but **no CI step** asserts the committed `app/src/lib/api/` matches the live backend's `/openapi.json`. Drift is silent.

---

## 1. Per-stack coverage

### js — vitest + istanbul

- Tool: `bun x vitest run --coverage` (provider: istanbul; existing CI invocation).
- E2E runner: Playwright (`app/playwright.config.ts`; specs at `app/e2e/c*.spec.ts`).
- **Measured coverage: 91.92% statements** (1832/1993).
- **Measured scope: 25 files** (vitest.config.ts `coverage.include` allow-list — 7 patterns under `BooksTab/`, `VoiceProfiles/`, `AudioTrimmer/`, `useBooks*`, `booksStore.ts`, `lib/utils/audio.ts`, `useReferenceTranscript.ts`).
- **App total: 193 TS/TSX source files** → measurement covers ~13% of the source tree by file count.

Ascending gap list (measured files only):

| File | Coverage |
|---|---|
| `app/src/components/VoiceProfiles/ProfileForm.tsx`      | 80.9%  |
| `app/src/components/BooksTab/VoiceEditor.tsx`           | 85.82% |
| `app/src/components/BooksTab/AnalysisProgress.tsx`      | 88.88% |
| `app/src/components/VoiceProfiles/SampleUpload.tsx`     | 89.18% |
| `app/src/components/BooksTab/BooksTab.tsx`              | 90.9%  |
| `app/src/components/BooksTab/ChapterEditor.tsx`         | 91.41% |
| `app/src/components/BooksTab/BookOverview.tsx`          | 92.12% |
| `app/src/components/AudioTrimmer/AudioTrimmer.tsx`      | 93.53% |
| `app/src/components/BooksTab/AudiobookExport.tsx`       | 95.38% |
| `app/src/components/VoiceProfiles/ProfileList.tsx`      | 95.45% |
| `app/src/components/BooksTab/BookLibrary.tsx`           | 96%    |
| `app/src/components/VoiceProfiles/SampleList.tsx`       | 96.93% |
| `app/src/components/BooksTab/BookImport.tsx`            | 97.72% |
| `app/src/components/VoiceProfiles/ProfileCard.tsx`      | 97.77% |
| 11 more files at 98%+                                   | …      |

Files **outside** the measured scope (not in any coverage report at all): `DictateWindow`, `AccessibilityGate`, `InputMonitoringGate`, `CapturesTab`, `Effects/`, `EffectsTab/`, `ModelsTab/`, `History/`, `MainEditor/`, `AppFrame/`, `AudioStudio/`, `AudioTab/`, `Generation/`, `ChordPicker/`, `AudioPlayer/`, `CapturePill/`, all stores other than `booksStore`, all hooks other than `useBooks*`/`useReferenceTranscript`, all routes/pages, all of `lib/api/`, all `services/`.

### python — pytest-cov

- Tool: `pytest --cov=backend --cov-config=backend/pyproject.toml` (existing CI invocation).
- E2E runner: none (backend integration is exercised by pytest's HTTP tests against the in-process FastAPI app — no separate E2E framework).
- **Coverage: 95.10%** (7823/8226 statements). 1765 passed, 1 xfailed, 7m 10s.
- Measured scope: 88 backend modules.

Ascending gap list (bottom 15):

| File | Coverage |
|---|---|
| `backend/routes/health.py`                  | 80.00% |
| `backend/services/book_analysis.py`         | 81.50% |
| `backend/utils/hf_offline_patch.py`         | 81.94% |
| `backend/services/llm.py`                   | 83.33% |
| `backend/services/book_generation.py`       | 84.58% |
| `backend/services/book_regenerate.py`       | 85.32% |
| `backend/routes/book_regenerate.py`         | 85.71% |
| `backend/utils/capture_chords.py`           | 85.71% |
| `backend/routes/book_export.py`             | 86.00% |
| `backend/routes/models.py`                  | 86.80% |
| `backend/services/llm_structured.py`        | 88.88% |
| `backend/services/ingestion.py`             | 89.04% |
| `backend/services/literary_analysis.py`     | 89.21% |
| `backend/services/book_events.py`           | 89.47% |
| `backend/routes/generations.py`             | 90.41% |

Note: `backend/tests/test_profile_duplicate_names.py` is excluded via `--ignore=` in CI; this should be revisited or the test should be fixed/deleted.

### rust — cargo-tarpaulin

- Tool: `cargo tarpaulin --out Json` against `tauri/src-tauri/` with empty sidecar stubs (same pattern as CI).
- E2E runner: none / not applicable (no Rust-driven E2E; the Tauri-app E2E story is the Playwright row above + the `surface-proxy` finding).
- **Whole-crate coverage: 11.82%** (78/660 coverable lines).
- The previously-reported "Rust pure-logic 100%" is **100% of a curated subset**, not 100% of the crate.

Per-file breakdown:

| File | Lines covered / coverable | Note |
|---|---|---|
| `src/key_codes.rs`                | 60/60   = **100%** | pure logic |
| `src/audio_capture/mod.rs`        | 9/9     = **100%** | trait + dispatch |
| `src/focus_capture.rs`            | 4/4     = **100%** | pure logic |
| `src/keyboard_layout.rs`          | 3/3     = **100%** | pure logic |
| `src/synthetic_keys.rs`           | 2/2     = **100%** | pure logic |
| `src/audio_capture/linux.rs`      | 0/184   = **0%**   | system audio capture |
| `src/audio_output.rs`             | 0/243   = **0%**   | playback to devices |
| `src/clipboard.rs`                | 0/12    = **0%**   | arboard read/write |
| `src/hotkey_monitor.rs`           | 0/75    = **0%**   | global hotkey listener |
| `src/speak_monitor.rs`            | 0/68    = **0%**   | TTS playback monitor |
| `src/accessibility.rs`            | 0 coverable        | empty / platform-cfg |
| `src/audio_capture/macos.rs`      | 0 coverable        | not built on Linux |
| `src/audio_capture/windows.rs`    | 0 coverable        | not built on Linux |
| `src/input_monitoring.rs`         | 0 coverable        | platform-cfg |
| `src/lib.rs`                      | 0 coverable        | command-handler glue |

Total uncovered: **582 lines** across 5 hardware/OS-IO modules.

---

## 2. Surface inventory

### Structured vs Under-structured

`structured` here = wireframe under `.claude/tcoder/.../wireframes/` OR a machine-readable contract (OpenAPI / JSON Schema). Acceptance scenarios in a session's `design.md` alone do **not** make a surface `structured` per protocol §3 — they are reviewer-grade derivative artifacts, not the contract itself.

UI screens (user-facing entry points):

| Surface | Wireframe / contract | Tests | Classification |
|---|---|---|---|
| **BooksTab** (S1–S15: import → analyze → cast → assign → generate → export → read-along) | wireframes at `.claude/tcoder/2026-05-28-audiobook-mode/wireframes/00-library.html` through `06-export.html` (10 files) | vitest acceptance s1/s2/s5/s6/s11 + 30+ component tests | **structured** |
| **VoiceProfiles** (CRUD profiles, sample upload, clone-from-sample) | wireframes at `.claude/tcoder/2026-06-02-voice-clone-trimmer/wireframes/` + `2026-06-03-clone-transcript-autofill/wireframes/` | 11+ component tests | **structured** |
| **AudioTrimmer** | covered by VoiceProfiles wireframes (`04b-voice-clone.html`) | unit + integration | **structured** |
| **DictateWindow** (dictation workflow, calls `invoke()`/`emit()`/`listen()`) | none | **none** | **under-structured** |
| **AccessibilityGate** (macOS accessibility permission, calls `invoke()`/`listen()`) | none | **none** | **under-structured** |
| **InputMonitoringGate** (macOS input-monitoring permission, calls `invoke()`) | none | **none** | **under-structured** |
| **CapturesTab** (capture log, calls `listen()`/`save()`/`writeFile()`/`writeTextFile()`) | none | **none** | **under-structured** |
| **EffectsTab** / **Effects** (audio effects mgmt) | none | scattered | **under-structured** |
| **ModelsTab** (model selection / download UI) | none | none | **under-structured** |
| **History** (generation history UI) | none | none | **under-structured** |
| **AudioStudio** / **AudioTab** | none | none | **under-structured** |

(Internal composition components — `AppFrame`, `MainEditor` — are intentionally omitted from this table; they're routing/layout, not user-facing entry-point surfaces. Their coverage is captured under §1's "Files outside measured scope".)

HTTP API boundary (split per reviewer feedback):

| Surface | Wireframe / contract | Tests | Classification |
|---|---|---|---|
| **HTTP routes (server-side, FastAPI)** — 22 modules under `backend/routes/` | OpenAPI auto-generated by FastAPI at `/openapi.json` | `pytest backend/tests/test_routes_*.py` (high coverage) | **structured** |
| **HTTP API client (JS) — `app/src/lib/api/`** | generated from the server-side OpenAPI by `scripts/generate-api.sh`, but no CI drift check ⇒ effectively unverified at the boundary | `app/src/lib/api/__tests__/booksClient.test.ts` (5 unit tests, mocks `globalThis.fetch`) | **under-structured** (no conformance check; see §2 contract-unverified finding) |

Adjacent sub-projects (not in the primary audit scope, listed for completeness):

| Path | Purpose | Coverage today |
|---|---|---|
| `web/` | `@voiceit/web` — standalone Vite web build of the app (own `package.json`, `vite.config.ts`, `dist/`) | no coverage tooling, no tests; the Playwright specs in `app/e2e/` target dev server at `http://localhost:5173` which is this build |
| `landing/` | Next.js marketing site (separate `package.json`) | no coverage tooling, no tests |
| `tools/` | one shell script (`audit-verify-targets.sh`) | n/a (executable script) |
| `scripts/` | build/dev tooling; contains `test_download_progress.py` which is **not** in `backend/tests/` and not collected by pytest — misnamed if a real test, should be moved or renamed |

### Real-surface verification status (1.23.0 lens)

Run-time scan signals:
- `scan-proxies` (strict): **0 findings** — no `__TAURI_INTERNALS__` or `installTauriShim` usage anywhere in the codebase. The only `__mocks__/` file (`app/src/__mocks__/virtual-changelog.ts`) is a Vite plugin stub, not a Tauri shim. Explicitly verified to forestall a re-audit later.
- `scan-proxies --loose`: **13 findings** — all `vi.mock('@/lib/api/client', ...)` in component tests. See classification below.

| Finding | Classification (protocol taxonomy) | Severity |
|---|---|---|
| **Playwright E2E targets dev-web at `localhost:5173`, not the packaged Tauri app.** Specs c6–c15 boot via `just dev-web` (which serves the `web/` Vite app) and run inside a browser. Production code paths that call `@tauri-apps/api/core` (`invoke`), `@tauri-apps/api/event` (`emit`/`listen`), or `@tauri-apps/plugin-fs` cannot execute in that environment — scenarios "covered" by these specs verify the dev-web proxy, not the real packaged artifact. | `surface-proxy` | HIGH |
| **JS↔Python API contract has a generated source of truth but no CI drift check.** `scripts/generate-api.sh` boots the live backend, curls `/openapi.json`, runs `openapi-typescript-codegen` → `app/src/lib/api/`. The script is invoked manually (`bun run generate:api` / `just generate-api`). The Jenkinsfile does NOT run it, and there is no test that asserts the committed `app/src/lib/api/` matches what the current backend emits. After a backend route signature change, the committed JS client and all 13 component mocks remain self-consistent against the *old* generated types while the integrated call drifts silently. | `contract-unverified` (partial — has SoT, missing conformance check) | MEDIUM |
| **Four `@tauri-apps`-calling components have zero tests** — `DictateWindow`, `AccessibilityGate`, `InputMonitoringGate`, `CapturesTab`. This is **not** a `surface-proxy` finding (the protocol's definition requires coverage *through* a proxy); it is a plain coverage gap on user-facing surfaces, classified as `unverified-surface` (custom flag, not protocol taxonomy). Listed in §2's surface inventory as `under-structured` with no tests. | `unverified-surface` (custom) | HIGH |
| **13 component tests mock `@/lib/api/client`.** Standard React pattern; mocks use the generated types. Acceptable as unit-test mechanics — the *risk* is the contract-drift finding above. Not a separate `surface-proxy` finding because these are unit tests, not surface/scenario verifications (per protocol §4 the loose flag is "fine for unit tests, but a proxy when it is a scenario's only verification"). | (informational) | LOW |
| **Vitest coverage `include` allow-list is a narrow 7-pattern subset.** Coverage doesn't measure files added since the previous audit, and doesn't measure any of the "under-structured" surfaces in the table above. The 91.92% number creates false confidence. | (scope) | HIGH |
| **Rust coverage is reported as "100%" but is 100% of a curated pure-logic subset only.** Whole-crate is 11.82%. Hardware/OS-IO modules (582 lines) are entirely unverified. cargo-tarpaulin doesn't exercise the real audio hardware, but there is also no fake-backed test for these modules. | (scope) | HIGH |
| **xfailed test masking a real bug.** `backend/tests/test_routes_profiles.py:653` is `@pytest.mark.xfail(strict=True)` documenting U-py-012 from the prior audit: `backend/routes/profiles.py:307`'s catch-all `except Exception` swallows the explicit `HTTPException(404, 'Profile not found')` raised at line 290 and re-raises it as a 500 with `detail='404: Profile not found'`. The xfail makes CI pass while the bug remains. This belongs in Phase 2 as a bug-fix task. | `suppressed-signal` (custom — not protocol taxonomy) (test "passes" by being excluded from real-pass-fail signal) | MEDIUM |
| **`test_profile_duplicate_names.py` is `--ignore=`'d in CI.** Excluded test → "passes" by not running. Same shape as the xfail above. Need to fix the test or remove it. | `suppressed-signal` (custom — not protocol taxonomy) | MEDIUM |

---

## 3. Low-quality flags

Bash-detected (`audit-flag-low-quality`):

| Stack | Flag | Count | Notes |
|---|---|---|---|
| js | `call-count-assertions` | **75** | mostly `toHaveBeenCalled*` against `vi.mock('@/lib/api/client')` — implementation-shape assertions. Many of these were added by the prior audit's Phase 4 implementers. |
| js | `vacuous-assertions` | **7** | `.not.toThrow()` and `expect(true)` patterns; concentrated in `s5.test.ts`, `s6.test.ts`. |
| js | `implementation-naming` | **2** | `test('calls …')` style — names test by call rather than by behavior. |
| python | (none flagged) | 0 | The current `audit-flag-low-quality` regex is jest/JS-shaped and does NOT match Python `assert_called_*` / `MagicMock.call_count` — known v1 limitation. |

Top file:line evidence per bash-detected flag (Phase 2 input):

| Flag | Hotspot file | Count |
|---|---|---|
| `call-count-assertions` (top 5 files) | `app/src/components/VoiceProfiles/__tests__/SampleList.test.tsx` | 14 |
| | `app/src/components/VoiceProfiles/__tests__/AudioSampleUpload.test.tsx` | 11 |
| | `app/src/__tests__/acceptance/s5.test.ts` | 7 |
| | `app/src/components/VoiceProfiles/__tests__/AudioSampleRecording.test.tsx` | 7 |
| | `app/src/components/BooksTab/__tests__/BookImport.test.tsx` | 7 |
| `vacuous-assertions` (all 7) | `app/src/__tests__/acceptance/s6.test.ts:119`, `:142` | 2 |
| | `app/src/components/BooksTab/__tests__/ChapterReadAlong.test.tsx:274` | 1 |
| | `app/src/components/BooksTab/__tests__/SegmentRegenerateControl.test.tsx:216` | 1 |
| | `app/src/lib/hooks/__tests__/useBookProgress.test.ts:223`, `:224`, `:266` | 3 |
| `implementation-naming` (all 2) | `app/src/components/BooksTab/__tests__/ChapterReadAlong.test.tsx:240` | 1 |
| | `app/src/components/BooksTab/__tests__/VoiceEditorClone.test.tsx:121` | 1 |

Reviewer-level findings (these are NOT bash flags; they are noted here for Phase 2 design):

| Flag | Where | Why |
|---|---|---|
| `surface-proxy` | E2E specs c6–c15 (`app/e2e/`) | run in browser against `web/` Vite app at :5173, not packaged app (HIGH §2) |
| `contract-unverified` | `app/src/lib/api/` ↔ FastAPI routes | generated, but no CI drift check (MEDIUM §2) |
| `unverified-surface` (custom) | DictateWindow, AccessibilityGate, InputMonitoringGate, CapturesTab | zero tests; clearly a coverage gap, not a proxy finding (HIGH §2) |
| `missing-edge-cases` | deferred to `tcoder:test-augmentation-reviewer` pass during Phase 4 | per protocol §4 this is emitted by the per-task reviewer, not the audit-gate reviewer; no specific findings to enumerate at audit time |

---

## 4. Summary

Numbers vs reality:

| Stack | Measured | Reality |
|---|---|---|
| JS  | 91.92% statements | 91.92% of 25 files; ~168 source files unmeasured |
| Python | 95.10% statements | 95.10% of full backend — accurate |
| Rust | reported "100% pure-logic" previously | 11.82% whole crate; 582 lines of hardware/OS-IO at 0% |

Surface verification:

| Surface category | Verified against | Reality |
|---|---|---|
| BooksTab UI flow | vitest mocks + Playwright (dev-web) | UI behavior verified; **desktop-app integration is not** |
| VoiceProfiles UI | vitest + component mocks | UI behavior verified |
| Tauri IPC callers | nothing | **completely unverified** |
| JS↔Python contract | mocks against generated types | self-consistent; **drift detection missing** |
| Backend routes | pytest | verified |
| Rust hardware IO | nothing | **completely unverified** |

This re-audit's recommended Phase 2 priorities (in order):

1. **Decide the vitest scope policy** — either drop the `include` allow-list and measure the whole `src/`, or formally treat coverage as "of the audited subset" and stop reporting global percentages.
2. **Add a CI drift check** that runs `scripts/generate-api.sh` and fails the build if `app/src/lib/api/` diverges. Cheap, prevents silent contract drift. (`tooling-setup` task.)
3. **Real-app E2E** for at least the top user journeys: instead of (or in addition to) the browser-targeted Playwright specs, drive the **packaged Tauri binary** via `tauri-driver`/WebDriver or `pw-webview`. This is the only way to exercise the IPC boundary end-to-end.
4. **Tests for the 4 Tauri-IPC-using components** — at minimum unit tests with `@tauri-apps/api/core` mocked, and ideally one Playwright-against-real-app scenario per component.
5. **Rust hardware-IO modules** — add fake/seam-based tests (trait + impl, mockable in `cfg(test)`) for clipboard.rs (smallest, 12 lines) and speak_monitor.rs (68 lines). audio_capture and audio_output are larger and may not be worth the test effort given they're hardware-bound; flag as `accept-uncovered`.
6. **Address the 75 call-count-assertions** — many can be replaced with behavior-shape assertions (`toMatchObject`, `toBe(<final state>)`) without losing signal.

Phase 2 / Phase 3 will turn these into concrete tasks.
