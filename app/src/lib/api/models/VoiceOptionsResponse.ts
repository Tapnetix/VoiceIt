/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { VoiceProfileSummary } from './VoiceProfileSummary';
/**
 * Three-section voice picker payload. (Contract 02)
 */
export type VoiceOptionsResponse = {
    library?: Array<VoiceProfileSummary>;
    book?: Array<VoiceProfileSummary>;
    presets?: Array<Record<string, any>>;
};

