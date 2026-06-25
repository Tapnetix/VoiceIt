/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { RefinementFlagsModel } from './RefinementFlagsModel';
/**
 * Response model for ``POST /captures``.
 *
 * Adds ``auto_refine`` and ``allow_auto_paste`` — the server-side settings
 * captured at the moment the capture was created. The client reads these to
 * decide whether to chain a refinement request and whether to fire the
 * synthetic-paste pipeline, so it doesn't need a synced local copy of the
 * capture_settings table across sibling Tauri webviews.
 */
export type CaptureCreateResponse = {
    id: string;
    audio_path: string;
    source: string;
    language?: (string | null);
    duration_ms?: (number | null);
    transcript_raw: string;
    transcript_refined?: (string | null);
    stt_model?: (string | null);
    llm_model?: (string | null);
    refinement_flags?: (RefinementFlagsModel | null);
    created_at: string;
    auto_refine: boolean;
    allow_auto_paste: boolean;
};

