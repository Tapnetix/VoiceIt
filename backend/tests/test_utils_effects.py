"""
Unit tests for :mod:`backend.utils.effects` — the audio post-processing
effects engine.

Covers the four public entry points:

  - :func:`get_available_effects`
  - :func:`get_builtin_presets`
  - :func:`validate_effects_chain`
  - :func:`build_pedalboard`
  - :func:`apply_effects`

Tests use real ``pedalboard`` plugin classes (no first-party module mocks)
and real numpy audio buffers; assertions check observable behaviour
(returned data structures, audio sample values, validation messages).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from pedalboard import (
    Chorus,
    Compressor,
    Delay,
    Gain,
    HighpassFilter,
    LowpassFilter,
    Pedalboard,
    PitchShift,
    Reverb,
)

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.effects import (  # noqa: E402
    BUILTIN_PRESETS,
    EFFECT_REGISTRY,
    apply_effects,
    build_pedalboard,
    get_available_effects,
    get_builtin_presets,
    validate_effects_chain,
)


SR = 24000


def _tone(duration_s: float = 1.0, amp: float = 0.3, freq: float = 220.0) -> np.ndarray:
    """Return a mono sine-wave tone as float32."""
    n = int(duration_s * SR)
    t = np.arange(n, dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# get_available_effects
# ---------------------------------------------------------------------------


def test_get_available_effects_lists_every_registered_type():
    available = get_available_effects()
    types = {item["type"] for item in available}
    assert types == set(EFFECT_REGISTRY.keys())


def test_get_available_effects_returns_label_description_and_params_for_each():
    available = get_available_effects()
    for item in available:
        assert isinstance(item["label"], str) and item["label"]
        assert isinstance(item["description"], str) and item["description"]
        assert isinstance(item["params"], dict)
        # Every param should expose the default/min/max/description contract
        # the frontend renders the editor against.
        for pname, pdef in item["params"].items():
            assert isinstance(pname, str)
            assert "default" in pdef
            assert "min" in pdef
            assert "max" in pdef
            assert "description" in pdef


def test_get_available_effects_does_not_leak_the_internal_cls_field():
    # The registry stores plugin classes under "cls" — that's an implementation
    # detail that must not show up in the JSON-serialisable API payload.
    available = get_available_effects()
    for item in available:
        assert "cls" not in item


def test_get_available_effects_returns_a_fresh_copy_safe_to_mutate():
    # Callers (FastAPI route handlers) sometimes mutate the dict in flight;
    # mutating one returned copy must not corrupt subsequent calls.
    first = get_available_effects()
    first[0]["label"] = "MUTATED"
    first[0]["params"]["__injected__"] = {"default": 999}
    second = get_available_effects()
    assert second[0]["label"] != "MUTATED"
    assert "__injected__" not in second[0]["params"]


# ---------------------------------------------------------------------------
# get_builtin_presets
# ---------------------------------------------------------------------------


def test_get_builtin_presets_exposes_all_four_documented_presets():
    presets = get_builtin_presets()
    assert set(presets.keys()) == {"robotic", "radio", "echo_chamber", "deep_voice"}


def test_get_builtin_presets_have_valid_effect_types():
    presets = get_builtin_presets()
    for preset in presets.values():
        for effect in preset["effects_chain"]:
            assert effect["type"] in EFFECT_REGISTRY
            assert "enabled" in effect
            assert isinstance(effect["params"], dict)


def test_every_builtin_preset_passes_validate_effects_chain():
    presets = get_builtin_presets()
    for preset_id, preset in presets.items():
        err = validate_effects_chain(preset["effects_chain"])
        assert err is None, f"preset {preset_id!r} failed validation: {err}"


# ---------------------------------------------------------------------------
# validate_effects_chain
# ---------------------------------------------------------------------------


def test_validate_accepts_empty_chain():
    assert validate_effects_chain([]) is None


def test_validate_rejects_non_list():
    assert validate_effects_chain({"not": "a list"}) == "effects_chain must be a list"  # type: ignore[arg-type]


def test_validate_rejects_non_dict_entries():
    msg = validate_effects_chain(["not a dict"])  # type: ignore[list-item]
    assert msg == "Effect at index 0 must be a dict"


def test_validate_rejects_unknown_effect_type():
    msg = validate_effects_chain([{"type": "warp_drive", "params": {}}])
    assert msg is not None
    assert "warp_drive" in msg
    assert "Unknown effect type" in msg


def test_validate_rejects_non_dict_params():
    msg = validate_effects_chain([{"type": "gain", "params": "not a dict"}])
    assert msg is not None
    assert "params must be a dict" in msg


def test_validate_rejects_unknown_param_name():
    msg = validate_effects_chain([{"type": "gain", "params": {"volume_db": 0.0}}])
    assert msg is not None
    assert "unknown param" in msg
    assert "volume_db" in msg


def test_validate_rejects_non_numeric_param_value():
    msg = validate_effects_chain([{"type": "gain", "params": {"gain_db": "loud"}}])
    assert msg is not None
    assert "must be a number" in msg


def test_validate_rejects_param_below_min():
    msg = validate_effects_chain([{"type": "gain", "params": {"gain_db": -999.0}}])
    assert msg is not None
    assert "must be between" in msg
    assert "-999" in msg


def test_validate_rejects_param_above_max():
    msg = validate_effects_chain([{"type": "gain", "params": {"gain_db": 999.0}}])
    assert msg is not None
    assert "must be between" in msg


def test_validate_accepts_int_for_float_param():
    # Sliders in the UI sometimes send ints (e.g. 0 instead of 0.0);
    # the validator should accept both.
    assert validate_effects_chain([{"type": "gain", "params": {"gain_db": 0}}]) is None


def test_validate_accepts_chain_with_omitted_params():
    # Omitted params fall back to defaults at build time.
    assert validate_effects_chain([{"type": "reverb"}]) is None


def test_validate_includes_effect_index_in_error_messages():
    chain = [
        {"type": "gain", "params": {"gain_db": 0.0}},
        {"type": "gain", "params": {"gain_db": 999.0}},
    ]
    msg = validate_effects_chain(chain)
    assert msg is not None
    assert "index 1" in msg


# ---------------------------------------------------------------------------
# build_pedalboard
# ---------------------------------------------------------------------------


_EXPECTED_PLUGIN_CLS = {
    "chorus": Chorus,
    "reverb": Reverb,
    "delay": Delay,
    "compressor": Compressor,
    "gain": Gain,
    "highpass": HighpassFilter,
    "lowpass": LowpassFilter,
    "pitch_shift": PitchShift,
}


def test_build_pedalboard_returns_a_pedalboard_instance():
    board = build_pedalboard([{"type": "gain", "params": {"gain_db": 0.0}}])
    assert isinstance(board, Pedalboard)


def test_build_pedalboard_empty_chain_yields_empty_board():
    board = build_pedalboard([])
    assert isinstance(board, Pedalboard)
    assert len(list(board)) == 0


@pytest.mark.parametrize("effect_type,cls", list(_EXPECTED_PLUGIN_CLS.items()))
def test_build_pedalboard_maps_each_registered_type_to_its_plugin_class(effect_type, cls):
    board = build_pedalboard([{"type": effect_type, "enabled": True, "params": {}}])
    plugins = list(board)
    assert len(plugins) == 1
    assert isinstance(plugins[0], cls)


def test_build_pedalboard_preserves_order_of_enabled_effects():
    chain = [
        {"type": "highpass", "enabled": True, "params": {}},
        {"type": "gain", "enabled": True, "params": {}},
        {"type": "lowpass", "enabled": True, "params": {}},
    ]
    plugins = list(build_pedalboard(chain))
    assert [type(p) for p in plugins] == [HighpassFilter, Gain, LowpassFilter]


def test_build_pedalboard_skips_disabled_effects():
    chain = [
        {"type": "gain", "enabled": True, "params": {"gain_db": 0.0}},
        {"type": "reverb", "enabled": False, "params": {}},
        {"type": "highpass", "enabled": True, "params": {}},
    ]
    plugins = list(build_pedalboard(chain))
    assert [type(p) for p in plugins] == [Gain, HighpassFilter]


def test_build_pedalboard_treats_missing_enabled_as_enabled():
    # The route layer often omits ``enabled`` for newly-added effects;
    # build_pedalboard should default to "on".
    plugins = list(build_pedalboard([{"type": "gain", "params": {"gain_db": 0.0}}]))
    assert len(plugins) == 1


def test_build_pedalboard_applies_provided_param_values():
    plugins = list(build_pedalboard([{"type": "gain", "params": {"gain_db": 12.0}}]))
    assert plugins[0].gain_db == pytest.approx(12.0)


def test_build_pedalboard_falls_back_to_registry_defaults_for_missing_params():
    plugins = list(build_pedalboard([{"type": "gain", "enabled": True, "params": {}}]))
    expected_default = EFFECT_REGISTRY["gain"]["params"]["gain_db"]["default"]
    assert plugins[0].gain_db == pytest.approx(expected_default)


# ---------------------------------------------------------------------------
# apply_effects
# ---------------------------------------------------------------------------


def test_apply_effects_returns_input_unchanged_for_empty_chain():
    audio = _tone(0.5)
    out = apply_effects(audio, SR, [])
    # Same object — the function short-circuits for empty chains.
    assert out is audio


def test_apply_effects_unity_gain_preserves_signal():
    audio = _tone(0.5, amp=0.4)
    out = apply_effects(audio, SR, [{"type": "gain", "params": {"gain_db": 0.0}}])
    assert out.shape == audio.shape
    np.testing.assert_allclose(out, audio, atol=1e-5)


def test_apply_effects_positive_gain_increases_peak_amplitude():
    audio = _tone(0.5, amp=0.2)
    out = apply_effects(audio, SR, [{"type": "gain", "params": {"gain_db": 12.0}}])
    # +12 dB ~= 4x amplitude.
    assert np.abs(out).max() > np.abs(audio).max() * 3.5


def test_apply_effects_negative_gain_decreases_peak_amplitude():
    audio = _tone(0.5, amp=0.5)
    out = apply_effects(audio, SR, [{"type": "gain", "params": {"gain_db": -20.0}}])
    # -20 dB ~= 0.1x amplitude.
    assert np.abs(out).max() < np.abs(audio).max() * 0.2


def test_apply_effects_preserves_1d_shape_for_1d_input():
    audio = _tone(0.5)
    out = apply_effects(audio, SR, [{"type": "gain", "params": {"gain_db": 0.0}}])
    assert out.ndim == 1


def test_apply_effects_preserves_2d_shape_for_2d_input():
    mono = _tone(0.5)
    audio_2d = mono[np.newaxis, :]  # (1, samples)
    out = apply_effects(audio_2d, SR, [{"type": "gain", "params": {"gain_db": 0.0}}])
    assert out.ndim == 2
    assert out.shape[0] == 1


def test_apply_effects_returns_float32_compatible_output():
    audio = _tone(0.5).astype(np.float64)
    out = apply_effects(audio, SR, [{"type": "gain", "params": {"gain_db": 0.0}}])
    # apply_effects casts to float32 internally; output dtype should be float
    # and the values should still match the original tone.
    assert np.issubdtype(out.dtype, np.floating)


def test_apply_effects_with_disabled_chain_returns_unchanged_signal():
    audio = _tone(0.5, amp=0.4)
    # Disabled gain of +24 dB; if the disabled flag is honoured the output
    # should be (numerically) equal to the input.
    out = apply_effects(
        audio,
        SR,
        [{"type": "gain", "enabled": False, "params": {"gain_db": 24.0}}],
    )
    np.testing.assert_allclose(out, audio, atol=1e-5)


def test_apply_effects_runs_each_builtin_preset_end_to_end():
    audio = _tone(0.5, amp=0.3)
    for preset_id, preset in get_builtin_presets().items():
        out = apply_effects(audio, SR, preset["effects_chain"])
        assert isinstance(out, np.ndarray), f"preset {preset_id!r} returned non-array"
        assert out.ndim == 1
        # Pedalboard effects may change length slightly (tail/latency) but
        # the output should be non-trivially long for a 0.5s input.
        assert out.size > SR // 4
        # And it should not be all-zero / NaN.
        assert np.isfinite(out).all(), f"preset {preset_id!r} produced non-finite samples"
        assert np.abs(out).max() > 0.0, f"preset {preset_id!r} produced silence"
