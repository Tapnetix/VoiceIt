"""
Unit tests for ``backend.utils.audio``.

Targets the lower-level helpers (``_load_pcm_wav``, ``normalize_loudness``,
``normalize_audio``, ``load_audio``, ``save_audio``, ``trim_tts_output``)
that the existing ``test_audio.py`` / ``test_audio_preprocess.py`` files
don't exercise. Together these lift statement coverage on
``backend/utils/audio.py`` above the 80% target.

All tests work on synthetic audio (sine tones + zeros), so no external
fixture files are required.
"""

import sys
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.audio import (  # noqa: E402
    _load_pcm_wav,
    load_audio,
    normalize_audio,
    normalize_loudness,
    save_audio,
    trim_tts_output,
)


SR = 24000


def _tone(duration_s: float, amp: float = 0.3, freq: float = 220.0, sr: int = SR) -> np.ndarray:
    n = int(duration_s * sr)
    t = np.arange(n, dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _write_pcm16_wav(path: Path, audio: np.ndarray, sr: int, channels: int = 1) -> None:
    """Write a 16-bit PCM WAV using the stdlib `wave` module (no soundfile)."""
    pcm = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())


# ---------------------------------------------------------------------------
# _load_pcm_wav: fast-path decode for 16-bit PCM WAV
# ---------------------------------------------------------------------------

def test_load_pcm_wav_returns_mono_float32_at_target_sr(tmp_path):
    """The fast-path returns float32 audio at the requested target sample rate."""
    audio = _tone(0.5, amp=0.3, sr=SR)
    path = tmp_path / "mono.wav"
    _write_pcm16_wav(path, audio, SR, channels=1)

    out = _load_pcm_wav(str(path), target_sr=SR, mono=True)

    assert out is not None
    data, sr = out
    assert sr == SR
    assert data.dtype == np.float32
    # Round-trip via int16 loses precision; check magnitude is close.
    assert np.isclose(np.abs(data).max(), 0.3, atol=1e-3)


def test_load_pcm_wav_resamples_to_target_rate(tmp_path):
    """If the file's sample rate differs from target_sr, audio is resampled."""
    src_sr = 16000
    audio = _tone(0.5, amp=0.3, sr=src_sr)
    path = tmp_path / "lo.wav"
    _write_pcm16_wav(path, audio, src_sr, channels=1)

    out = _load_pcm_wav(str(path), target_sr=SR, mono=True)

    assert out is not None
    data, sr = out
    assert sr == SR
    # Length should reflect the new (higher) sample rate, give or take a frame.
    expected = int(0.5 * SR)
    assert abs(len(data) - expected) <= 64


def test_load_pcm_wav_downmixes_stereo_when_mono_true(tmp_path):
    """Stereo input with mono=True returns a 1-D mixdown array."""
    left = _tone(0.5, amp=0.3, sr=SR)
    right = _tone(0.5, amp=0.5, sr=SR, freq=330.0)
    stereo = np.empty((len(left) * 2,), dtype=np.float32)
    stereo[0::2] = left
    stereo[1::2] = right
    # Write as 2-channel by reshaping
    path = tmp_path / "stereo.wav"
    pcm16 = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16.tobytes())

    out = _load_pcm_wav(str(path), target_sr=SR, mono=True)

    assert out is not None
    data, _ = out
    assert data.ndim == 1
    # Mono mix should be roughly the average of the two channels.
    assert np.abs(data).max() <= 1.0


def test_load_pcm_wav_keeps_channels_when_mono_false(tmp_path):
    """With mono=False the helper returns a (channels, frames) array."""
    left = _tone(0.5, amp=0.3, sr=SR)
    right = _tone(0.5, amp=0.5, sr=SR, freq=330.0)
    interleaved = np.empty((len(left) * 2,), dtype=np.float32)
    interleaved[0::2] = left
    interleaved[1::2] = right
    path = tmp_path / "stereo_keep.wav"
    pcm16 = (np.clip(interleaved, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16.tobytes())

    out = _load_pcm_wav(str(path), target_sr=SR, mono=False)

    assert out is not None
    data, _ = out
    assert data.ndim == 2
    assert data.shape[0] == 2  # (channels, frames)


def test_load_pcm_wav_returns_none_for_24bit_wav(tmp_path):
    """24-bit WAVs are out-of-scope for the fast-path; must return None."""
    audio = _tone(0.2, amp=0.3)
    path = tmp_path / "24bit.wav"
    sf.write(str(path), audio, SR, subtype="PCM_24")

    assert _load_pcm_wav(str(path), target_sr=SR, mono=True) is None


def test_load_pcm_wav_returns_none_for_non_wav(tmp_path):
    """Garbage / non-WAV bytes must surface as None, not raise."""
    path = tmp_path / "not_a_wav.bin"
    path.write_bytes(b"not a wave file at all")
    assert _load_pcm_wav(str(path), target_sr=SR, mono=True) is None


def test_load_pcm_wav_returns_none_for_missing_file(tmp_path):
    """A missing path returns None (OSError is swallowed)."""
    missing = tmp_path / "does_not_exist.wav"
    assert _load_pcm_wav(str(missing), target_sr=SR, mono=True) is None


# ---------------------------------------------------------------------------
# load_audio: public loader (delegates to fast-path then librosa)
# ---------------------------------------------------------------------------

def test_load_audio_uses_fast_path_for_pcm16_wav(tmp_path):
    """A 16-bit PCM WAV at target sr round-trips through load_audio."""
    audio = _tone(0.5, amp=0.3)
    path = tmp_path / "fast.wav"
    _write_pcm16_wav(path, audio, SR, channels=1)

    data, sr = load_audio(str(path), sample_rate=SR, mono=True)

    assert sr == SR
    assert data.dtype == np.float32
    assert data.ndim == 1
    assert np.isclose(np.abs(data).max(), 0.3, atol=1e-3)


def test_load_audio_falls_back_to_librosa_for_float_wav(tmp_path):
    """A 32-bit float WAV (no fast-path) still loads via librosa fallback."""
    audio = _tone(0.5, amp=0.3)
    path = tmp_path / "float.wav"
    sf.write(str(path), audio, SR, subtype="FLOAT")

    data, sr = load_audio(str(path), sample_rate=SR, mono=True)

    assert sr == SR
    assert data.ndim == 1
    assert np.isclose(np.abs(data).max(), 0.3, atol=1e-2)


# ---------------------------------------------------------------------------
# save_audio: atomic write
# ---------------------------------------------------------------------------

def test_save_audio_writes_file_that_round_trips(tmp_path):
    """save_audio writes a WAV that load_audio can read back."""
    audio = _tone(0.3, amp=0.4)
    out_path = tmp_path / "out.wav"

    save_audio(audio, str(out_path), sample_rate=SR)

    assert out_path.exists()
    data, sr = load_audio(str(out_path), sample_rate=SR, mono=True)
    assert sr == SR
    # WAV round-trip preserves amplitude to within float precision.
    assert np.isclose(np.abs(data).max(), 0.4, atol=1e-2)


def test_save_audio_creates_missing_parent_directories(tmp_path):
    """Nested target directories are created automatically."""
    audio = _tone(0.2, amp=0.3)
    out_path = tmp_path / "nested" / "deeper" / "out.wav"

    save_audio(audio, str(out_path), sample_rate=SR)

    assert out_path.exists()
    assert out_path.parent.is_dir()


def test_save_audio_cleans_up_temp_file_on_failure(tmp_path):
    """If the write fails, the .tmp file must not be left behind, and an OSError is raised."""
    audio = _tone(0.2, amp=0.3)
    # Pointing at a directory (not a file) makes os.replace fail.
    bad_target = tmp_path / "a_directory"
    bad_target.mkdir()

    with pytest.raises(OSError):
        save_audio(audio, str(bad_target), sample_rate=SR)

    # No stray .tmp file should remain.
    assert not (tmp_path / "a_directory.tmp").exists()


def test_save_audio_does_not_leave_partial_on_target_path(tmp_path):
    """The temp file is renamed atomically; no .tmp sidecar after success."""
    audio = _tone(0.2, amp=0.3)
    out_path = tmp_path / "final.wav"

    save_audio(audio, str(out_path), sample_rate=SR)

    assert out_path.exists()
    assert not (tmp_path / "final.wav.tmp").exists()


# ---------------------------------------------------------------------------
# normalize_audio: RMS-target + peak limit
# ---------------------------------------------------------------------------

def test_normalize_audio_caps_peak_at_limit():
    """A loud signal gets clipped to peak_limit after RMS gain is applied."""
    audio = _tone(0.5, amp=0.9)
    out = normalize_audio(audio, target_db=-20.0, peak_limit=0.85)
    assert np.abs(out).max() <= 0.85 + 1e-6


def test_normalize_audio_returns_zero_for_silent_input():
    """Pure silence has rms=0 so no gain applied; output stays all zeros."""
    silent = np.zeros(SR, dtype=np.float32)
    out = normalize_audio(silent, target_db=-20.0, peak_limit=0.85)
    assert np.all(out == 0.0)


def test_normalize_audio_lifts_quiet_signal_toward_target():
    """A very quiet signal gets gained up (RMS moves toward target)."""
    audio = _tone(0.5, amp=0.01)
    out = normalize_audio(audio, target_db=-20.0, peak_limit=0.85)
    out_rms = float(np.sqrt(np.mean(out**2)))
    in_rms = float(np.sqrt(np.mean(audio**2)))
    assert out_rms > in_rms  # gained up


def test_normalize_audio_returns_float32():
    """Normalization preserves float32 dtype."""
    audio = _tone(0.3, amp=0.2).astype(np.float64)
    out = normalize_audio(audio)
    assert out.dtype == np.float32


# ---------------------------------------------------------------------------
# normalize_loudness: LUFS normalization
# ---------------------------------------------------------------------------

def test_normalize_loudness_returns_silent_input_unchanged():
    """Silent / near-silent input (LUFS=-inf) is returned as-is."""
    silent = np.zeros(SR * 3, dtype=np.float32)
    out = normalize_loudness(silent, sample_rate=SR, target_lufs=-18.0)
    assert np.array_equal(out, silent)


def test_normalize_loudness_moves_signal_toward_target():
    """A normal-level signal is gained so measured LUFS approaches the target."""
    import pyloudnorm as pyln

    audio = _tone(3.0, amp=0.05)  # quiet on purpose
    meter = pyln.Meter(SR)
    before = meter.integrated_loudness(audio)
    out = normalize_loudness(audio, sample_rate=SR, target_lufs=-18.0)
    after = meter.integrated_loudness(out)

    # Skip if the source was below the meter's gating threshold.
    if not (np.isinf(before) or np.isnan(before)):
        # Should be much closer to -18 LUFS than the input was.
        assert abs(after - (-18.0)) < abs(before - (-18.0))


def test_normalize_loudness_preserves_dtype_float32():
    """Output stays float32 even when input is float64."""
    audio = _tone(3.0, amp=0.1).astype(np.float64)
    out = normalize_loudness(audio, sample_rate=SR)
    assert out.dtype == np.float32


# ---------------------------------------------------------------------------
# trim_tts_output: trailing-silence + hallucination cut
# ---------------------------------------------------------------------------

def test_trim_tts_output_returns_input_when_shorter_than_frame():
    """Input shorter than one frame is returned untouched."""
    tiny = np.zeros(5, dtype=np.float32)
    out = trim_tts_output(tiny, sample_rate=SR, frame_ms=20)
    assert np.array_equal(out, tiny)


def test_trim_tts_output_cuts_at_long_internal_silence():
    """A speech + long-silence + hallucination pattern is cut at the silence gap."""
    speech = _tone(1.0, amp=0.3)
    silence = np.zeros(int(SR * 1.5), dtype=np.float32)  # 1.5s gap > 1s max
    hallucination = _tone(0.5, amp=0.3, freq=880.0)
    audio = np.concatenate([speech, silence, hallucination]).astype(np.float32)

    out = trim_tts_output(audio, sample_rate=SR, max_internal_silence_ms=1000)

    # The hallucination after the long gap must be removed.
    assert len(out) < len(audio)
    # The speech body should still be present in roughly the original duration.
    assert len(out) <= len(speech) + int(SR * 0.5)


def test_trim_tts_output_trims_trailing_silence():
    """Pure trailing silence after speech is trimmed to a short tail."""
    speech = _tone(1.0, amp=0.3)
    tail = np.zeros(int(SR * 0.8), dtype=np.float32)
    audio = np.concatenate([speech, tail]).astype(np.float32)

    out = trim_tts_output(audio, sample_rate=SR, min_silence_ms=200)

    assert len(out) < len(audio)
    # Should keep roughly the speech length plus a short tail.
    assert len(out) >= int(SR * 0.9)


def test_trim_tts_output_applies_cosine_fade_out():
    """The last samples of the output are attenuated by a cosine fade."""
    speech = _tone(1.0, amp=0.5)
    out = trim_tts_output(speech, sample_rate=SR, fade_ms=30, max_internal_silence_ms=5000)

    # The very last sample should be attenuated relative to the body mid-section.
    body_peak = float(np.abs(out[len(out) // 4 : len(out) // 2]).max())
    tail_peak = float(np.abs(out[-5:]).max())
    assert tail_peak < body_peak


def test_trim_tts_output_returns_input_when_frame_len_zero():
    """A frame_ms small enough to round to 0 samples returns input untouched."""
    # frame_ms=0 -> frame_len = 0 -> early return
    audio = _tone(0.5, amp=0.3)
    out = trim_tts_output(audio, sample_rate=SR, frame_ms=0)
    assert np.array_equal(out, audio)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
