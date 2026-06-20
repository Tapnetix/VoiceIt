// Unit tests for `src/audio_capture/mod.rs` (U-rs-001).
//
// `mod.rs` re-exports the per-OS capture backend and defines the
// `AudioCaptureState` value type that owns the live capture session's
// shared mutable state. The OS bridges (linux/macos/windows submodules)
// are deliberately not exercised here — they require a working host
// audio subsystem and are covered by the gated integration test in
// `audio_capture_test.rs`. These tests stay focused on the pure-logic
// surface of `AudioCaptureState` so they remain device-independent
// and runnable in CI.

use voiceit::audio_capture::AudioCaptureState;

// Re-export contract: `mod.rs` promises that whichever OS submodule is
// active (linux / macos / windows), its `start_capture`, `stop_capture`
// and `is_supported` symbols are reachable through the parent module's
// public surface. Imports are the assertion — if the contract ever
// regresses (e.g. a `cfg(target_os = ...)` typo), this file stops
// compiling and the test target turns red.
#[allow(unused_imports)]
use voiceit::audio_capture::{is_supported, start_capture, stop_capture};

#[test]
fn new_state_exposes_clean_empty_session_with_cd_quality_defaults() {
    // `AudioCaptureState::new()` is the canonical constructor every
    // capture session runs through. A fresh value must carry no
    // residual samples, no error, and no stop channel from a prior
    // session, and its format fields must default to CD-quality
    // 44.1 kHz / stereo so that callers who skip device negotiation
    // (e.g. early WAV header writes) see sensible values.
    let state = AudioCaptureState::new();

    assert!(
        state.samples.lock().unwrap().is_empty(),
        "fresh state should hold no captured samples"
    );
    assert!(
        state.error.lock().unwrap().is_none(),
        "fresh state should carry no error"
    );
    assert!(
        state.stop_tx.lock().unwrap().is_none(),
        "fresh state should have no live stop channel"
    );
    assert_eq!(
        *state.sample_rate.lock().unwrap(),
        44_100,
        "default sample rate should be 44.1 kHz"
    );
    assert_eq!(
        *state.channels.lock().unwrap(),
        2,
        "default channel count should be stereo"
    );
}

#[test]
fn state_shares_mutable_fields_across_clones_of_the_internal_arcs() {
    // The capture backends spawn a thread that needs to write samples
    // and report errors back to the owning state. `AudioCaptureState`
    // achieves that by holding each field in an `Arc<Mutex<_>>`. The
    // observable contract is "a write through any clone of the inner
    // Arc is visible through the original" — exercise that directly,
    // because backends rely on it.
    let state = AudioCaptureState::new();
    let samples_clone = state.samples.clone();
    let error_clone = state.error.clone();

    samples_clone.lock().unwrap().extend_from_slice(&[0.25, -0.5, 0.75]);
    *error_clone.lock().unwrap() = Some("backend reported failure".to_string());

    assert_eq!(
        state.samples.lock().unwrap().as_slice(),
        &[0.25, -0.5, 0.75],
        "samples written via a cloned Arc must be visible on the original"
    );
    assert_eq!(
        state.error.lock().unwrap().as_deref(),
        Some("backend reported failure"),
        "errors written via a cloned Arc must be visible on the original"
    );
}

#[test]
fn reset_drops_prior_session_payload_while_keeping_negotiated_format() {
    // `reset` runs at the start of every capture. Per its inline use
    // in the backends, it must:
    //   * drop residual samples from the previous session,
    //   * clear any sticky error,
    //   * but PRESERVE the negotiated sample-rate / channel pair,
    //     because those reflect the device's actual format and are
    //     set during `start_capture`. Wiping them would force the
    //     WAV writer to lie about the data it just wrote.
    let state = AudioCaptureState::new();

    state
        .samples
        .lock()
        .unwrap()
        .extend_from_slice(&[0.1, 0.2, -0.3]);
    *state.error.lock().unwrap() = Some("previous failure".to_string());
    *state.sample_rate.lock().unwrap() = 48_000;
    *state.channels.lock().unwrap() = 1;

    state.reset();

    assert!(
        state.samples.lock().unwrap().is_empty(),
        "reset should drop prior samples"
    );
    assert!(
        state.error.lock().unwrap().is_none(),
        "reset should clear prior error"
    );
    assert_eq!(
        *state.sample_rate.lock().unwrap(),
        48_000,
        "reset must preserve the previously negotiated sample rate"
    );
    assert_eq!(
        *state.channels.lock().unwrap(),
        1,
        "reset must preserve the previously negotiated channel count"
    );
}

#[test]
fn reset_does_not_disturb_an_in_flight_stop_channel() {
    // `reset` is called by the backends BEFORE they install a new stop
    // sender; the stop channel field is owned by the capture lifecycle
    // (set in `start_capture`, taken in `stop_capture`). `reset` must
    // not touch it, otherwise a stale `reset` could orphan a live
    // capture's shutdown signal. Document and verify that boundary.
    let state = AudioCaptureState::new();
    let (tx, _rx) = tokio::sync::mpsc::channel::<()>(1);
    *state.stop_tx.lock().unwrap() = Some(tx);

    state.reset();

    assert!(
        state.stop_tx.lock().unwrap().is_some(),
        "reset must leave any in-flight stop channel intact"
    );
}

#[test]
fn reset_is_idempotent_on_an_already_clean_state() {
    // Calling `reset` twice in a row, or on a freshly-constructed
    // state, must be a no-op visible only as "still clean". This
    // matters because the backends call `reset` unconditionally at
    // the top of `start_capture`, including the very first session.
    let state = AudioCaptureState::new();

    state.reset();
    state.reset();

    assert!(state.samples.lock().unwrap().is_empty());
    assert!(state.error.lock().unwrap().is_none());
    assert_eq!(*state.sample_rate.lock().unwrap(), 44_100);
    assert_eq!(*state.channels.lock().unwrap(), 2);
}
