//! Captures the focused-UI snapshot at chord-start so auto-paste can land
//! in the user's original text field even after focus drifts during
//! transcription / refinement.
//!
//! We don't try to re-focus a specific sub-element on restore — many apps
//! expose complex focus hierarchies that don't respond consistently to
//! programmatic focus pokes. Bringing the owning *window* to the
//! foreground is enough: the window's own focus manager restores its
//! last-focused field, which is what every well-behaved paste-buffer tool
//! does and what users expect.
//!
//! - **macOS** — `AXUIElementCopyAttributeValue(kAXFocusedUIElement)` +
//!   `AXUIElementGetPid` + NSRunningApplication activation. Activation
//!   uses the cooperative-activation pattern on macOS 14+ (the caller
//!   `yieldActivationToApplication:`s, then the target `activate`s) and
//!   falls back to the pre-Sonoma `activateWithOptions:` on 11–13. See
//!   `activate_pid` for the rationale.
//! - **Windows** — `GetForegroundWindow` + `GetWindowThreadProcessId` for
//!   the top-level HWND and PID; UIAutomation's `IUIAutomation::GetFocusedElement`
//!   for best-effort control-class (skipped silently if COM isn't usable).
//!   Activation walks top-level windows for the saved PID and calls
//!   `SetForegroundWindow`, bracketed by the `AttachThreadInput` dance
//!   so Windows' foreground-lock rules don't silently swallow the
//!   activation into a taskbar flash.
//!
//! PID + bundle id + role are all captured for diagnostics — the bundle
//! id lets step 6 (internal direct injection) detect "focus was inside
//! VoiceIt itself" and short-circuit the synthetic-paste path. On
//! Windows, `bundle_id` holds the lowercased exe basename (`"voiceit.exe"`)
//! since there's no equivalent of macOS' reverse-DNS bundle identifier.

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct FocusSnapshot {
    pub pid: i32,
    pub bundle_id: Option<String>,
    pub role: Option<String>,
}

#[cfg(target_os = "macos")]
use core_foundation_sys::base::{kCFAllocatorDefault, CFRelease};
#[cfg(target_os = "macos")]
use core_foundation_sys::string::{
    kCFStringEncodingUTF8, CFStringCreateWithCString, CFStringGetCString, CFStringGetLength,
    CFStringRef,
};
#[cfg(target_os = "macos")]
use objc::runtime::Object;
#[cfg(target_os = "macos")]
use objc::{class, msg_send, sel, sel_impl};

#[cfg(target_os = "macos")]
type Id = *mut Object;

#[cfg(target_os = "macos")]
mod ffi {
    use core_foundation_sys::base::CFTypeRef;
    use core_foundation_sys::string::CFStringRef;

    pub type AXError = i32;
    pub const AX_ERROR_SUCCESS: AXError = 0;
    pub type AXUIElementRef = *const std::ffi::c_void;
    pub type Pid = i32;

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        pub fn AXUIElementCreateSystemWide() -> AXUIElementRef;
        pub fn AXUIElementCopyAttributeValue(
            element: AXUIElementRef,
            attribute: CFStringRef,
            value: *mut CFTypeRef,
        ) -> AXError;
        pub fn AXUIElementGetPid(element: AXUIElementRef, pid: *mut Pid) -> AXError;
    }
    // AX attribute keys are exposed as C macros that expand to CFSTR(...)
    // literals, not as linkable symbols — build the CFStrings at runtime
    // instead (see `cf_string_const` in focus_capture.rs).
}

#[cfg(target_os = "macos")]
struct AutoreleasePool {
    pool: Id,
}

#[cfg(target_os = "macos")]
impl AutoreleasePool {
    unsafe fn new() -> Self {
        let pool: Id = msg_send![class!(NSAutoreleasePool), alloc];
        let pool: Id = msg_send![pool, init];
        Self { pool }
    }
}

#[cfg(target_os = "macos")]
impl Drop for AutoreleasePool {
    fn drop(&mut self) {
        unsafe {
            let _: () = msg_send![self.pool, drain];
        }
    }
}

#[cfg(target_os = "macos")]
unsafe fn ns_string_to_rust(s: Id) -> Option<String> {
    if s.is_null() {
        return None;
    }
    let bytes: *const i8 = msg_send![s, UTF8String];
    if bytes.is_null() {
        return None;
    }
    std::ffi::CStr::from_ptr(bytes)
        .to_str()
        .ok()
        .map(|x| x.to_owned())
}

/// Build a `+1` retained CFString from an ASCII constant. Caller owns the
/// returned reference and must `CFRelease` it. Used for AX attribute keys
/// (`"AXFocusedUIElement"`, `"AXRole"`) because those aren't exported as
/// linker symbols — Apple ships them as `CFSTR(...)` macros.
#[cfg(target_os = "macos")]
unsafe fn cf_string_const(s: &str) -> Option<CFStringRef> {
    let cstr = std::ffi::CString::new(s).ok()?;
    let result = CFStringCreateWithCString(kCFAllocatorDefault, cstr.as_ptr(), kCFStringEncodingUTF8);
    if result.is_null() {
        None
    } else {
        Some(result)
    }
}

#[cfg(target_os = "macos")]
unsafe fn cfstring_to_rust(s: CFStringRef) -> Option<String> {
    if s.is_null() {
        return None;
    }
    let len = CFStringGetLength(s);
    if len == 0 {
        return Some(String::new());
    }
    // CFStringGetLength is in UTF-16 code units; UTF-8 can need up to 4
    // bytes per unit plus the trailing NUL.
    let max_bytes = (len * 4 + 1) as usize;
    let mut buf = vec![0u8; max_bytes];
    let ok = CFStringGetCString(
        s,
        buf.as_mut_ptr() as *mut i8,
        max_bytes as isize,
        kCFStringEncodingUTF8,
    );
    if ok == 0 {
        return None;
    }
    let cstr = std::ffi::CStr::from_ptr(buf.as_ptr() as *const i8);
    cstr.to_str().ok().map(|x| x.to_owned())
}

#[cfg(target_os = "macos")]
unsafe fn bundle_id_for_pid(pid: i32) -> Option<String> {
    let _pool = AutoreleasePool::new();
    let app: Id = msg_send![
        class!(NSRunningApplication),
        runningApplicationWithProcessIdentifier: pid
    ];
    if app.is_null() {
        return None;
    }
    let bundle: Id = msg_send![app, bundleIdentifier];
    ns_string_to_rust(bundle)
}

/// Read the system-wide focused UI element's PID, bundle id, and AX role.
///
/// Returns an error when no element is focused (e.g. Dock has focus) or
/// when Accessibility permission is missing — `AXUIElementCopyAttributeValue`
/// returns `-25204 kAXErrorAPIDisabled` in that case.
#[cfg(target_os = "macos")]
pub fn capture_focus() -> Result<FocusSnapshot, String> {
    use ffi::*;
    unsafe {
        let system_wide = AXUIElementCreateSystemWide();
        if system_wide.is_null() {
            return Err("AXUIElementCreateSystemWide returned null".into());
        }
        let _sys_guard = scopeguard::guard(system_wide, |e| {
            CFRelease(e as *const std::ffi::c_void)
        });

        let focused_attr = cf_string_const("AXFocusedUIElement")
            .ok_or("Failed to build AXFocusedUIElement CFString")?;
        let _focused_attr_guard =
            scopeguard::guard(focused_attr, |s| CFRelease(s as *const std::ffi::c_void));

        let mut focused: *const std::ffi::c_void = std::ptr::null();
        let err = AXUIElementCopyAttributeValue(
            system_wide,
            focused_attr,
            &mut focused as *mut _,
        );
        if err != AX_ERROR_SUCCESS || focused.is_null() {
            return Err(format!(
                "No focused element (AXError {}). Verify Accessibility permission is granted and a focused text field exists.",
                err
            ));
        }
        let _focus_guard = scopeguard::guard(focused, |e| CFRelease(e));

        let focused_elem = focused as AXUIElementRef;

        let mut pid: Pid = 0;
        let err = AXUIElementGetPid(focused_elem, &mut pid);
        if err != AX_ERROR_SUCCESS {
            return Err(format!("AXUIElementGetPid failed (AXError {})", err));
        }

        let role = {
            let role_attr = cf_string_const("AXRole");
            match role_attr {
                Some(role_attr) => {
                    let _role_attr_guard = scopeguard::guard(role_attr, |s| {
                        CFRelease(s as *const std::ffi::c_void)
                    });
                    let mut role_value: *const std::ffi::c_void = std::ptr::null();
                    let err = AXUIElementCopyAttributeValue(
                        focused_elem,
                        role_attr,
                        &mut role_value as *mut _,
                    );
                    if err == AX_ERROR_SUCCESS && !role_value.is_null() {
                        let _role_guard = scopeguard::guard(role_value, |e| CFRelease(e));
                        cfstring_to_rust(role_value as CFStringRef)
                    } else {
                        None
                    }
                }
                None => None,
            }
        };

        let bundle_id = bundle_id_for_pid(pid);

        Ok(FocusSnapshot {
            pid,
            bundle_id,
            role,
        })
    }
}

/// Bring the app owning `pid` to the foreground, re-activating its
/// last-focused window. Paired with [`capture_focus`] at chord-start so a
/// post-transcription synthetic ⌘V lands where the user started, not
/// wherever focus drifted to during the transcribe / refine window.
///
/// macOS 14 (Sonoma) deprecated `activateWithOptions:` in favour of a
/// cooperative-activation pattern: the caller first invokes
/// `yieldActivationToApplication:` on its own `NSRunningApplication` to
/// grant the target activation rights, then the target's `activate`
/// succeeds against the tightened Sonoma foreground rules. Without the
/// yield, `activate` on 14+ sometimes silently fails or only bounces the
/// dock icon — exactly the "paste lands in the wrong app" symptom we're
/// trying to prevent. The yield is discovered at runtime via
/// `respondsToSelector:` so we don't need an operatingSystemVersion probe
/// and the pre-Sonoma path stays identical.
///
/// The BOOL return of both `activate` and `activateWithOptions:` is now
/// propagated — if the system refuses activation (target quit mid-
/// transcription, trust revoked, cooperative-activation refused) the
/// caller aborts before clobbering the clipboard.
#[cfg(target_os = "macos")]
pub fn activate_pid(pid: i32) -> Result<(), String> {
    unsafe {
        let _pool = AutoreleasePool::new();
        let target: Id = msg_send![
            class!(NSRunningApplication),
            runningApplicationWithProcessIdentifier: pid
        ];
        if target.is_null() {
            return Err(format!("No running application for PID {}", pid));
        }

        let activated: bool = if can_yield_activation() {
            let current: Id =
                msg_send![class!(NSRunningApplication), currentApplication];
            if !current.is_null() {
                let _: () = msg_send![current, yieldActivationToApplication: target];
            }
            msg_send![target, activate]
        } else {
            // NSApplicationActivateIgnoringOtherApps = 1 << 1 = 2.
            msg_send![target, activateWithOptions: 2u64]
        };

        if !activated {
            return Err(format!(
                "NSRunningApplication activate returned false for PID {} — the target may have quit mid-transcription, Accessibility is no longer trusted, or the system refused cooperative activation.",
                pid
            ));
        }
        Ok(())
    }
}

/// `true` when `NSRunningApplication` responds to
/// `yieldActivationToApplication:` — the macOS 14+ discriminator for the
/// cooperative-activation APIs. Cached since the answer doesn't change
/// over a process's lifetime and the objc_msgSend probe is otherwise
/// repeated on every paste.
#[cfg(target_os = "macos")]
fn can_yield_activation() -> bool {
    use std::sync::OnceLock;
    static CACHED: OnceLock<bool> = OnceLock::new();
    *CACHED.get_or_init(|| unsafe {
        let current: Id = msg_send![class!(NSRunningApplication), currentApplication];
        if current.is_null() {
            return false;
        }
        let responds: bool = msg_send![
            current,
            respondsToSelector: sel!(yieldActivationToApplication:)
        ];
        responds
    })
}

#[cfg(target_os = "windows")]
mod win {
    use std::path::Path;

    use windows::core::{IUnknown, BOOL, BSTR, PWSTR};
    use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM};
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED,
    };
    use windows::Win32::System::Threading::{
        AttachThreadInput, GetCurrentThreadId, OpenProcess, QueryFullProcessImageNameW,
        PROCESS_NAME_FORMAT, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    use windows::Win32::UI::Accessibility::{CUIAutomation, IUIAutomation, IUIAutomationElement};
    use windows::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetForegroundWindow, GetWindow, GetWindowThreadProcessId, IsWindowVisible,
        SetForegroundWindow, GW_OWNER,
    };

    /// Read the PID that owns `hwnd`. Returns 0 on failure.
    pub unsafe fn hwnd_pid(hwnd: HWND) -> u32 {
        let mut pid: u32 = 0;
        let _ = GetWindowThreadProcessId(hwnd, Some(&mut pid as *mut _));
        pid
    }

    /// Query a PID's executable path and return its lowercased basename
    /// (e.g. `"voiceit.exe"`). This is the Windows analogue of macOS'
    /// `bundleIdentifier`, just less globally unique — two apps with the
    /// same exe name can collide, but that's rare enough to accept for
    /// the self-paste short-circuit.
    pub fn exe_basename(pid: u32) -> Option<String> {
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
            let mut buf = [0u16; 1024];
            let mut size = buf.len() as u32;
            let ok = QueryFullProcessImageNameW(
                handle,
                PROCESS_NAME_FORMAT(0),
                PWSTR(buf.as_mut_ptr()),
                &mut size,
            );
            let _ = CloseHandle(handle);
            if ok.is_err() || size == 0 {
                return None;
            }
            let full = String::from_utf16(&buf[..size as usize]).ok()?;
            let basename = Path::new(&full)
                .file_name()
                .and_then(|s| s.to_str())
                .map(|s| s.to_ascii_lowercase())?;
            Some(basename)
        }
    }

    /// Best-effort `UIAutomation::GetFocusedElement().CurrentClassName()`.
    /// Returns `None` when COM init, CoCreateInstance, or any UIA call
    /// fails — role info is nice-to-have, not load-bearing for paste.
    pub fn focused_control_class() -> Option<String> {
        unsafe {
            // MTA per-thread init. Ignore HRESULT: S_OK / S_FALSE /
            // RPC_E_CHANGED_MODE are all benign for our uses here, and
            // we deliberately never call CoUninitialize (the Tauri
            // runtime thread lives for the life of the process, so
            // leaving COM init in place is fine).
            let _ = CoInitializeEx(None, COINIT_MULTITHREADED);

            let automation: IUIAutomation =
                CoCreateInstance(&CUIAutomation, None::<&IUnknown>, CLSCTX_INPROC_SERVER).ok()?;
            let element: IUIAutomationElement = automation.GetFocusedElement().ok()?;
            // UIAutomationElement's CurrentClassName allocates a BSTR
            // the caller has to drop. `BSTR` in `windows` crate is a
            // Drop-wrapped owned string, so just returning `.to_string()`
            // is safe.
            let class: BSTR = element.CurrentClassName().ok()?;
            let s = class.to_string();
            if s.is_empty() {
                None
            } else {
                Some(s)
            }
        }
    }

    /// Find a visible top-level window owned by `pid`. Returns the first
    /// match via `EnumWindows`. Top-level ≡ no owner window.
    pub fn find_top_level_window(pid: u32) -> Option<HWND> {
        struct Ctx {
            target_pid: u32,
            found: Option<HWND>,
        }
        let mut ctx = Ctx {
            target_pid: pid,
            found: None,
        };
        unsafe extern "system" fn callback(hwnd: HWND, lparam: LPARAM) -> BOOL {
            let ctx = &mut *(lparam.0 as *mut Ctx);
            if hwnd_pid(hwnd) != ctx.target_pid {
                return BOOL(1);
            }
            // Skip tool windows / invisible shells. `GetWindow(GW_OWNER)`
            // is non-null for modal dialogs and other secondary windows;
            // we want the real app frame, which has no owner.
            if !IsWindowVisible(hwnd).as_bool() {
                return BOOL(1);
            }
            if !GetWindow(hwnd, GW_OWNER).unwrap_or(HWND(std::ptr::null_mut())).is_invalid() {
                return BOOL(1);
            }
            ctx.found = Some(hwnd);
            BOOL(0)
        }
        unsafe {
            let _ = EnumWindows(
                Some(callback),
                LPARAM(&mut ctx as *mut _ as isize),
            );
        }
        ctx.found
    }

    /// Bring `hwnd` to the foreground reliably.
    ///
    /// Plain `SetForegroundWindow` loses to Windows' foreground-lock
    /// rules — when our process isn't already foreground it can't hand
    /// focus to another app. The documented workaround is to attach the
    /// current thread's input queue to the current foreground window's
    /// thread for the duration of the call, which temporarily lets us
    /// share that thread's "last user activity" stamp.
    pub fn activate_hwnd(hwnd: HWND) -> Result<(), String> {
        unsafe {
            let fg = GetForegroundWindow();
            if fg == hwnd {
                return Ok(());
            }

            let our_thread = GetCurrentThreadId();
            let fg_thread = if fg.is_invalid() {
                0
            } else {
                let mut _pid: u32 = 0;
                GetWindowThreadProcessId(fg, Some(&mut _pid as *mut _))
            };

            let attached = fg_thread != 0
                && fg_thread != our_thread
                && AttachThreadInput(our_thread, fg_thread, true).as_bool();

            let ok = SetForegroundWindow(hwnd).as_bool();

            if attached {
                let _ = AttachThreadInput(our_thread, fg_thread, false);
            }

            if !ok {
                return Err(format!(
                    "SetForegroundWindow failed for HWND {:?} — Windows foreground-lock may have denied the activation.",
                    hwnd.0
                ));
            }
            Ok(())
        }
    }
}

#[cfg(target_os = "windows")]
pub fn capture_focus() -> Result<FocusSnapshot, String> {
    use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;

    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_invalid() {
            return Err(
                "GetForegroundWindow returned null — the desktop has no focused window (secure attention sequence, lock screen, or no user session)."
                    .into(),
            );
        }
        let pid = win::hwnd_pid(hwnd);
        if pid == 0 {
            return Err("GetWindowThreadProcessId returned PID 0 for the foreground window".into());
        }
        let bundle_id = win::exe_basename(pid);
        let role = win::focused_control_class();
        Ok(FocusSnapshot {
            pid: pid as i32,
            bundle_id,
            role,
        })
    }
}

#[cfg(target_os = "windows")]
pub fn activate_pid(pid: i32) -> Result<(), String> {
    if pid <= 0 {
        return Err(format!("Cannot activate invalid PID {pid}"));
    }
    let hwnd = win::find_top_level_window(pid as u32)
        .ok_or_else(|| format!("No visible top-level window for PID {pid}"))?;
    win::activate_hwnd(hwnd)
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
pub fn capture_focus() -> Result<FocusSnapshot, String> {
    Err("focus capture is not yet implemented on this platform".into())
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
pub fn activate_pid(_pid: i32) -> Result<(), String> {
    Err("app activation is not yet implemented on this platform".into())
}

#[cfg(test)]
mod tests {
    //! Pure-logic tests for `focus_capture`.
    //!
    //! The macOS and Windows code paths are thin wrappers over OS FFI
    //! (Accessibility, NSRunningApplication, UIAutomation, EnumWindows)
    //! and must not be exercised from a unit test — they require a real
    //! windowing session and granted Accessibility permission, neither
    //! of which exists in CI. What *is* testable on every host:
    //!
    //!   1. The stub implementations the binary falls back to on
    //!      unsupported platforms must return `Err` (so the higher-level
    //!      `paste_final_text` flow aborts cleanly rather than silently
    //!      "succeeding" with a no-op).
    //!   2. `FocusSnapshot` is a Tauri IPC payload — the frontend hands
    //!      one back to `paste_final_text` as `focus: FocusSnapshot`
    //!      after receiving it from `debug_capture_focus`. Its JSON
    //!      shape (field names, optional handling, signed PID) is part
    //!      of that wire contract, so any accidental rename or type
    //!      change would silently break auto-paste. The serde round-trip
    //!      tests pin that shape.
    //!
    //! Per-platform behaviour assertions on the stub paths are gated by
    //! the same `cfg` the stubs themselves use, so they're only compiled
    //! on the platform whose stub they're checking.

    use super::FocusSnapshot;

    #[test]
    fn focus_snapshot_serializes_with_lowercase_field_names_and_omits_nones() {
        // Serde shape contract: the frontend serialises a FocusSnapshot
        // straight into the `paste_final_text` Tauri command, and the
        // backend echoes one out of `debug_capture_focus`. Field names
        // must stay snake_case `pid` / `bundle_id` / `role` because
        // that's what main.rs:824 documents and what the TS callers
        // rely on. `None` for bundle_id / role must serialise as JSON
        // `null` (not omitted) so the frontend can distinguish "we
        // looked and found nothing" from "the backend forgot to send
        // the field".
        let snap = FocusSnapshot {
            pid: 4321,
            bundle_id: None,
            role: None,
        };
        let json = serde_json::to_value(&snap).expect("FocusSnapshot must serialize");
        assert_eq!(json["pid"], serde_json::json!(4321));
        assert!(json.get("bundle_id").is_some(), "bundle_id key must be present even when None");
        assert!(json["bundle_id"].is_null(), "None bundle_id must serialise as JSON null");
        assert!(json.get("role").is_some(), "role key must be present even when None");
        assert!(json["role"].is_null(), "None role must serialise as JSON null");
    }

    #[test]
    fn focus_snapshot_serializes_populated_bundle_id_and_role_as_strings() {
        // The macOS path stores reverse-DNS bundle ids ("com.apple.Safari")
        // and AX role strings ("AXTextField"); the Windows path stores
        // lowercase exe basenames ("voiceit.exe") and UIA class names.
        // Both must arrive on the frontend as plain JSON strings.
        let snap = FocusSnapshot {
            pid: 1234,
            bundle_id: Some("com.apple.Safari".to_string()),
            role: Some("AXTextField".to_string()),
        };
        let json = serde_json::to_value(&snap).expect("FocusSnapshot must serialize");
        assert_eq!(json["pid"], serde_json::json!(1234));
        assert_eq!(json["bundle_id"], serde_json::json!("com.apple.Safari"));
        assert_eq!(json["role"], serde_json::json!("AXTextField"));
    }

    #[test]
    fn focus_snapshot_round_trips_through_json() {
        // `paste_final_text(text, focus)` receives the snapshot from
        // the frontend after it was emitted by `debug_capture_focus`,
        // so the deserialise path must accept exactly what the
        // serialise path emits — including the `null` form for absent
        // optionals.
        let original = FocusSnapshot {
            pid: 9999,
            bundle_id: Some("com.example.app".to_string()),
            role: None,
        };
        let json = serde_json::to_string(&original).expect("serialize");
        let decoded: FocusSnapshot = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(decoded.pid, original.pid);
        assert_eq!(decoded.bundle_id, original.bundle_id);
        assert_eq!(decoded.role, original.role);
    }

    #[test]
    fn focus_snapshot_accepts_negative_pid_so_invalid_marker_round_trips() {
        // `pid` is `i32` (not `u32`) specifically so frontend code can
        // pass a sentinel like `-1` to mean "no focus captured" without
        // serde rejecting it. `activate_pid` on Windows then rejects
        // `pid <= 0` itself (see win::activate_pid). Locking the signed
        // type prevents an accidental switch to `u32` from silently
        // changing that contract.
        let json = r#"{"pid":-1,"bundle_id":null,"role":null}"#;
        let decoded: FocusSnapshot = serde_json::from_str(json).expect("negative pid must deserialize");
        assert_eq!(decoded.pid, -1);
        assert!(decoded.bundle_id.is_none());
        assert!(decoded.role.is_none());
    }

    #[test]
    fn focus_snapshot_clone_produces_independent_copy_of_owned_strings() {
        // FocusSnapshot derives Clone because hotkey_monitor.rs stores
        // it inside chord-start state and later hands a copy to the
        // paste command. The Clone must deep-copy the owned String
        // fields, not alias them, so a later mutation to one half
        // can't surprise the other.
        let original = FocusSnapshot {
            pid: 7,
            bundle_id: Some("com.example.app".to_string()),
            role: Some("AXTextField".to_string()),
        };
        let cloned = original.clone();
        assert_eq!(cloned.pid, original.pid);
        assert_eq!(cloned.bundle_id, original.bundle_id);
        assert_eq!(cloned.role, original.role);
        // Sanity: the cloned strings are distinct allocations, not the
        // same pointer — guarantees a future mutation of one wouldn't
        // affect the other.
        if let (Some(a), Some(b)) = (original.bundle_id.as_ref(), cloned.bundle_id.as_ref()) {
            assert_eq!(a, b);
            assert!(!std::ptr::eq(a.as_ptr(), b.as_ptr()));
        }
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    #[test]
    fn capture_focus_on_unsupported_platform_returns_explanatory_error() {
        // The stub exists so the rest of the binary can link on Linux
        // dev hosts, but it must never appear to succeed — auto-paste
        // would then activate a phantom PID 0 and clobber the
        // clipboard with nothing to show for it. The error message
        // must be descriptive enough for a triage engineer to recognise
        // "this is the platform stub, not a real OS failure".
        let err = super::capture_focus().expect_err("stub must return Err on Linux");
        assert!(
            err.to_lowercase().contains("not") && err.to_lowercase().contains("implemented"),
            "stub error should explain it's unimplemented, got: {err}"
        );
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    #[test]
    fn activate_pid_on_unsupported_platform_returns_explanatory_error_for_any_pid() {
        // Same rationale as capture_focus's stub: must hard-fail rather
        // than no-op-succeed. The PID value should be irrelevant on the
        // stub — we accept it but always reject.
        let err = super::activate_pid(1234).expect_err("stub must return Err on Linux");
        assert!(
            err.to_lowercase().contains("not") && err.to_lowercase().contains("implemented"),
            "stub error should explain it's unimplemented, got: {err}"
        );
        // Negative / zero PIDs must also be rejected by the stub — the
        // Windows path has its own `pid <= 0` guard, but on Linux the
        // platform-not-implemented error fires first regardless.
        let err = super::activate_pid(0).expect_err("stub must reject PID 0 too");
        assert!(err.to_lowercase().contains("not"));
        let err = super::activate_pid(-1).expect_err("stub must reject negative PID too");
        assert!(err.to_lowercase().contains("not"));
    }
}
