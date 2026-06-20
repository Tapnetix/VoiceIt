#!/usr/bin/env bash
# audit-verify-targets.sh
# ---------------------------------------------------------------------------
# Aggregate verifier for the 2026-06-19 audit-testing-coverage plan.
#
# Runs the full coverage suite for every stack and gates the result against
# the targets recorded in plan.meta.audit_target:
#
#   JS         >= 80% (lines, vitest istanbul, json-summary)
#   Python     >= 80% (lines, pytest-cov over backend/ with pyproject omit set)
#   Rust       >= 60% over the pure-logic file set in plan.meta
#              (rust_os_bridge files are exempt and excluded from the ratio)
#
# Wires to Phase 5 of the plan. Exit code 0 only if every gate passes.
# Per-stack gates can be skipped with SKIP_JS=1 / SKIP_PY=1 / SKIP_RS=1 when
# the caller wants a partial verification (for example to re-check one stack
# after a fix).
# ---------------------------------------------------------------------------

set -u
set -o pipefail

# Resolve repo root from the script location so the file works regardless of
# the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PY_TARGET=80
JS_TARGET=80
RS_TARGET=60

# Pure-logic Rust files (audit-protocol). Anything not in this list is treated
# as OS-bridge and dropped from the aggregate. Keep in sync with
# plan.meta.rust_pure_logic_files.
RUST_PURE_LOGIC=(
  "src/key_codes.rs"
  "src/keyboard_layout.rs"
  "src/audio_capture/mod.rs"
  "src/synthetic_keys.rs"
  "src/focus_capture.rs"
)

# --- helpers ---------------------------------------------------------------

c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_green() { printf '\033[32m%s\033[0m' "$*"; }
c_bold()  { printf '\033[1m%s\033[0m' "$*"; }

log()  { printf '%s\n' "$*" >&2; }
hdr()  { log ""; log "$(c_bold "=== $* ===")"; }

# Compare two numbers (may be floats) with awk. Returns 0 if $1 >= $2.
ge() {
  awk -v a="$1" -v b="$2" 'BEGIN { exit (a + 0 >= b + 0) ? 0 : 1 }'
}

# Resolve a working `bunx` invocation. The audit-protocol command uses
# `bunx vitest`, but some hosts only ship `bun x`. Echo the prefix to use.
resolve_bunx() {
  if command -v bunx >/dev/null 2>&1; then
    echo "bunx"
  elif command -v bun >/dev/null 2>&1; then
    echo "bun x"
  else
    return 1
  fi
}

# --- python ----------------------------------------------------------------

run_python() {
  hdr "Python coverage (target ${PY_TARGET}%)"

  local py="$REPO_ROOT/backend/venv/bin/python"
  if [[ ! -x "$py" ]]; then
    log "$(c_red "FAIL"): backend venv python missing at $py"
    return 2
  fi

  # The explicit --cov-config is mandatory; without it the pyproject omit list
  # silently does nothing. See plan brief.
  local out_file
  out_file="$(mktemp)"

  ( cd "$REPO_ROOT" && \
      "$py" -m pytest backend/tests \
        --cov=backend \
        --cov-config=backend/pyproject.toml \
        --cov-report=term-missing ) \
        > "$out_file" 2>&1
  local rc=$?

  # Show the tail so a human running the script can see what happened.
  tail -n 40 "$out_file" >&2

  # Parse percent even if pytest failed -- a failing test does not invalidate
  # the coverage table, and the diagnostic is useful for the summary.
  # pytest-cov prints a `TOTAL` row whose last column is the percentage,
  # e.g. "TOTAL  1234  56  95%".
  local pct
  pct="$(grep -E '^TOTAL' "$out_file" | tail -n 1 | awk '{print $NF}' | tr -d '%')"
  rm -f "$out_file"

  if [[ -n "$pct" ]]; then
    PY_PCT="$pct"
  fi

  if [[ $rc -ne 0 ]]; then
    log "$(c_red "FAIL"): pytest exited with code $rc (any test failure blocks the gate, regardless of coverage)"
    return 2
  fi

  if [[ -z "$pct" ]]; then
    log "$(c_red "FAIL"): could not parse TOTAL percent from pytest-cov output"
    return 2
  fi

  log "Python total: ${pct}% (target ${PY_TARGET}%)"
  if ge "$pct" "$PY_TARGET"; then
    log "$(c_green "PASS"): python coverage >= ${PY_TARGET}%"
    PY_PCT="$pct"
    return 0
  fi
  log "$(c_red "FAIL"): python coverage ${pct}% < ${PY_TARGET}%"
  PY_PCT="$pct"
  return 1
}

# --- javascript ------------------------------------------------------------

run_js() {
  hdr "JS coverage (target ${JS_TARGET}%)"

  local app_dir="$REPO_ROOT/app"
  if [[ ! -d "$app_dir" ]]; then
    log "$(c_red "FAIL"): app/ directory missing"
    return 2
  fi

  local bunx_cmd
  if ! bunx_cmd="$(resolve_bunx)"; then
    log "$(c_red "FAIL"): neither bunx nor bun is on PATH"
    return 2
  fi

  local out_file
  out_file="$(mktemp)"

  # shellcheck disable=SC2086  # bunx_cmd may be "bun x" (two words) on purpose
  ( cd "$app_dir" && \
      $bunx_cmd vitest run \
        --coverage \
        --coverage.reporter=json-summary \
        --coverage.reporter=text ) \
      > "$out_file" 2>&1
  local rc=$?

  tail -n 60 "$out_file" >&2
  rm -f "$out_file"

  # vitest writes coverage-summary.json under app/coverage/ even when tests
  # fail, so parse it first and surface the percent in the summary -- then
  # let the runner exit code gate the result.
  local summary="$app_dir/coverage/coverage-summary.json"
  local pct=""
  if [[ -f "$summary" ]]; then
    pct="$(jq -r '.total.lines.pct' "$summary")"
    if [[ "$pct" == "null" ]]; then pct=""; fi
  fi
  if [[ -n "$pct" ]]; then
    JS_PCT="$pct"
  fi

  if [[ $rc -ne 0 ]]; then
    log "$(c_red "FAIL"): vitest exited with code $rc (any test failure blocks the gate)"
    return 2
  fi

  if [[ ! -f "$summary" ]]; then
    log "$(c_red "FAIL"): coverage-summary.json not produced at $summary"
    return 2
  fi
  if [[ -z "$pct" ]]; then
    log "$(c_red "FAIL"): could not read .total.lines.pct from $summary"
    return 2
  fi

  log "JS total lines: ${pct}% (target ${JS_TARGET}%)"
  if ge "$pct" "$JS_TARGET"; then
    log "$(c_green "PASS"): js coverage >= ${JS_TARGET}%"
    JS_PCT="$pct"
    return 0
  fi
  log "$(c_red "FAIL"): js coverage ${pct}% < ${JS_TARGET}%"
  JS_PCT="$pct"
  return 1
}

# --- rust ------------------------------------------------------------------

run_rust() {
  hdr "Rust pure-logic coverage (target ${RS_TARGET}%)"

  local rs_dir="$REPO_ROOT/tauri/src-tauri"
  if [[ ! -d "$rs_dir" ]]; then
    log "$(c_red "FAIL"): tauri/src-tauri/ missing"
    return 2
  fi

  if ! command -v cargo >/dev/null 2>&1; then
    log "$(c_red "FAIL"): cargo not on PATH"
    return 2
  fi

  local out_file
  out_file="$(mktemp)"

  ( cd "$rs_dir" && \
      cargo tarpaulin \
        --out Json \
        --lib --tests \
        --skip-clean \
        -- --skip test_system_audio_capture ) \
      > "$out_file" 2>&1
  local rc=$?

  tail -n 60 "$out_file" >&2

  if [[ $rc -ne 0 ]]; then
    log "$(c_red "FAIL"): cargo tarpaulin exited with code $rc"
    rm -f "$out_file"
    return 2
  fi
  rm -f "$out_file"

  local report="$rs_dir/tarpaulin-report.json"
  if [[ ! -f "$report" ]]; then
    log "$(c_red "FAIL"): tarpaulin-report.json not produced at $report"
    return 2
  fi

  # Build a jq query that filters files by suffix-matching any pure-logic
  # path. Tarpaulin reports `path` as an array of path components; we join
  # those with "/" and check for an endswith match against each member of
  # RUST_PURE_LOGIC.
  local pure_logic_json
  pure_logic_json="$(printf '%s\n' "${RUST_PURE_LOGIC[@]}" | jq -R . | jq -s .)"

  # Sum covered/coverable across only the pure-logic files.
  local sums
  sums="$(jq --argjson keep "$pure_logic_json" '
      .files
      | map(select(
          (.path | join("/")) as $p
          | any($keep[]; . as $k | $p | endswith($k))
        ))
      | {
          covered:   (map(.covered)   | add // 0),
          coverable: (map(.coverable) | add // 0),
          matched:   (map(.path | join("/")))
        }
    ' "$report")"

  local covered coverable
  covered="$(echo "$sums"   | jq -r '.covered')"
  coverable="$(echo "$sums" | jq -r '.coverable')"

  log "Rust pure-logic files matched:"
  echo "$sums" | jq -r '.matched[]' | sed 's/^/  /' >&2

  if [[ -z "$coverable" || "$coverable" == "0" || "$coverable" == "null" ]]; then
    log "$(c_red "FAIL"): no coverable lines found across pure-logic files (matched 0?)"
    return 2
  fi

  local pct
  pct="$(awk -v c="$covered" -v t="$coverable" 'BEGIN { printf "%.2f", (c / t) * 100 }')"

  log "Rust pure-logic total: ${covered}/${coverable} = ${pct}% (target ${RS_TARGET}%)"
  if ge "$pct" "$RS_TARGET"; then
    log "$(c_green "PASS"): rust pure-logic coverage >= ${RS_TARGET}%"
    RS_PCT="$pct"
    return 0
  fi
  log "$(c_red "FAIL"): rust pure-logic coverage ${pct}% < ${RS_TARGET}%"
  RS_PCT="$pct"
  return 1
}

# --- main ------------------------------------------------------------------

PY_PCT=""
JS_PCT=""
RS_PCT=""

PY_RESULT="skip"
JS_RESULT="skip"
RS_RESULT="skip"

OVERALL=0

if [[ "${SKIP_PY:-0}" != "1" ]]; then
  if run_python; then PY_RESULT="pass"; else PY_RESULT="fail"; OVERALL=1; fi
fi

if [[ "${SKIP_JS:-0}" != "1" ]]; then
  if run_js;     then JS_RESULT="pass"; else JS_RESULT="fail"; OVERALL=1; fi
fi

if [[ "${SKIP_RS:-0}" != "1" ]]; then
  if run_rust;   then RS_RESULT="pass"; else RS_RESULT="fail"; OVERALL=1; fi
fi

hdr "Summary"
printf 'Python : %s  (%s%%, target %d%%)\n' "$PY_RESULT" "${PY_PCT:-?}" "$PY_TARGET" >&2
printf 'JS     : %s  (%s%%, target %d%%)\n' "$JS_RESULT" "${JS_PCT:-?}" "$JS_TARGET" >&2
printf 'Rust   : %s  (%s%%, target %d%% pure-logic)\n' "$RS_RESULT" "${RS_PCT:-?}" "$RS_TARGET" >&2

if [[ $OVERALL -eq 0 ]]; then
  log "$(c_green "All gates passed.")"
else
  log "$(c_red "One or more gates failed.")"
fi
exit $OVERALL
