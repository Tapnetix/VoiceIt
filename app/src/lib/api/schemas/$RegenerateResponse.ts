/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $RegenerateResponse = {
    description: `Response for POST /segments/{segment_id}/regenerate. (Contract 03)`,
    properties: {
        segment_id: {
            type: 'string',
            isRequired: true,
        },
        generation_id: {
            type: 'string',
            isRequired: true,
        },
        version_id: {
            type: 'string',
            isRequired: true,
        },
        status: {
            type: 'string',
            isRequired: true,
        },
    },
} as const;
