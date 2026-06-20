//! S10 acceptance: the keyboard-layout module resolves a valid V-keycode
//! identifier on every platform branch (`#[cfg(target_os = "macos")]` and
//! `#[cfg(not(target_os = "macos"))]`) so the synthetic-paste path in
//! `synthetic_keys::send_paste` always has a deliverable keycode.
//!
//! Mapping the audit's scenario wording onto the actual public surface:
//!
//!   scenario "current_layout() returns a non-empty layout identifier"
//!   ─►  keyboard_layout::paste_keycode_v() returns a non-zero keycode
//!       (the layout-translated keycode for `'v'`, the only identifier the
//!        module exposes — there is no separate `current_layout()` symbol;
//!        the V-keycode IS the identifier the rest of the crate consumes).
//!
//! The contract documented in `src/keyboard_layout.rs` is:
//!
//!   * Before any resolution runs, the cache hands back `FALLBACK_V_KEYCODE`
//!     (= 9, the `kVK_ANSI_V` constant) so synthetic Cmd+V still hits the
//!     QWERTY V position during the startup window.
//!   * On macOS, `init()` walks Carbon's TIS / UCKeyTranslate to find the
//!     keycode whose layout translation is `'v'`. If that lookup can't run
//!     (headless test env, no Unicode key layout data on the active input
//!     source), the cache stays at the fallback — never zero, never empty.
//!   * On non-macOS, `init()` is documented as a no-op; the cache must
//!     remain at the fallback (or whatever non-zero value it held before).
//!
//! Each `#[cfg]` branch below exercises the platform path that actually
//! compiles on the test host. Both branches assert the same observable
//! S10 outcome: a non-zero, layout-meaningful keycode after `init()`,
//! exactly matching the audit scenario's "non-empty layout identifier"
//! wording in the only surface the crate actually exposes.

use voiceit::keyboard_layout::{init, paste_keycode_v};

/// The documented `kVK_ANSI_V` fallback value the module guarantees
/// across every platform. Mirrors the private `FALLBACK_V_KEYCODE`
/// constant in `src/keyboard_layout.rs`; pinned here so the test fails
/// loudly if the public contract value ever drifts. Only the non-macOS
/// branch asserts on the exact value (macOS may resolve to a different
/// keycode on Dvorak/AZERTY hosts), so the constant is gated to the
/// same cfg to keep the macOS test build warning-clean.
#[cfg(not(target_os = "macos"))]
const FALLBACK_V_KEYCODE: u16 = 9;

#[test]
fn s10_paste_keycode_is_always_non_zero_so_synthetic_paste_can_deliver() {
    // Cross-platform invariant: regardless of whether `init()` has run
    // yet in this test binary, `paste_keycode_v()` must never hand back
    // 0. A zero identifier would mean synthetic-paste fires
    // `Cmd + <keycode 0>` and the target app would never see Paste.
    //
    // This test asserts the lower-bound shape of the "non-empty layout
    // identifier" S10 contract without coupling to test-execution
    // order: the per-platform tests below pin the exact post-init
    // value, while this one pins the cross-platform non-zero floor.
    let kc = paste_keycode_v();
    assert_ne!(
        kc, 0,
        "V keycode must never be 0 (would synthesize Cmd+<key 0>)"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn s10_macos_branch_resolves_a_non_empty_v_keycode_after_init() {
    // macOS branch: `init()` runs the real TIS/UCKeyTranslate resolver
    // on the main thread. On any sane macOS host the active input
    // source carries Unicode key layout data and the scan finds a
    // keycode whose translation is `'v'` (= the V position for the
    // active layout — 9 on US QWERTY, 47 on Dvorak, etc.). On a host
    // where the data isn't available (TIS returns null, no
    // UnicodeKeyLayoutData property), the documented contract is
    // "fall back to FALLBACK_V_KEYCODE" — still a non-zero, valid
    // identifier.
    //
    // The S10 scenario is "non-empty layout identifier on this
    // platform branch", so this test asserts the union of those two
    // outcomes: the post-init value is non-zero and lies in the
    // documented standard-virtual-keycode range (0..=127).
    init();

    let kc = paste_keycode_v();
    assert_ne!(
        kc, 0,
        "macOS init() must leave a non-zero V keycode (resolved or fallback)"
    );
    assert!(
        kc <= 127,
        "macOS init() must leave a standard virtual keycode (0..=127), got {kc}"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn s10_macos_branch_init_is_idempotent_so_repeated_setup_hooks_stay_valid() {
    // Same-platform follow-up: `init()` is invoked from Tauri's setup
    // hook and again from the kTISNotifySelectedKeyboardInputSourceChanged
    // observer (every time the user switches layout). Calling it twice
    // in a row must leave the cache in a still-valid non-empty state —
    // a regression where the second call clobbered the cache to 0
    // would break paste after the first layout switch.
    init();
    let first = paste_keycode_v();
    init();
    let second = paste_keycode_v();

    assert_ne!(first, 0, "first init() must leave a non-zero keycode");
    assert_ne!(second, 0, "repeated init() must leave a non-zero keycode");
    assert_eq!(
        first, second,
        "repeated init() on the same input source must be deterministic"
    );
}

#[cfg(not(target_os = "macos"))]
#[test]
fn s10_non_macos_branch_init_is_a_noop_and_keycode_stays_non_empty() {
    // Non-macOS branch: `init()` is documented as a no-op. There is
    // no TIS bridge on Windows or Linux; the synthetic-paste path on
    // those platforms uses `SendInput`/uinput with `wVk = VK_V` /
    // `KEY_V` directly and never reads the cache for layout purposes.
    // Still, `paste_keycode_v()` must keep returning the documented
    // non-zero fallback so any caller that does read it gets a valid
    // identifier rather than 0.
    //
    // This is the "non-empty layout identifier on this platform
    // branch" half of the S10 scenario for non-macOS hosts.
    init();

    let kc = paste_keycode_v();
    assert_ne!(
        kc, 0,
        "non-macOS init() must leave the V keycode at a non-zero value"
    );
    assert_eq!(
        kc, FALLBACK_V_KEYCODE,
        "non-macOS init() must leave the V keycode at the documented fallback (9)"
    );
}

#[cfg(not(target_os = "macos"))]
#[test]
fn s10_non_macos_branch_init_is_idempotent_across_repeated_calls() {
    // Same-platform follow-up: the non-macOS `init()` is a no-op, so
    // calling it any number of times must never disturb the cached
    // identifier. The contract here is even stronger than on macOS:
    // there is no resolver to run, so the value must be exactly the
    // documented fallback before and after, with no drift between
    // calls.
    init();
    let first = paste_keycode_v();
    init();
    init();
    let third = paste_keycode_v();

    assert_eq!(
        first, FALLBACK_V_KEYCODE,
        "non-macOS init() must land at the documented fallback"
    );
    assert_eq!(
        first, third,
        "repeated non-macOS init() must not drift the cached keycode"
    );
}

#[test]
fn s10_resolved_keycode_is_consumable_by_the_synthetic_paste_caller() {
    // Cross-platform consumer-shape check: the only caller of
    // `paste_keycode_v()` in the crate is
    // `synthetic_keys::send_paste`, which feeds the value into a
    // platform key-event API as a virtual keycode. Those APIs expect
    // a value in the standard virtual-keycode range (0..=127 on
    // macOS, and the equivalent VK_* / KEY_* range elsewhere — all
    // well below u16::MAX). A value of 0 would synthesize a Cmd+<no
    // key> event; a value above 127 on macOS would be silently
    // ignored by the keyboard subsystem. Both failure modes silently
    // break paste, so S10 must rule them out on the active platform
    // branch.
    init();

    let kc = paste_keycode_v();
    assert!(
        (1..=127).contains(&kc),
        "post-init keycode {kc} must be in the standard virtual-keycode range (1..=127) \
         so the synthetic-paste caller can deliver it"
    );
}
