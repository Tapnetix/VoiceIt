/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $GenerationSettingsUpdate = {
    description: `Partial update for generation settings — every field is optional.`,
    properties: {
        max_chunk_chars: {
            type: 'any-of',
            contains: [{
                type: 'number',
                maximum: 5000,
                minimum: 100,
            }, {
                type: 'null',
            }],
        },
        crossfade_ms: {
            type: 'any-of',
            contains: [{
                type: 'number',
                maximum: 500,
            }, {
                type: 'null',
            }],
        },
        normalize_audio: {
            type: 'any-of',
            contains: [{
                type: 'boolean',
            }, {
                type: 'null',
            }],
        },
        autoplay_on_generate: {
            type: 'any-of',
            contains: [{
                type: 'boolean',
            }, {
                type: 'null',
            }],
        },
    },
} as const;
