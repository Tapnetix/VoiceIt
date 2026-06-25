/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { RefinementFlagsModel } from './RefinementFlagsModel';
/**
 * Response model for a capture.
 */
export type CaptureResponse = {
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
};

