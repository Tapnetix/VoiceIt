//! Synthetic keyboard event posting for the auto-paste pipeline.
//!
//! `send_paste` fires the four-event paste sequence onto the OS input
//! pipeline so the focused app performs its native paste action against
//! whatever the clipboard module has just staged.
//!
//! - **macOS** — Cmd down, V down with Cmd flag, V up with Cmd flag, Cmd
//!   up via `CGEventPost` at `kCGHIDEventTap`. Accessibility permission is
//!   load-bearing: without it the system swallows the events silently, so
//!   callers must gate on [`crate::accessibility::is_trusted`].
//! - **Windows** — Ctrl down, V down, V up, Ctrl up via `SendInput`. No
//!   permission gate, but UAC/UIPI blocks delivery into elevated target
//!   windows when we run non-elevated — nothing we can do short of also
//!   running elevated.
//!
//! On macOS the V keycode is resolved per-layout by
//! [`crate::keyboard_layout`] — Cmd+V is matched against the layout-
//! translated character via NSMenu key equivalents, so hardcoding
//! `kVK_ANSI_V` (the QWERTY V position) would fire Cmd+. on Dvorak. The
//! resolved keycode is read once per paste from an atomic; the cache is
//! primed at startup and refreshed on layout change.
//!
//! Windows hardcodes `VK_V`. `SendInput` with `wVk = VK_V` makes the
//! target receive `WM_KEYDOWN` with `wParam = VK_V` regardless of the
//! active layout, and most Windows apps treat that as Ctrl+V (the same
//! reason `Send "^v"` works in AutoHotkey on Dvorak Windows).

#[cfg(target_os = "macos")]
use std::ffi::c_void;

#[cfg(target_os = "macos")]
mod ffi {
    use std::ffi::c_void;

    #[repr(C)]
    pub struct CGEvent {
        _opaque: [u8; 0],
    }
    pub type CGEventRef = *mut CGEvent;

    #[repr(C)]
    pub struct CGEventSource {
        _opaque: [u8; 0],
    }
    pub type CGEventSourceRef = *mut CGEventSource;

    pub type CGEventTapLocation = u32;
    pub type CGKeyCode = u16;
    pub type CGEventFlags = u64;
    pub type CGEventSourceStateID = i32;

    /// `kCGHIDEventTap` — posted events enter at the HID level so every
    /// downstream tap (including the target app) sees them exactly as if the
    /// hardware had produced them.
    pub const K_CG_HID_EVENT_TAP: CGEventTapLocation = 0;

    /// `kCGEventSourceStateHIDSystemState` — mimics hardware, which is what
    /// we want: modifier bookkeeping inside target apps stays consistent.
    pub const K_CG_EVENT_SOURCE_STATE_HID_SYSTEM_STATE: CGEventSourceStateID = 1;

    /// `kCGEventFlagMaskCommand` — the Cmd modifier bit inside `CGEventFlags`.
    pub const K_CG_EVENT_FLAG_MASK_COMMAND: CGEventFlags = 0x00100000;

    /// `kVK_Command` (left Cmd).
    pub const KEYCODE_LEFT_CMD: CGKeyCode = 0x37;

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        pub fn CGEventSourceCreate(state_id: CGEventSourceStateID) -> CGEventSourceRef;
        pub fn CGEventCreateKeyboardEvent(
            source: CGEventSourceRef,
            virtual_key: CGKeyCode,
            key_down: bool,
        ) -> CGEventRef;
        pub fn CGEventSetFlags(event: CGEventRef, flags: CGEventFlags);
        pub fn CGEventPost(tap: CGEventTapLocation, event: CGEventRef);
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        pub fn CFRelease(cf: *const c_void);
    }
}

/// Post the four-event Cmd+V sequence to the HID event tap.
///
/// Returns after the events are queued — there's no completion callback,
/// so callers should sleep briefly afterwards to let the target app
/// process the paste before any follow-up (e.g. clipboard restore).
#[cfg(target_os = "macos")]
pub fn send_paste() -> Result<(), String> {
    use ffi::*;

    let v_keycode = crate::keyboard_layout::paste_keycode_v();

    unsafe {
        let source = CGEventSourceCreate(K_CG_EVENT_SOURCE_STATE_HID_SYSTEM_STATE);
        if source.is_null() {
            return Err("CGEventSourceCreate returned null".into());
        }
        let _source_guard = scopeguard::guard(source, |s| CFRelease(s as *const c_void));

        let events = [
            (KEYCODE_LEFT_CMD, true, 0),
            (v_keycode, true, K_CG_EVENT_FLAG_MASK_COMMAND),
            (v_keycode, false, K_CG_EVENT_FLAG_MASK_COMMAND),
            (KEYCODE_LEFT_CMD, false, 0),
        ];

        // Build the four events up front so CFRelease happens after all posts.
        // Posting in a loop that interleaved create → post → release would
        // work, but keeping the events alive for the full sequence matches
        // the pattern CGEventPost's docs show and is easier to reason about.
        let mut guards = Vec::with_capacity(events.len());
        let mut created = Vec::with_capacity(events.len());

        for (key, down, flags) in events {
            let event = CGEventCreateKeyboardEvent(source, key, down);
            if event.is_null() {
                return Err(format!(
                    "CGEventCreateKeyboardEvent(key={}, down={}) returned null",
                    key, down
                ));
            }
            let guard = scopeguard::guard(event, |e| CFRelease(e as *const c_void));
            if flags != 0 {
                CGEventSetFlags(event, flags);
            }
            created.push(event);
            guards.push(guard);
        }

        for event in created {
            CGEventPost(K_CG_HID_EVENT_TAP, event);
        }

        drop(guards);
        Ok(())
    }
}

#[cfg(target_os = "windows")]
mod win {
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYBD_EVENT_FLAGS, KEYEVENTF_KEYUP,
        VIRTUAL_KEY,
    };

    pub fn make_key(vk: VIRTUAL_KEY, up: bool) -> INPUT {
        let flags = if up {
            KEYEVENTF_KEYUP
        } else {
            KEYBD_EVENT_FLAGS(0)
        };
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }
}

#[cfg(target_os = "windows")]
pub fn send_paste() -> Result<(), String> {
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        SendInput, INPUT, VK_CONTROL, VK_V,
    };

    // Four-event Ctrl+V sequence. Matches the macOS CGEvent pattern: the
    // modifier brackets the letter so the target app sees a fully formed
    // accelerator rather than a lone V. `dwExtraInfo` is zero — we're not
    // tagging these as "ours" because no consumer in the paste path needs
    // to distinguish synthetic events from hardware ones.
    let events = [
        win::make_key(VK_CONTROL, false),
        win::make_key(VK_V, false),
        win::make_key(VK_V, true),
        win::make_key(VK_CONTROL, true),
    ];

    unsafe {
        let sent = SendInput(&events, std::mem::size_of::<INPUT>() as i32);
        if sent as usize != events.len() {
            return Err(format!(
                "SendInput delivered {} of {} events — the input desktop may be locked (secure attention sequence) or a higher-integrity window is intercepting.",
                sent,
                events.len()
            ));
        }
    }

    Ok(())
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
pub fn send_paste() -> Result<(), String> {
    Err("synthetic paste is not yet implemented on this platform".into())
}

#[cfg(test)]
mod tests {
    //! Pure-logic tests for `synthetic_keys`.
    //!
    //! The macOS and Windows code paths drive real OS input pipelines
    //! (`CGEventPost` at the HID tap, `SendInput` against the active
    //! desktop) and must not be exercised from a unit test — they would
    //! synthesise live keystrokes against whatever happens to be focused
    //! on the developer's box. What *is* testable on every host:
    //!
    //!   1. The Linux stub `send_paste` must hard-fail rather than
    //!      no-op-succeed, because callers in `main.rs` (`paste_final_text`,
    //!      `paste_refined_text`) trust an `Ok(())` to mean "Cmd+V / Ctrl+V
    //!      was queued onto the OS event tap" and follow up with a brief
    //!      sleep before restoring the clipboard. If the stub ever
    //!      returned `Ok` the clipboard restore would race the (never-
    //!      executed) paste, leaving the user with the raw clipboard
    //!      contents wiped and nothing pasted.
    //!   2. The macOS FFI constants (`KEYCODE_LEFT_CMD`,
    //!      `K_CG_EVENT_FLAG_MASK_COMMAND`,
    //!      `K_CG_HID_EVENT_TAP`,
    //!      `K_CG_EVENT_SOURCE_STATE_HID_SYSTEM_STATE`) are part of
    //!      Apple's stable ABI — pinning them prevents a typo from
    //!      silently degrading the paste sequence to "Cmd up, Ctrl+V, ..."
    //!      or queueing events into the session tap (which user apps
    //!      can't see) instead of the HID tap.
    //!   3. The Windows `make_key` helper is the only pure-logic surface
    //!      on that platform — it must set `INPUT_KEYBOARD`, carry the
    //!      requested `wVk`, and set `KEYEVENTF_KEYUP` if and only if
    //!      `up == true`. Anything else and `send_paste` would emit a
    //!      malformed paste sequence regardless of how `SendInput`
    //!      behaves.
    //!
    //! Per-platform assertions are gated by the same `cfg` blocks the
    //! production code uses, so they only compile on the platform whose
    //! invariant they encode. Tarpaulin (Linux) measures coverage on
    //! the stub branch.

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    #[test]
    fn send_paste_on_unsupported_platform_returns_explanatory_error() {
        // The stub exists so the rest of the binary can link on Linux
        // dev hosts, but it must never appear to succeed. Per the
        // callers in main.rs, an `Ok` return is taken to mean "the
        // four-event Cmd+V / Ctrl+V sequence is queued onto the OS
        // input tap" and the next step (a brief sleep before clipboard
        // restore) assumes that. Returning Err is what stops
        // `paste_final_text` cold so the clipboard isn't clobbered for
        // a paste that never happened.
        let err = super::send_paste().expect_err("Linux stub must return Err");
        let lower = err.to_lowercase();
        assert!(
            lower.contains("not") && lower.contains("implemented"),
            "stub error should identify itself as the unimplemented platform stub, got: {err}"
        );
        assert!(
            lower.contains("platform"),
            "stub error should mention the platform, got: {err}"
        );
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    #[test]
    fn send_paste_is_deterministic_across_calls_on_the_stub() {
        // The Linux stub has no internal state; calling it ten times
        // in a row must produce ten identical errors. If it ever
        // started returning Ok intermittently (e.g. someone "fixed"
        // it by adding an early-return), the higher-level paste flow
        // would race the clipboard restore on the first Ok and the
        // developer's debugging session would suddenly start losing
        // clipboard contents at random.
        let first = super::send_paste().expect_err("first call must Err");
        for _ in 0..10 {
            let again = super::send_paste().expect_err("repeat call must Err");
            assert_eq!(again, first, "stub error must be stable across calls");
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_event_tap_constants_match_apples_published_values() {
        // These four constants are the ABI surface the macOS paste
        // sequence relies on. Hand-typing them in the `ffi` submodule
        // is the only place a typo could silently downgrade behaviour
        // (e.g. posting to the session tap, which user apps can't see,
        // or stamping the wrong modifier flag), so pin them here.
        use super::ffi::*;
        assert_eq!(K_CG_HID_EVENT_TAP, 0, "kCGHIDEventTap is 0");
        assert_eq!(
            K_CG_EVENT_SOURCE_STATE_HID_SYSTEM_STATE, 1,
            "kCGEventSourceStateHIDSystemState is 1"
        );
        assert_eq!(
            K_CG_EVENT_FLAG_MASK_COMMAND, 0x00100000,
            "kCGEventFlagMaskCommand is bit 20"
        );
        assert_eq!(KEYCODE_LEFT_CMD, 0x37, "kVK_Command (left Cmd) is 0x37");
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn make_key_sets_keyboard_type_and_carries_requested_virtual_key() {
        // `send_paste` builds the four-event Ctrl+V sequence by handing
        // each (vk, up) pair to `make_key`. The whole sequence is
        // garbage if `make_key` ever returns the wrong INPUT type
        // (mouse vs keyboard) or drops the requested VK, so pin both.
        use super::win::make_key;
        use windows::Win32::UI::Input::KeyboardAndMouse::{
            INPUT_KEYBOARD, VK_CONTROL, VK_V,
        };
        let down = make_key(VK_CONTROL, false);
        assert_eq!(down.r#type, INPUT_KEYBOARD, "must be a keyboard INPUT");
        unsafe {
            assert_eq!(
                down.Anonymous.ki.wVk, VK_CONTROL,
                "must carry the requested virtual key"
            );
            assert_eq!(down.Anonymous.ki.wScan, 0, "scan code is unused");
            assert_eq!(down.Anonymous.ki.time, 0, "time must be zero (system fills)");
            assert_eq!(down.Anonymous.ki.dwExtraInfo, 0, "no synthetic-tag in extra info");
        }
        let v_down = make_key(VK_V, false);
        unsafe {
            assert_eq!(v_down.Anonymous.ki.wVk, VK_V);
        }
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn make_key_flags_keyup_only_when_up_argument_is_true() {
        // The four-event paste sequence is Ctrl down, V down, V up,
        // Ctrl up — half the events need KEYEVENTF_KEYUP set, the
        // other half need it clear. If `make_key` ever ignored its
        // `up` argument the target app would receive four key-down
        // events in a row and Ctrl would stay logically held after
        // the paste, breaking the next typed character.
        use super::win::make_key;
        use windows::Win32::UI::Input::KeyboardAndMouse::{
            KEYBD_EVENT_FLAGS, KEYEVENTF_KEYUP, VK_CONTROL,
        };
        let down = make_key(VK_CONTROL, false);
        let up = make_key(VK_CONTROL, true);
        unsafe {
            assert_eq!(
                down.Anonymous.ki.dwFlags,
                KEYBD_EVENT_FLAGS(0),
                "down event must clear KEYEVENTF_KEYUP"
            );
            assert_eq!(
                up.Anonymous.ki.dwFlags, KEYEVENTF_KEYUP,
                "up event must set KEYEVENTF_KEYUP"
            );
        }
    }
}
