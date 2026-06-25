/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $GenerateRequest = {
    description: `Optional body for generate / chapter-generate endpoints. (Contract 03)`,
    properties: {
        engine: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        model_size: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        overwrite_errors: {
            type: 'boolean',
        },
    },
} as const;
