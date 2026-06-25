/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { ModelReadiness } from './ModelReadiness';
/**
 * Backend gates that must be green before the global hotkey will fire.
 *
 * The frontend combines this with its own TCC permission checks (input
 * monitoring, accessibility) into the full dictation readiness checklist.
 * Hotkey-enabled is the user's intent toggle and lives outside this struct.
 */
export type CaptureReadinessResponse = {
    stt: ModelReadiness;
    llm: ModelReadiness;
};

