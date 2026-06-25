/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $CaptureListResponse = {
    description: `Response model for paginated capture list.`,
    properties: {
        items: {
            type: 'array',
            contains: {
                type: 'CaptureResponse',
            },
            isRequired: true,
        },
        total: {
            type: 'number',
            isRequired: true,
        },
    },
} as const;
