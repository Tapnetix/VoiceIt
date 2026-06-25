/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $CaptureRetranscribeRequest = {
    description: `Request to re-run STT on a capture's audio with a different model.`,
    properties: {
        model: {
            type: 'any-of',
            contains: [{
                type: 'string',
                pattern: '^(base|small|medium|large|turbo)$',
            }, {
                type: 'null',
            }],
        },
        language: {
            type: 'any-of',
            contains: [{
                type: 'string',
                pattern: '^(en|zh|ja|ko|de|fr|ru|pt|es|it)$',
            }, {
                type: 'null',
            }],
        },
    },
} as const;
