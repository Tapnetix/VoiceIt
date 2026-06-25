/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $CaptureResponse = {
    description: `Response model for a capture.`,
    properties: {
        id: {
            type: 'string',
            isRequired: true,
        },
        audio_path: {
            type: 'string',
            isRequired: true,
        },
        source: {
            type: 'string',
            isRequired: true,
        },
        language: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        duration_ms: {
            type: 'any-of',
            contains: [{
                type: 'number',
            }, {
                type: 'null',
            }],
        },
        transcript_raw: {
            type: 'string',
            isRequired: true,
        },
        transcript_refined: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        stt_model: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        llm_model: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        refinement_flags: {
            type: 'any-of',
            contains: [{
                type: 'RefinementFlagsModel',
            }, {
                type: 'null',
            }],
        },
        created_at: {
            type: 'string',
            isRequired: true,
            format: 'date-time',
        },
    },
} as const;
