/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $PreviewResponse = {
    description: `Response for POST /characters/{char_id}/preview. (Contract 02)`,
    properties: {
        generation_id: {
            type: 'string',
            isRequired: true,
        },
        audio_path: {
            type: 'string',
            isRequired: true,
        },
    },
} as const;
