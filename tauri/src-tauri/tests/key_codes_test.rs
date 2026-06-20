//! Integration tests for `key_codes::key_from_str`.
//!
//! `key_from_str` is the pure-logic bridge between the browser-side
//! `KeyboardEvent.code` strings persisted in `capture_settings` and the
//! `keytap::Key` variants the chord engine matches against. Everything here
//! exercises that mapping in isolation — no OS hooks, no tauri runtime.

use keytap::Key;
use voiceit::key_codes::key_from_str;

// ---- Modifiers: left/right fidelity ----------------------------------------

#[test]
fn resolves_left_and_right_alt_to_distinct_keys() {
    assert_eq!(key_from_str("AltLeft"), Some(Key::AltLeft));
    assert_eq!(key_from_str("AltRight"), Some(Key::AltRight));
}

#[test]
fn resolves_left_and_right_control_to_distinct_keys() {
    assert_eq!(key_from_str("ControlLeft"), Some(Key::ControlLeft));
    assert_eq!(key_from_str("ControlRight"), Some(Key::ControlRight));
}

#[test]
fn resolves_left_and_right_meta_to_distinct_keys() {
    assert_eq!(key_from_str("MetaLeft"), Some(Key::MetaLeft));
    assert_eq!(key_from_str("MetaRight"), Some(Key::MetaRight));
}

#[test]
fn resolves_left_and_right_shift_to_distinct_keys() {
    assert_eq!(key_from_str("ShiftLeft"), Some(Key::ShiftLeft));
    assert_eq!(key_from_str("ShiftRight"), Some(Key::ShiftRight));
}

#[test]
fn resolves_caps_lock() {
    assert_eq!(key_from_str("CapsLock"), Some(Key::CapsLock));
}

// ---- Legacy aliases: rows written before the keytap swap keep working ------

#[test]
fn legacy_alt_alias_resolves_to_left_alt() {
    // The pre-keytap frontend persisted bare "Alt" / "AltGr". Older
    // capture_settings rows must still round-trip; we map them onto the
    // physical left/right Alt key.
    assert_eq!(key_from_str("Alt"), Some(Key::AltLeft));
    assert_eq!(key_from_str("AltGr"), Some(Key::AltRight));
}

#[test]
fn legacy_arrow_aliases_resolve_to_arrow_keys() {
    assert_eq!(key_from_str("UpArrow"), Some(Key::ArrowUp));
    assert_eq!(key_from_str("DownArrow"), Some(Key::ArrowDown));
    assert_eq!(key_from_str("LeftArrow"), Some(Key::ArrowLeft));
    assert_eq!(key_from_str("RightArrow"), Some(Key::ArrowRight));
}

#[test]
fn legacy_digit_aliases_resolve_to_digit_keys() {
    assert_eq!(key_from_str("Num0"), Some(Key::Digit0));
    assert_eq!(key_from_str("Num5"), Some(Key::Digit5));
    assert_eq!(key_from_str("Num9"), Some(Key::Digit9));
}

#[test]
fn legacy_punctuation_aliases_resolve_to_canonical_keys() {
    assert_eq!(key_from_str("BackQuote"), Some(Key::Backtick));
    assert_eq!(key_from_str("LeftBracket"), Some(Key::BracketLeft));
    assert_eq!(key_from_str("RightBracket"), Some(Key::BracketRight));
    assert_eq!(key_from_str("SemiColon"), Some(Key::Semicolon));
    assert_eq!(key_from_str("BackSlash"), Some(Key::Backslash));
    assert_eq!(key_from_str("Dot"), Some(Key::Period));
}

#[test]
fn legacy_return_alias_resolves_to_enter() {
    assert_eq!(key_from_str("Return"), Some(Key::Enter));
}

// ---- W3C canonical names: whitespace / navigation --------------------------

#[test]
fn resolves_whitespace_keys() {
    assert_eq!(key_from_str("Space"), Some(Key::Space));
    assert_eq!(key_from_str("Tab"), Some(Key::Tab));
    assert_eq!(key_from_str("Enter"), Some(Key::Enter));
    assert_eq!(key_from_str("Backspace"), Some(Key::Backspace));
}

#[test]
fn resolves_editing_and_navigation_keys() {
    assert_eq!(key_from_str("Delete"), Some(Key::Delete));
    assert_eq!(key_from_str("Escape"), Some(Key::Escape));
    assert_eq!(key_from_str("Insert"), Some(Key::Insert));
    assert_eq!(key_from_str("Home"), Some(Key::Home));
    assert_eq!(key_from_str("End"), Some(Key::End));
    assert_eq!(key_from_str("PageUp"), Some(Key::PageUp));
    assert_eq!(key_from_str("PageDown"), Some(Key::PageDown));
}

#[test]
fn resolves_arrow_keys_canonical_form() {
    assert_eq!(key_from_str("ArrowUp"), Some(Key::ArrowUp));
    assert_eq!(key_from_str("ArrowDown"), Some(Key::ArrowDown));
    assert_eq!(key_from_str("ArrowLeft"), Some(Key::ArrowLeft));
    assert_eq!(key_from_str("ArrowRight"), Some(Key::ArrowRight));
}

// ---- Function row ---------------------------------------------------------

#[test]
fn resolves_all_function_row_keys() {
    let cases = [
        ("F1", Key::F1), ("F2", Key::F2), ("F3", Key::F3), ("F4", Key::F4),
        ("F5", Key::F5), ("F6", Key::F6), ("F7", Key::F7), ("F8", Key::F8),
        ("F9", Key::F9), ("F10", Key::F10), ("F11", Key::F11), ("F12", Key::F12),
    ];
    for (name, expected) in cases {
        assert_eq!(key_from_str(name), Some(expected), "expected {name} -> {expected:?}");
    }
}

// ---- Letters: KeyA-style names map to bare letter variants ----------------

#[test]
fn key_a_through_key_z_map_to_bare_letter_variants() {
    let cases = [
        ("KeyA", Key::A), ("KeyB", Key::B), ("KeyC", Key::C),
        ("KeyD", Key::D), ("KeyE", Key::E), ("KeyF", Key::F),
        ("KeyG", Key::G), ("KeyH", Key::H), ("KeyI", Key::I),
        ("KeyJ", Key::J), ("KeyK", Key::K), ("KeyL", Key::L),
        ("KeyM", Key::M), ("KeyN", Key::N), ("KeyO", Key::O),
        ("KeyP", Key::P), ("KeyQ", Key::Q), ("KeyR", Key::R),
        ("KeyS", Key::S), ("KeyT", Key::T), ("KeyU", Key::U),
        ("KeyV", Key::V), ("KeyW", Key::W), ("KeyX", Key::X),
        ("KeyY", Key::Y), ("KeyZ", Key::Z),
    ];
    for (name, expected) in cases {
        assert_eq!(key_from_str(name), Some(expected), "expected {name} -> {expected:?}");
    }
}

// ---- Digits: Digit0-9 canonical form ---------------------------------------

#[test]
fn digit0_through_digit9_canonical_names_resolve() {
    let cases = [
        ("Digit0", Key::Digit0), ("Digit1", Key::Digit1), ("Digit2", Key::Digit2),
        ("Digit3", Key::Digit3), ("Digit4", Key::Digit4), ("Digit5", Key::Digit5),
        ("Digit6", Key::Digit6), ("Digit7", Key::Digit7), ("Digit8", Key::Digit8),
        ("Digit9", Key::Digit9),
    ];
    for (name, expected) in cases {
        assert_eq!(key_from_str(name), Some(expected), "expected {name} -> {expected:?}");
    }
}

// ---- Punctuation: canonical names -----------------------------------------

#[test]
fn resolves_canonical_punctuation_keys() {
    assert_eq!(key_from_str("Backquote"), Some(Key::Backtick));
    assert_eq!(key_from_str("Minus"), Some(Key::Minus));
    assert_eq!(key_from_str("Equal"), Some(Key::Equal));
    assert_eq!(key_from_str("BracketLeft"), Some(Key::BracketLeft));
    assert_eq!(key_from_str("BracketRight"), Some(Key::BracketRight));
    assert_eq!(key_from_str("Semicolon"), Some(Key::Semicolon));
    assert_eq!(key_from_str("Quote"), Some(Key::Quote));
    assert_eq!(key_from_str("Backslash"), Some(Key::Backslash));
    assert_eq!(key_from_str("Comma"), Some(Key::Comma));
    assert_eq!(key_from_str("Period"), Some(Key::Period));
    assert_eq!(key_from_str("Slash"), Some(Key::Slash));
}

// ---- Unknown names: rejected so the chord engine never silently drops keys -

#[test]
fn returns_none_for_unknown_key_name() {
    // The docstring is explicit: the command surface rejects unknown names
    // so we never silently drop a key from a stored chord.
    assert_eq!(key_from_str("Nonsense"), None);
    assert_eq!(key_from_str(""), None);
}

#[test]
fn returns_none_for_case_mismatched_canonical_name() {
    // The W3C spec is case-sensitive ("KeyA", not "keya"). Browsers emit the
    // canonical casing and stored chords mirror that; a lower-cased entry is
    // not a recognized name.
    assert_eq!(key_from_str("keya"), None);
    assert_eq!(key_from_str("arrowup"), None);
    assert_eq!(key_from_str("space"), None);
}

#[test]
fn returns_none_for_bare_letter_or_digit_glyph() {
    // The input contract is the W3C `KeyboardEvent.code` identifier, not the
    // glyph. "A" / "0" are not canonical codes (those would be "KeyA" / "Digit0").
    assert_eq!(key_from_str("A"), None);
    assert_eq!(key_from_str("0"), None);
}

#[test]
fn returns_none_for_whitespace_padded_name() {
    // No trimming inside `key_from_str` — the persistence layer is expected
    // to hand over already-canonical strings. A padded name is a bug upstream,
    // and silently accepting it would hide that bug.
    assert_eq!(key_from_str(" Space"), None);
    assert_eq!(key_from_str("Space "), None);
}

// ---- Round-trip evidence over the full canonical surface -------------------

#[test]
fn every_canonical_name_round_trips_to_a_distinct_keytap_variant() {
    // The on-disk contract: a chord written by the frontend as the W3C
    // canonical name must always come back as some `keytap::Key`. We don't
    // care which variants here — just that the canonical surface is total.
    let canonical = [
        "AltLeft", "AltRight", "ControlLeft", "ControlRight",
        "MetaLeft", "MetaRight", "ShiftLeft", "ShiftRight", "CapsLock",
        "Space", "Tab", "Enter", "Backspace", "Delete", "Escape",
        "Insert", "Home", "End", "PageUp", "PageDown",
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
        "Digit0", "Digit1", "Digit2", "Digit3", "Digit4",
        "Digit5", "Digit6", "Digit7", "Digit8", "Digit9",
        "KeyA", "KeyB", "KeyC", "KeyD", "KeyE", "KeyF", "KeyG", "KeyH",
        "KeyI", "KeyJ", "KeyK", "KeyL", "KeyM", "KeyN", "KeyO", "KeyP",
        "KeyQ", "KeyR", "KeyS", "KeyT", "KeyU", "KeyV", "KeyW", "KeyX",
        "KeyY", "KeyZ",
        "Backquote", "Minus", "Equal", "BracketLeft", "BracketRight",
        "Semicolon", "Quote", "Backslash", "Comma", "Period", "Slash",
    ];
    for name in canonical {
        assert!(
            key_from_str(name).is_some(),
            "canonical name {name:?} must resolve to a keytap::Key"
        );
    }
}
