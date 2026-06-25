/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Partial update for capture settings — every field is optional.
 */
export type CaptureSettingsUpdate = {
    stt_model?: (string | null);
    language?: (string | null);
    auto_refine?: (boolean | null);
    llm_model?: (string | null);
    smart_cleanup?: (boolean | null);
    self_correction?: (boolean | null);
    preserve_technical?: (boolean | null);
    allow_auto_paste?: (boolean | null);
    default_playback_voice_id?: (string | null);
    hotkey_enabled?: (boolean | null);
    chord_push_to_talk_keys?: (Array<string> | null);
    chord_toggle_to_talk_keys?: (Array<string> | null);
};

