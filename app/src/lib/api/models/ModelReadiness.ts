/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Per-model entry in the dictation readiness checklist.
 *
 * ``model_name`` is the canonical id used by ``POST /models/download`` so the
 * frontend can wire a one-click "Download" button without a second lookup.
 * ``size`` is the user's chosen variant (e.g. "turbo", "0.6B"); ``display_name``
 * is what the checklist row should show ("Whisper Turbo").
 */
export type ModelReadiness = {
    ready: boolean;
    model_name: string;
    display_name: string;
    size: string;
    size_mb?: (number | null);
};

