/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $LLMGenerateRequest = {
    description: `Request model for LLM text generation.`,
    properties: {
        prompt: {
            type: 'string',
            isRequired: true,
            maxLength: 50000,
            minLength: 1,
        },
        system: {
            type: 'any-of',
            contains: [{
                type: 'string',
                maxLength: 4000,
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
        max_tokens: {
            type: 'number',
            maximum: 4096,
            minimum: 1,
        },
        temperature: {
            type: 'number',
            maximum: 2,
        },
        examples: {
            type: 'any-of',
            contains: [{
                type: 'array',
                contains: {
                    type: 'array',
                    contains: {
                        type: 'string',
                    },
                },
            }, {
                type: 'null',
            }],
        },
    },
} as const;
