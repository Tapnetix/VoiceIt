//! Layout-aware resolution of the keycode whose current-layout translation
//! is `'v'`. Drives [`crate::synthetic_keys::send_paste`] so the synthetic
//! Cmd+V it posts is interpreted as Paste by the focused app regardless of
//! the user's active keyboard layout (Dvorak, Colemak, AZERTY, …).
//!
//! macOS apps process Cmd+V via NSMenu key equivalents, which match against
//! `[NSEvent charactersIgnoringModifiers]` — i.e. the layout-translated
//! character, not the raw keycode. Posting `kVK_ANSI_V` (= 9, the QWERTY V
//! position) on Dvorak therefore produces Cmd+. and never triggers Paste.
//!
//! All TIS calls happen on the main thread: once at startup via [`init`]
//! from Tauri's setup hook, and again from the
//! `kTISNotifySelectedKeyboardInputSourceChanged` distributed notification
//! (delivered to the main runloop). The hot path ([`paste_keycode_v`])
//! only reads an [`AtomicU16`], so paste latency is unchanged.
//!
//! Windows is intentionally not covered here. `SendInput` with
//! `wVk = VK_V` delivers `WM_KEYDOWN` to the target with `wParam = VK_V`
//! regardless of the active layout — most Windows apps treat that as
//! Ctrl+V. AutoHotkey relies on the same behaviour.

use std::sync::atomic::{AtomicU16, Ordering};

/// `kVK_ANSI_V` — the keycode for the physical V key on a US QWERTY
/// layout. Used as the fallback whenever live resolution can't produce a
/// better answer (no Unicode key layout data, lookup failure, non-macOS).
const FALLBACK_V_KEYCODE: u16 = 9;

static V_KEYCODE: AtomicU16 = AtomicU16::new(FALLBACK_V_KEYCODE);

/// Returns the keycode whose current-layout translation is `'v'`. Falls
/// back to `kVK_ANSI_V` when resolution hasn't run, the active input
/// source carries no Unicode key layout data, or no keycode in the layout
/// produces `v`.
pub fn paste_keycode_v() -> u16 {
    V_KEYCODE.load(Ordering::Relaxed)
}

#[cfg(target_os = "macos")]
pub fn init() {
    macos::init();
}

#[cfg(not(target_os = "macos"))]
pub fn init() {}

#[cfg(test)]
mod tests {
    //! Pure-logic tests for the layout-aware paste-keycode cache.
    //!
    //! These tests intentionally avoid touching the live OS bridge
    //! (Carbon's TIS / UCKeyTranslate, the CFNotificationCenter observer).
    //! On non-macOS hosts the `macos` submodule isn't compiled at all, so the
    //! only surface exposed here is the atomic-backed cache plus the
    //! `init()` shim.
    //!
    //! `V_KEYCODE` is process-global static state. Tests that mutate it are
    //! serialized through a `Mutex` so cargo's default parallel test runner
    //! can't interleave them and produce nondeterministic reads.
    use super::{paste_keycode_v, FALLBACK_V_KEYCODE, V_KEYCODE};
    use std::sync::atomic::Ordering;
    use std::sync::Mutex;

    /// Serializes access to the process-global `V_KEYCODE` cache. Tests that
    /// write to it must hold this lock for the duration of the
    /// load/observe/restore sequence; tests that only assert on a freshly
    /// initialized process state (no prior write) hold it too so they don't
    /// race a concurrent writer in the same test binary.
    static V_KEYCODE_LOCK: Mutex<()> = Mutex::new(());

    /// RAII guard that snapshots `V_KEYCODE` on construction and restores it on
    /// drop, so a test can mutate the cache freely without leaking state into
    /// sibling tests (which would otherwise see the mutated value after this
    /// test releases `V_KEYCODE_LOCK`).
    struct VKeycodeGuard {
        original: u16,
        _lock: std::sync::MutexGuard<'static, ()>,
    }

    impl VKeycodeGuard {
        fn acquire() -> Self {
            // `lock()` returns `Err` only when the mutex is poisoned; if a
            // prior test panicked while holding it the state is still
            // meaningful for us, so recover via `into_inner`.
            let lock = V_KEYCODE_LOCK
                .lock()
                .unwrap_or_else(|poison| poison.into_inner());
            let original = V_KEYCODE.load(Ordering::Relaxed);
            Self {
                original,
                _lock: lock,
            }
        }
    }

    impl Drop for VKeycodeGuard {
        fn drop(&mut self) {
            V_KEYCODE.store(self.original, Ordering::Relaxed);
        }
    }

    #[test]
    fn fallback_keycode_matches_us_qwerty_v_position() {
        // `FALLBACK_V_KEYCODE` is the documented `kVK_ANSI_V` constant and
        // must stay pinned to 9 — the synthetic-paste path on every
        // non-macOS platform (and the pre-`init` window on macOS) relies on
        // this exact value being delivered as the V keycode.
        assert_eq!(FALLBACK_V_KEYCODE, 9);
    }

    #[test]
    fn paste_keycode_v_reports_the_currently_cached_value() {
        // The hot path must reflect whatever `V_KEYCODE` currently holds —
        // its only job is `load(Relaxed)`. Writing a distinctive sentinel
        // and reading it back through the public API verifies that contract
        // without depending on any specific layout-resolution outcome.
        let _guard = VKeycodeGuard::acquire();

        V_KEYCODE.store(42, Ordering::Relaxed);
        assert_eq!(paste_keycode_v(), 42);

        // A second distinct value confirms the read isn't a constant-fold
        // of the first observation.
        V_KEYCODE.store(123, Ordering::Relaxed);
        assert_eq!(paste_keycode_v(), 123);
    }

    #[test]
    fn paste_keycode_v_starts_at_fallback_before_any_resolution() {
        // Before any resolution has run, the cache must hand back the
        // documented fallback so a paste fired during the startup window
        // still hits the QWERTY V position rather than a random uninit
        // value. We assert this by resetting the atomic to its
        // declared-at-rest state and observing the public reader.
        let _guard = VKeycodeGuard::acquire();

        V_KEYCODE.store(FALLBACK_V_KEYCODE, Ordering::Relaxed);
        assert_eq!(paste_keycode_v(), FALLBACK_V_KEYCODE);
        assert_eq!(paste_keycode_v(), 9);
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn init_on_non_macos_is_a_noop_and_leaves_cache_at_fallback() {
        // On non-macOS targets `init()` is documented as a no-op: there is
        // no TIS bridge and no observer to register. After calling it the
        // cache must remain at the fallback (or whatever value it held
        // before — which on a clean process is the fallback).
        let _guard = VKeycodeGuard::acquire();

        V_KEYCODE.store(FALLBACK_V_KEYCODE, Ordering::Relaxed);
        super::init();
        assert_eq!(
            paste_keycode_v(),
            FALLBACK_V_KEYCODE,
            "non-macOS init() must not mutate the V keycode cache"
        );
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn init_on_non_macos_does_not_overwrite_a_pre_seeded_keycode() {
        // Stronger form of the previous test: even when something has
        // already written a non-fallback value into the cache (e.g. a test
        // harness, or a future platform-specific resolver), the non-macOS
        // `init()` must not clobber it. This pins the "no-op" contract
        // against the alternative implementation of "reset to fallback".
        let _guard = VKeycodeGuard::acquire();

        V_KEYCODE.store(77, Ordering::Relaxed);
        super::init();
        assert_eq!(paste_keycode_v(), 77);
    }
}

#[cfg(target_os = "macos")]
mod macos {
    use super::{FALLBACK_V_KEYCODE, V_KEYCODE};
    use core_foundation_sys::base::CFRelease;
    use core_foundation_sys::data::{CFDataGetBytePtr, CFDataRef};
    use core_foundation_sys::dictionary::CFDictionaryRef;
    use core_foundation_sys::notification_center::{
        CFNotificationCenterAddObserver, CFNotificationCenterGetDistributedCenter,
        CFNotificationCenterRef, CFNotificationName,
        CFNotificationSuspensionBehaviorDeliverImmediately,
    };
    use core_foundation_sys::string::CFStringRef;
    use std::ffi::c_void;
    use std::ptr;
    use std::sync::atomic::Ordering;

    type TISInputSourceRef = *mut c_void;

    /// `kUCKeyActionDown`.
    const K_UC_KEY_ACTION_DOWN: u16 = 0;
    /// `kUCKeyTranslateNoDeadKeysMask` — collapse dead-key state machine so
    /// a single call gives us the bare character. V is never a dead key on
    /// any layout we care about, but the flag costs nothing and removes
    /// any chance of ambiguous output.
    const K_UC_KEY_TRANSLATE_NO_DEAD_KEYS_MASK: u32 = 1;
    /// Standard US-style virtual keycodes occupy 0..0x7F. We iterate the
    /// full range so non-US-extended layouts (ISO, JIS) can still be
    /// resolved if their `v` lives outside the ANSI range.
    const MAX_KEYCODE: u16 = 127;
    const TARGET_CHAR: u16 = b'v' as u16;

    #[link(name = "Carbon", kind = "framework")]
    extern "C" {
        fn TISCopyCurrentKeyboardLayoutInputSource() -> TISInputSourceRef;
        fn TISGetInputSourceProperty(
            source: TISInputSourceRef,
            key: CFStringRef,
        ) -> *mut c_void;
        fn LMGetKbdType() -> u8;
        fn UCKeyTranslate(
            keyboard_layout: *const u8,
            virtual_key_code: u16,
            key_action: u16,
            modifier_key_state: u32,
            keyboard_type: u32,
            key_translate_options: u32,
            dead_key_state: *mut u32,
            max_string_length: usize,
            actual_string_length: *mut usize,
            unicode_string: *mut u16,
        ) -> i32;

        static kTISPropertyUnicodeKeyLayoutData: CFStringRef;
        static kTISNotifySelectedKeyboardInputSourceChanged: CFStringRef;
    }

    pub fn init() {
        resolve_into_cache();
        register_layout_change_observer();
    }

    fn resolve_into_cache() {
        let kc = resolve_v_keycode().unwrap_or(FALLBACK_V_KEYCODE);
        V_KEYCODE.store(kc, Ordering::Relaxed);
    }

    fn resolve_v_keycode() -> Option<u16> {
        unsafe {
            let source = TISCopyCurrentKeyboardLayoutInputSource();
            if source.is_null() {
                return None;
            }
            let _src_guard = scopeguard::guard(source, |s| CFRelease(s as *const c_void));

            let layout_data_ptr =
                TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData);
            if layout_data_ptr.is_null() {
                return None;
            }
            let layout_bytes = CFDataGetBytePtr(layout_data_ptr as CFDataRef);
            if layout_bytes.is_null() {
                return None;
            }

            let kbd_type = LMGetKbdType() as u32;

            for keycode in 0..=MAX_KEYCODE {
                let mut dead_key_state: u32 = 0;
                let mut chars: [u16; 4] = [0; 4];
                let mut actual_len: usize = 0;
                let status = UCKeyTranslate(
                    layout_bytes,
                    keycode,
                    K_UC_KEY_ACTION_DOWN,
                    0, // no modifiers
                    kbd_type,
                    K_UC_KEY_TRANSLATE_NO_DEAD_KEYS_MASK,
                    &mut dead_key_state,
                    chars.len(),
                    &mut actual_len,
                    chars.as_mut_ptr(),
                );
                if status == 0 && actual_len == 1 && chars[0] == TARGET_CHAR {
                    return Some(keycode);
                }
            }
            None
        }
    }

    extern "C" fn layout_changed(
        _center: CFNotificationCenterRef,
        _observer: *mut c_void,
        _name: CFNotificationName,
        _object: *const c_void,
        _user_info: CFDictionaryRef,
    ) {
        resolve_into_cache();
    }

    fn register_layout_change_observer() {
        unsafe {
            let center = CFNotificationCenterGetDistributedCenter();
            if center.is_null() {
                return;
            }
            CFNotificationCenterAddObserver(
                center,
                ptr::null(),
                layout_changed,
                kTISNotifySelectedKeyboardInputSourceChanged,
                ptr::null(),
                CFNotificationSuspensionBehaviorDeliverImmediately,
            );
        }
    }
}
