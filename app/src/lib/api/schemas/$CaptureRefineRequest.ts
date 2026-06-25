/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $CaptureRefineRequest = {
    description: `Request to refine a capture's transcript via the LLM.`,
    properties: {
        flags: {
            type: 'any-of',
            contains: [{
                type: 'RefinementFlagsModel',
            }, {
                type: 'null',
            }],
        },
        model_size: {
            type: 'any-of',
            contains: [{
                type: 'string',
                pattern: '^(0\\.6B|1\\.7B|4B)$',
            }, {
                type: 'null',
            }],
        },
    },
} as const;
