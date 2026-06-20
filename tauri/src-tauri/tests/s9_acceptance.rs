//! S9 acceptance: the canonical key-code table is a total, injective
//! bridge between W3C `KeyboardEvent.code` strings (as persisted by the
//! frontend in `capture_settings`) and `keytap::Key` variants (as matched
//! by the chord engine in `hotkey_monitor`).
//!
//! The "round-trip" here is the on-disk contract end-to-end:
//!
//!     frontend writes "MetaRight"  ──►  capture_settings JSON
//!                                       │
//!                                       ▼
//!                                key_codes::key_from_str
//!                                       │
//!                                       ▼
//!                          hotkey_monitor matches Key::MetaRight
//!
//! For every canonical name documented in `src/key_codes.rs`, the
//! mapping must (S9.a) resolve to *some* `keytap::Key` (totality), and
//! (S9.b) resolve to a *distinct* `keytap::Key` per canonical name
//! (injectivity) — two different physical keys must never collapse to
//! one variant, otherwise a stored chord like `MetaLeft+KeyA` could
//! collide with `MetaRight+KeyA` after a round-trip and silently fire
//! the wrong action. Legacy aliases are then verified to (S9.c) fold
//! onto the same variant as their canonical name so older
//! capture_settings rows keep working byte-for-byte.

use keytap::Key;
use std::collections::{HashMap, HashSet};
use voiceit::key_codes::key_from_str;

/// Every canonical W3C `KeyboardEvent.code` identifier the frontend may
/// persist in `capture_settings`. Mirrors the canonical arms of
/// `key_codes::key_from_str`. Legacy aliases are exercised separately
/// in [`legacy_aliases`].
fn canonical_table() -> &'static [&'static str] {
    &[
        // Modifiers (left/right always distinguished)
        "AltLeft", "AltRight",
        "ControlLeft", "ControlRight",
        "MetaLeft", "MetaRight",
        "ShiftLeft", "ShiftRight",
        "CapsLock",
        // Whitespace / editing / navigation
        "Space", "Tab", "Enter", "Backspace",
        "Delete", "Escape", "Insert",
        "Home", "End", "PageUp", "PageDown",
        // Arrows
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        // Function row
        "F1", "F2", "F3", "F4", "F5", "F6",
        "F7", "F8", "F9", "F10", "F11", "F12",
        // Digits
        "Digit0", "Digit1", "Digit2", "Digit3", "Digit4",
        "Digit5", "Digit6", "Digit7", "Digit8", "Digit9",
        // Letters
        "KeyA", "KeyB", "KeyC", "KeyD", "KeyE", "KeyF",
        "KeyG", "KeyH", "KeyI", "KeyJ", "KeyK", "KeyL",
        "KeyM", "KeyN", "KeyO", "KeyP", "KeyQ", "KeyR",
        "KeyS", "KeyT", "KeyU", "KeyV", "KeyW", "KeyX",
        "KeyY", "KeyZ",
        // Punctuation / symbols
        "Backquote", "Minus", "Equal",
        "BracketLeft", "BracketRight",
        "Semicolon", "Quote", "Backslash",
        "Comma", "Period", "Slash",
    ]
}

/// Legacy alias → its canonical form. Older `capture_settings` rows
/// written before the keytap swap stored these strings, and the
/// migration story is "they keep working". Each row asserts that
/// reading the legacy alias yields the same `keytap::Key` as reading
/// the canonical name today.
fn legacy_aliases() -> &'static [(&'static str, &'static str)] {
    &[
        ("Alt", "AltLeft"),
        ("AltGr", "AltRight"),
        ("Return", "Enter"),
        ("UpArrow", "ArrowUp"),
        ("DownArrow", "ArrowDown"),
        ("LeftArrow", "ArrowLeft"),
        ("RightArrow", "ArrowRight"),
        ("Num0", "Digit0"),
        ("Num1", "Digit1"),
        ("Num2", "Digit2"),
        ("Num3", "Digit3"),
        ("Num4", "Digit4"),
        ("Num5", "Digit5"),
        ("Num6", "Digit6"),
        ("Num7", "Digit7"),
        ("Num8", "Digit8"),
        ("Num9", "Digit9"),
        ("BackQuote", "Backquote"),
        ("LeftBracket", "BracketLeft"),
        ("RightBracket", "BracketRight"),
        ("SemiColon", "Semicolon"),
        ("BackSlash", "Backslash"),
        ("Dot", "Period"),
    ]
}

#[test]
fn s9_canonical_table_resolves_every_name_to_some_keytap_variant() {
    // S9.a — Totality. The on-disk surface is `KeyboardEvent.code`; the
    // chord engine cannot match a key it never received. Any canonical
    // name the frontend may persist must therefore land on some
    // `keytap::Key`, no exceptions, otherwise `build_chord` would reject
    // a chord the user legitimately captured.
    let unresolved: Vec<&&str> = canonical_table()
        .iter()
        .filter(|name| key_from_str(name).is_none())
        .collect();

    assert!(
        unresolved.is_empty(),
        "canonical names with no keytap::Key mapping: {unresolved:?}"
    );
}

#[test]
fn s9_canonical_table_is_injective_so_distinct_keys_never_collide() {
    // S9.b — Injectivity. Two distinct canonical names must map to two
    // distinct `keytap::Key` variants. If e.g. "MetaLeft" and "MetaRight"
    // both resolved to `Key::MetaLeft`, a chord stored as `MetaRight+A`
    // would silently fire on `MetaLeft+A` after the round-trip. We
    // detect that class of bug by inverting the table: every variant
    // hit must come from exactly one canonical name.
    let mut by_key: HashMap<Key, Vec<&str>> = HashMap::new();
    for name in canonical_table() {
        let key = key_from_str(name)
            .unwrap_or_else(|| panic!("canonical name {name:?} did not resolve"));
        by_key.entry(key).or_default().push(name);
    }

    let collisions: Vec<(Key, Vec<&str>)> = by_key
        .into_iter()
        .filter(|(_, names)| names.len() > 1)
        .collect();

    assert!(
        collisions.is_empty(),
        "canonical names collapsed onto the same keytap::Key: {collisions:?}"
    );
}

#[test]
fn s9_legacy_aliases_fold_onto_their_canonical_keytap_variant() {
    // S9.c — Backward compatibility. Each legacy alias must resolve to
    // exactly the same `keytap::Key` as its modern canonical form, so
    // that older `capture_settings` rows produce byte-for-byte the same
    // chord set the frontend would produce today.
    let mut mismatches: Vec<(&str, &str)> = Vec::new();
    for (legacy, canonical) in legacy_aliases() {
        let legacy_key = key_from_str(legacy);
        let canonical_key = key_from_str(canonical);
        if legacy_key.is_none() || legacy_key != canonical_key {
            mismatches.push((legacy, canonical));
        }
    }

    assert!(
        mismatches.is_empty(),
        "legacy aliases that don't match their canonical form: {mismatches:?}"
    );
}

#[test]
fn s9_canonical_table_covers_every_modifier_with_left_right_fidelity() {
    // S9.d — The chord engine's whole point on the modifier row is
    // left/right fidelity (the docstring is explicit). A canonical
    // table that drops one side breaks the captured-chord contract
    // for users who hold e.g. right-Alt. Verify each side is present
    // and resolves to its own physical variant.
    let pairs = [
        ("AltLeft", Key::AltLeft, "AltRight", Key::AltRight),
        ("ControlLeft", Key::ControlLeft, "ControlRight", Key::ControlRight),
        ("MetaLeft", Key::MetaLeft, "MetaRight", Key::MetaRight),
        ("ShiftLeft", Key::ShiftLeft, "ShiftRight", Key::ShiftRight),
    ];
    for (left_name, left_key, right_name, right_key) in pairs {
        assert_eq!(
            key_from_str(left_name),
            Some(left_key),
            "{left_name} must resolve to {left_key:?}"
        );
        assert_eq!(
            key_from_str(right_name),
            Some(right_key),
            "{right_name} must resolve to {right_key:?}"
        );
        assert_ne!(
            key_from_str(left_name),
            key_from_str(right_name),
            "{left_name} and {right_name} must resolve to different keytap::Key variants"
        );
    }
}

#[test]
fn s9_unknown_names_are_rejected_so_chord_builds_fail_loudly() {
    // S9.e — The build_chord call site in `main.rs` propagates a `None`
    // here as a hard error ("Unsupported key in <chord> chord: <raw>").
    // That contract is what keeps a typoed or corrupted capture_settings
    // row from silently dropping a key out of the chord and turning a
    // 3-key chord into a 2-key one. Any name not in the canonical table
    // and not in the legacy alias list must therefore return `None`.
    let known: HashSet<&str> = canonical_table()
        .iter()
        .copied()
        .chain(legacy_aliases().iter().map(|(legacy, _)| *legacy))
        .collect();

    let probes = [
        "", " ", "Nonsense", "keya", "ARROWUP", "Space ", " Space",
        "A", "0", "Key", "Digit", "Foo42", "Numpad0",
    ];
    for probe in probes {
        assert!(
            !known.contains(probe),
            "probe {probe:?} accidentally appears in the canonical/legacy table; pick another"
        );
        assert_eq!(
            key_from_str(probe),
            None,
            "unknown name {probe:?} must be rejected so build_chord fails loudly"
        );
    }
}
