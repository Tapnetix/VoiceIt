/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $VoiceProfileSummary = {
    description: `Compact voice profile entry for the voice picker. (Contract 02)`,
    properties: {
        id: {
            type: 'string',
            isRequired: true,
        },
        name: {
            type: 'string',
            isRequired: true,
        },
        voice_type: {
            type: 'string',
            isRequired: true,
        },
        is_library: {
            type: 'boolean',
        },
        book_id: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
    },
} as const;
