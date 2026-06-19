// Integration test for the live system-audio capture path.
//
// The end-to-end test requires:
//   1. A working host audio subsystem with a usable input/monitor device
//      (PulseAudio/PipeWire monitor on Linux, ScreenCaptureKit on macOS,
//      WASAPI loopback on Windows).
//   2. Audio actively playing on the system during the test so that the
//      monitor source produces non-silent samples.
//
// Neither of these is true in CI or sandboxed dev environments, so the
// test is marked `#[ignore]` and is additionally gated at runtime on
// `audio_capture::is_supported()`. To run it explicitly:
//
//     cargo test --test audio_capture_test -- --ignored --nocapture
//
// Device-independent unit tests for `AudioCaptureState` live in
// `mod_test.rs` so the default `cargo test` invocation stays green
// everywhere even when no audio device is present.
//
// (Bug-fix per audit task F5; layout refactored under U-rs-001.)

use base64::Engine;
use cpal::traits::{DeviceTrait, HostTrait};
use voiceit::audio_capture::{is_supported, start_capture, stop_capture, AudioCaptureState};

/// Probe the host more deeply than `is_supported()` does. `is_supported` returns
/// true if cpal reports *any* default input device, but on headless boxes that
/// device often fails to negotiate a config (ALSA "default" PCM with no card).
/// We additionally require that the default input device can hand back a
/// usable `default_input_config`, which is the very first thing `start_capture`
/// itself does. If that fails here, the real capture would fail too.
fn has_usable_input_device() -> bool {
    if !is_supported() {
        return false;
    }
    let host = cpal::default_host();
    match host.default_input_device() {
        Some(d) => d.default_input_config().is_ok(),
        None => false,
    }
}

#[tokio::test]
#[ignore = "requires a working audio subsystem and audio playing on the host; \
            run explicitly with `cargo test -- --ignored`"]
async fn test_system_audio_capture() {
    // Runtime gate: if the host cannot expose a capture device (e.g. headless
    // CI, container without /dev/snd), short-circuit cleanly instead of
    // emitting a misleading panic. This complements the `#[ignore]` attribute
    // by also protecting an operator who runs `--ignored` on a box where the
    // capability simply isn't there.
    if !has_usable_input_device() {
        eprintln!(
            "Skipping test_system_audio_capture: no usable audio capture \
             device detected on this host (is_supported / default_input_config \
             check failed)."
        );
        return;
    }

    let state = AudioCaptureState::new();

    println!("Starting system audio capture with 5 second max duration...");

    start_capture(&state, 5)
        .await
        .expect("start_capture should succeed when is_supported() is true");

    println!("Capture started, waiting 5 seconds...");
    tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;

    println!("Stopping capture...");
    let base64_wav = stop_capture(&state)
        .await
        .expect("stop_capture should return base64 WAV after a successful capture");

    let decoded_bytes = base64::engine::general_purpose::STANDARD
        .decode(&base64_wav)
        .expect("stop_capture output should be valid base64");

    // A well-formed WAV file produced by hound must start with the RIFF
    // container marker and carry the WAVE form-type, followed by a `fmt `
    // chunk. Checking these is a far stronger assertion than "non-empty".
    assert!(
        decoded_bytes.len() > 44,
        "WAV payload should be larger than a bare 44-byte header, got {} bytes",
        decoded_bytes.len()
    );
    assert_eq!(
        &decoded_bytes[0..4],
        b"RIFF",
        "decoded payload should begin with RIFF magic"
    );
    assert_eq!(
        &decoded_bytes[8..12],
        b"WAVE",
        "decoded payload should declare WAVE form-type"
    );
    assert_eq!(
        &decoded_bytes[12..16],
        b"fmt ",
        "decoded payload should contain a fmt chunk"
    );

    println!(
        "Test passed: captured {} bytes of valid WAV data",
        decoded_bytes.len()
    );
}
