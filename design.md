# VoiceIt — Coverage Remediation Design (2026-06-25)

Phase 2 design for the re-audit. Approved audit findings live at `audit-report.md`.

## 1. Per-stack effective audit_target

User-confirmed uniform threshold for this run: **80%** for js, python, and rust.

| Stack | Target | Baseline this run | Notes |
|---|---|---|---|
| js     | 80% | 91.92% on current narrow scope; will drop after `coverage.include` is widened | Phase 5 re-measures on the widened scope; new tests need to keep it above 80% |
| python | 80% | 95.10%                                                                       | Comfortable headroom; effectively a regression gate |
| rust   | 80% | 11.82% whole crate                                                            | Scope-fix needed: see §2 — measurement is whole-crate, but the Phase 5 gate applies only to a "pure-logic + tested-IO" subset defined below |

### Rust pure-logic + tested-IO subset (gate scope)

Phase 5's Rust gate measures only the pure-logic set + any IO modules we add fake-backed tests for in Phase 4. Each excluded file has an explicit `accept-uncovered` decision recorded below (per audit-report §4 item 5):

| File | Lines uncovered | Decision | Rationale |
|---|---:|---|---|
| `src/audio_capture/linux.rs` | 184 | `accept-uncovered` | ALSA real-hardware bound; no fake-seam at this layer today. Out of scope this run; revisit if a portaudio/cubeb abstraction lands. |
| `src/audio_output.rs`        | 243 | `accept-uncovered` | cpal-bound; no fake-seam. Largest file; would need a `trait AudioSink` refactor before testability. Out of scope. |
| `src/hotkey_monitor.rs`      |  75 | `accept-uncovered` | rdev-bound global hotkey listener; OS event loop integration. Out of scope. |
| `src/speak_monitor.rs`       |  68 | **deferred** (NOT excluded long-term) | Audit-report §4.5 explicitly recommends adding fake-backed tests. **Not in scope this run** — recorded as a known follow-up `unit-test` task for the NEXT audit cycle. Currently excluded only because the seam doesn't exist yet. |
| `src/clipboard.rs`           |  12 | **deferred** (NOT excluded long-term) | Audit-report §4.5: smallest file, prime candidate for fake-backed tests via arboard-trait wrap. **Not in scope this run** — same as speak_monitor. |
| `src/audio_capture/macos.rs` |   0 coverable | `accept-uncovered` (cosmetic) | Not built on Linux CI; excluding is cosmetic since tarpaulin already shows 0/0. |
| `src/audio_capture/windows.rs`|  0 coverable | `accept-uncovered` (cosmetic) | Same as macos.rs. |

Tarpaulin invocation:

```bash
cargo tarpaulin --out Json \
  --exclude-files 'src/audio_capture/linux.rs' \
  --exclude-files 'src/audio_capture/macos.rs' \
  --exclude-files 'src/audio_capture/windows.rs' \
  --exclude-files 'src/audio_output.rs' \
  --exclude-files 'src/hotkey_monitor.rs' \
  --exclude-files 'src/speak_monitor.rs' \
  --exclude-files 'src/clipboard.rs'
```

When `speak_monitor.rs` and `clipboard.rs` land their fake-backed tests in a future audit cycle, the corresponding `--exclude-files` flags are removed and they re-enter the gate denominator.

## 2. Vitest coverage scope decision

Approved: **widen and explicitly exclude hardware/integration files**.

Replace `vitest.config.ts`'s `coverage.include` with a wide default plus explicit exclude for the Tauri-IPC-heavy components and other code paths that cannot be sensibly unit-tested without a real Tauri runtime:

```ts
// vitest.config.ts (target shape)
coverage: {
  provider: 'istanbul',
  reporter: ['text', 'html', 'json-summary'],
  include: ['src/**/*.{ts,tsx}'],
  exclude: [
    'src/**/*.test.{ts,tsx}',
    'src/**/__tests__/**',
    'src/**/__mocks__/**',
    'src/main.tsx',                                  // entry shim
    'src/components/DictateWindow/**',               // direct @tauri-apps callers — see acceptance S18
    'src/components/AccessibilityGate/**',           // — see acceptance S19
    'src/components/InputMonitoringGate/**',         // — see acceptance S20
    'src/components/CapturesTab/**',                 // — see acceptance S21
    'src/lib/hooks/useChordSync.ts',                 // @tauri-apps invoke caller, tested via S20/S21
    'src/lib/hooks/useCaptureRecordingSession.ts',   // @tauri-apps emit caller, tested via S21
  ],
}
```

The excluded files are not "untested forever" — they're tested via the acceptance scenarios S18–S21 against the real Tauri runtime (Phase 4 tooling-setup adds tauri-driver). Excluding them from the line-coverage gate prevents the headline number from misrepresenting their state.

## 3. Triage decisions (low-quality test files)

User-approved: **improve all 19 flagged files** (full triage). Each generates an `existing-test-improve` task.

| Task | File | Findings to address |
|---|---|---|
| T-LQ-01 | `app/src/__tests__/acceptance/s5.test.ts` | 7× call-count, 0× vacuous, 0× implementation-naming. **Acceptance test — implementer must re-anchor on S5's user-observable outcome (dialogue reassignment lands and persists), NOT mechanically swap matchers.** |
| T-LQ-02 | `app/src/__tests__/acceptance/s6.test.ts` | 0× call-count, 2× vacuous, 0× implementation-naming. **Acceptance test — same caveat as T-LQ-01: re-verify the S6 (generate audio for chapter/book) observable outcome is still asserted after the rewrite.** |
| T-LQ-03 | `app/src/components/BooksTab/__tests__/AudiobookExport.test.tsx` | call-count |
| T-LQ-04 | `app/src/components/BooksTab/__tests__/BookImport.test.tsx` | 7× call-count |
| T-LQ-05 | `app/src/components/BooksTab/__tests__/BookOverview.test.tsx` | call-count |
| T-LQ-06 | `app/src/components/BooksTab/__tests__/ChapterEditor.test.tsx` | call-count |
| T-LQ-07 | `app/src/components/BooksTab/__tests__/ChapterEditorEmotion.test.tsx` | call-count |
| T-LQ-08 | `app/src/components/BooksTab/__tests__/ChapterReadAlong.test.tsx` | 1× vacuous (L274), 1× implementation-naming (L240) |
| T-LQ-09 | `app/src/components/BooksTab/__tests__/SegmentRegenerateControl.test.tsx` | 1× vacuous (L216) |
| T-LQ-10 | `app/src/components/BooksTab/__tests__/VoiceEditor.test.tsx` | call-count |
| T-LQ-11 | `app/src/components/BooksTab/__tests__/VoiceEditorClone.test.tsx` | 1× implementation-naming (L121) |
| T-LQ-12 | `app/src/components/VoiceProfiles/__tests__/AudioSampleRecording.test.tsx` | 7× call-count |
| T-LQ-13 | `app/src/components/VoiceProfiles/__tests__/AudioSampleUpload.test.tsx` | 11× call-count |
| T-LQ-14 | `app/src/components/VoiceProfiles/__tests__/ProfileCard.test.tsx` | call-count |
| T-LQ-15 | `app/src/components/VoiceProfiles/__tests__/ProfileList.test.tsx` | call-count |
| T-LQ-16 | `app/src/components/VoiceProfiles/__tests__/SampleList.test.tsx` | 14× call-count |
| T-LQ-17 | `app/src/components/VoiceProfiles/__tests__/SampleUpload.test.tsx` | call-count |
| T-LQ-18 | `app/src/lib/hooks/__tests__/useBookProgress.test.ts` | 3× vacuous (L223,224,266) |
| T-LQ-19 | `app/src/lib/hooks/__tests__/useReferenceTranscript.test.ts` | call-count |

Treatment: replace `toHaveBeenCalled*` with behavior-shape assertions (`toMatchObject`, `toBe(<final state>)`, observable DOM/store assertions). Replace `.not.toThrow()` with concrete post-condition assertions. Rename `test('calls …')` styles to behavior-named tests.

## 4. Unified Acceptance Scenarios

### Carried forward from the prior audit

| ID  | Description                                                       | Existing spec(s) |
|-----|-------------------------------------------------------------------|------|
| S1  | Import a `.epub` and see parsed title/author/chapter count        | `app/src/__tests__/acceptance/s1.test.ts`, `app/e2e/c6.spec.ts` |
| S2  | Stream analysis progress with live characters appearing           | `s2.test.ts`, `c7.spec.ts` |
| S3  | Book overview hub (cast + chapters)                               | `c8.spec.ts` |
| S4  | Character voice editor — Design tab + preview                     | `c10.spec.ts` |
| S5  | Reassign dialogue in book view                                    | `s5.test.ts`, `c14.spec.ts` |
| S6  | Generate audio for a chapter / book                               | `s6.test.ts` |
| S7  | Manage cast (merge/delete) from overview                          | `c9.spec.ts` |
| S11 | Library-tab voice assignment                                      | `s11.test.ts`, `c11.spec.ts` |
| S12 | Clone a voice from a sample                                       | `c12.spec.ts` |
| S13 | Save/promote a voice to the library                               | `c13.spec.ts` |
| S15 | Fix mis-detected line (split/merge/retype/edit text)              | `c15.spec.ts` |

### New for this re-audit

| ID  | Description                                                                                                   | New artifact                                  | Why |
|-----|---------------------------------------------------------------------------------------------------------------|-----------------------------------------------|-----|
| S16 | API contract conformance: CI runs `scripts/generate-api.sh` and fails if `app/src/lib/api/` drifts             | Jenkinsfile Verify gate                       | MEDIUM contract-unverified |
| S17a | Real-Tauri-app boot smoke: tauri-driver launches the packaged binary, the renderer reaches BooksTab           | tauri-driver Playwright spec `app/e2e/r17a.spec.ts` | HIGH surface-proxy (boot half) |
| S17b | Tauri IPC round-trip: with the packaged app open, an `invoke()` to a no-op Tauri command returns the expected response | tauri-driver Playwright spec `app/e2e/r17b.spec.ts` | HIGH surface-proxy (IPC half) |
| S18 | DictateWindow flow: open dictate window → start dictation → stop → verify text emitted via @tauri-apps event   | `app/e2e/r18.spec.ts` + unit tests w/ @tauri-apps mocked | HIGH unverified-surface |
| S19 | AccessibilityGate: permission request → grant → no longer shows gate                                          | `app/e2e/r19.spec.ts` + unit tests             | HIGH unverified-surface |
| S20 | InputMonitoringGate: permission request → grant → no longer shows gate                                        | `app/e2e/r20.spec.ts` + unit tests             | HIGH unverified-surface |
| S21 | CapturesTab: listen for capture events → render to list → save selected capture to disk                       | `app/e2e/r21.spec.ts` + unit tests             | HIGH unverified-surface |
| S22 | Backend route `GET /profiles/{id}` returns 404 (not 500) when the profile doesn't exist                       | `backend/tests/test_routes_profiles.py:653`   | MEDIUM suppressed-signal U-py-012 |

### Scope notes / dropped scenarios

- No scenarios dropped — all three stacks accepted.
- S17a/S17b/S18/S19/S20/S21 require a `tooling-setup` task for tauri-driver. tauri-driver runs on Linux (via `selenium`) and Windows. **macOS is not supported by tauri-driver as of 2026-Q2** — these specs will run on Linux only in CI; macOS regression coverage relies on the local-dev manual check + the unit-test layer (`@tauri-apps` mocked at the module boundary).
- **S19 and S20 macOS-specificity gap.** AccessibilityGate (S19) and InputMonitoringGate (S20) wrap the macOS-only Accessibility / Input-Monitoring permission APIs. The Linux tauri-driver E2E cannot exercise those system APIs (they don't exist on Linux), and the unit-test layer mocks `@tauri-apps/api/core` — neither layer touches the real macOS permission grant flow. **S19 and S20 verify component reaction logic given mocked permission events only; the underlying macOS permission grant API has NO automated verification this run.** Recorded as a known coverage gap; remediation is a manual-on-macOS local check at release time. No new tasks added for the underlying API.
- S16 has two halves: (a) the drift check itself is `tooling-setup`; (b) the conformance verification is the gate's PASS/FAIL outcome — no separate test code.
- S22's "test" is mostly a code fix: replace `@pytest.mark.xfail(strict=True)` with a normal `def test_*` after fixing `backend/routes/profiles.py:307` to re-raise `HTTPException` before the catch-all.

## 5. Remediation roadmap (sets up Phase 3)

The plan-drafter (Phase 3) will turn this into typed tasks:

- **tooling-setup × 4**:
  1. Widen vitest scope (`vitest.config.ts`).
  2. Add OpenAPI drift gate to Jenkinsfile Verify (S16).
  3. Provision tauri-driver in CI + write `app/e2e/fixtures-tauri.ts` (S17a/S17b + S18-S21).
  4. **Un-ignore `backend/tests/test_profile_duplicate_names.py`**: triage decision is `un-ignore`. Manually verified all 6 tests pass when run in isolation (213 lines, proper fixtures, uses real sqlite + `create_profile`/`update_profile` paths). The `--ignore=` was added during the prior audit's Phase 4 and never revisited. Action: delete the `--ignore=backend/tests/test_profile_duplicate_names.py` line from the Jenkinsfile Verify stage's pytest invocation. (If a flake surfaces under parallel/full-suite execution, investigate at that point — but the test is currently good and being suppressed.)
- **existing-test-improve × 19**: T-LQ-01 … T-LQ-19 above.
- **unit-test × 4**: DictateWindow / AccessibilityGate / InputMonitoringGate / CapturesTab with `@tauri-apps` mocked.
- **e2e-spec × 7**: S16, S17a, S17b, S18, S19, S20, S21 (new Playwright specs + the OpenAPI drift gate's CI-side assertion).
- **integration-test × 1**: S22 — fix `backend/routes/profiles.py:307`, flip the xfail to a normal test.

Roughly **35 tasks**. Phase 4 batches them: tooling-setup first (4), then improve+unit+integration in parallel per-stack, then e2e last.
