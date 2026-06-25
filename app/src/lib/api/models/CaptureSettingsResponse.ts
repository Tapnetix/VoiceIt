/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Server-persisted defaults for the capture / refine flow.
 */
export type CaptureSettingsResponse = {
    stt_model?: string;
    language?: string;
    auto_refine?: boolean;
    llm_model?: string;
    smart_cleanup?: boolean;
    self_correction?: boolean;
    preserve_technical?: boolean;
    allow_auto_paste?: boolean;
    default_playback_voice_id?: (string | null);
    hotkey_enabled?: boolean;
    chord_push_to_talk_keys?: Array<string>;
    chord_toggle_to_talk_keys?: Array<string>;
};

