/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $LLMGenerateResponse = {
    description: `Response model for LLM text generation.`,
    properties: {
        text: {
            type: 'string',
            isRequired: true,
        },
        model_size: {
            type: 'string',
            isRequired: true,
        },
    },
} as const;
