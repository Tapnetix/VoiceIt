/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $MCPClientBindingListResponse = {
    properties: {
        items: {
            type: 'array',
            contains: {
                type: 'MCPClientBindingResponse',
            },
            isRequired: true,
        },
    },
} as const;
