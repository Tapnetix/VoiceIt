# VoiceIt — Coverage Remediation Plan (2026-06-25)

Machine-readable form: `plan.json`. This file is the human-review companion. Approved scope lives in `audit-report.md` and `design.md`.

**35 tasks across 5 batches** (4 tooling-setup → 19 LQ improves → 4 unit-tests + 1 integration-test → 7 e2e specs → 1 conditional coverage-gap follow-up).

## Batches

### B1 — Tooling setup (4 tasks, parallel)

| Task | Title | Touches |
|---|---|---|
| `T-TS-01` | Widen vitest coverage scope | `app/vitest.config.ts` |
| `T-TS-02` | OpenAPI drift gate in Jenkinsfile Verify (S16) | `Jenkinsfile` |
| `T-TS-03` | Tauri-driver provisioning + `app/e2e/fixtures-tauri.ts` | `Jenkinsfile`, `app/playwright.config.ts`, `justfile`, new `app/e2e/fixtures-tauri.ts` |
| `T-TS-04` | Un-ignore `test_profile_duplicate_names.py` | `Jenkinsfile` |

**Rationale:** B1 must complete before B4 (tauri-driver needed for e2e specs) and before the conditional B5 (the post-widen coverage measurement that drives B5's task list). T-TS-02 and T-TS-04 are independent and can run alongside T-TS-01/T-TS-03.

### B2 — Existing-test-improve (19 tasks, parallel)

19 tasks `T-LQ-01` … `T-LQ-19` covering every file flagged by `audit-flag-low-quality` (75 call-count-assertions / 7 vacuous-assertions / 2 implementation-naming). T-LQ-01 (s5.test.ts) and T-LQ-02 (s6.test.ts) carry the additional "re-anchor on user-observable scenario outcome" note per `design.md` Issue 4 resolution.

File sets are disjoint — each task touches exactly one test file. Safe to fan out fully in parallel.

### B3 — Unit + integration tests (5 tasks, parallel)

| Task | Type | Target |
|---|---|---|
| `T-UT-DICTATE`  | unit-test | `DictateWindow` with `@tauri-apps` mocked |
| `T-UT-A11Y`     | unit-test | `AccessibilityGate` with `@tauri-apps` mocked (note: macOS perm API gap recorded) |
| `T-UT-IM`       | unit-test | `InputMonitoringGate` with `@tauri-apps` mocked (same gap) |
| `T-UT-CAPTURES` | unit-test | `CapturesTab` with `@tauri-apps/api/event` + `plugin-dialog` + `plugin-fs` mocked |
| `T-IT-S22`      | integration-test | Fix `backend/routes/profiles.py:307` HTTPException-swallowing bug, flip the xfail at `test_routes_profiles.py:653` |

Parallel: file sets are disjoint.

### B4 — E2E specs (7 tasks, parallel after B1's T-TS-03)

| Task | Scenario | Spec file |
|---|---|---|
| `T-E2E-S16`  | S16 OpenAPI drift gate | (CI-gate outcome — no new spec) |
| `T-E2E-S17A` | S17a Tauri boot smoke | `app/e2e/r17a.spec.ts` |
| `T-E2E-S17B` | S17b IPC round-trip | `app/e2e/r17b.spec.ts` |
| `T-E2E-S18`  | S18 DictateWindow flow | `app/e2e/r18.spec.ts` |
| `T-E2E-S19`  | S19 AccessibilityGate (mocked perm events, Linux-only) | `app/e2e/r19.spec.ts` |
| `T-E2E-S20`  | S20 InputMonitoringGate (mocked perm events, Linux-only) | `app/e2e/r20.spec.ts` |
| `T-E2E-S21`  | S21 CapturesTab flow | `app/e2e/r21.spec.ts` |

All depend on T-TS-03's `fixtures-tauri.ts`. T-E2E-S17B may require adding a `#[tauri::command] fn ping()` to `tauri/src-tauri/src/lib.rs` as the test fixture (the implementer decides; alternative is to reuse an existing no-op command).

### B5 — Conditional coverage-gap follow-up (0+ tasks, dynamic)

After B1's T-TS-01 widens the vitest scope, re-run JS coverage. Any file below the 80% gate gets a dynamically-added `unit-test` task. Expected to be **empty**: the unmeasured files today are either (a) the 4 Tauri-IPC components we explicitly excluded from coverage in design.md §2 or (b) already tested by the existing 49 vitest suites that just weren't in the include list. Confirm at Phase 4 entry of B5.

## Bug escalations expected

`T-IT-S22` is a known bug fix that ships with the test flip. No fresh bug-escalation prompts expected in B3 for this task.

The implementers in B2 may surface escalations if a low-quality test was hiding a real production bug (e.g., a `toHaveBeenCalled` was the only "assertion" and a behavior-shape rewrite reveals the production code is wrong). Phase 4's bug-escalation triage AskUserQuestion handles those one-by-one.

## Phase 5 verify scope

Per `design.md` §1:
- js: re-run `bun x vitest run --coverage` against the widened scope; gate at 80%.
- python: re-run `pytest --cov=backend --cov-config=backend/pyproject.toml`; gate at 80% (currently 95.10%, headroom).
- rust: re-run `cargo tarpaulin` with the 7 `--exclude-files` from `plan.json` `meta.rust_exclude_files`; gate at 80% on the resulting subset.

Plus E2E re-run: all c6-c15 dev-web specs + all r17a/r17b/r18-r21 tauri-driver specs (Linux), with one S22 pytest behavior-check.
