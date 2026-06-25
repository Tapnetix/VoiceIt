/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $VoiceOptionsResponse = {
    description: `Three-section voice picker payload. (Contract 02)`,
    properties: {
        library: {
            type: 'array',
            contains: {
                type: 'VoiceProfileSummary',
            },
        },
        book: {
            type: 'array',
            contains: {
                type: 'VoiceProfileSummary',
            },
        },
        presets: {
            type: 'array',
            contains: {
                type: 'dictionary',
                contains: {
                    properties: {
                    },
                },
            },
        },
    },
} as const;
