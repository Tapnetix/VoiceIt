/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Body for POST /speak — non-MCP REST surface that mirrors voiceit.speak.
 */
export type SpeakRequest = {
    text: string;
    /**
     * Voice profile name or id. Falls back to per-client binding, then default.
     */
    profile?: (string | null);
    engine?: (string | null);
    /**
     * When true and the profile has a personality prompt, the input text is rewritten in-character before TTS. When null, the per-client binding's default_personality flag decides.
     */
    personality?: (boolean | null);
    language?: (string | null);
};

