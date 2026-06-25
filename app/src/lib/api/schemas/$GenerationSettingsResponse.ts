/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $GenerationSettingsResponse = {
    description: `Server-persisted defaults for the generation flow.`,
    properties: {
        max_chunk_chars: {
            type: 'number',
            maximum: 5000,
            minimum: 100,
        },
        crossfade_ms: {
            type: 'number',
            maximum: 500,
        },
        normalize_audio: {
            type: 'boolean',
        },
        autoplay_on_generate: {
            type: 'boolean',
        },
    },
} as const;
