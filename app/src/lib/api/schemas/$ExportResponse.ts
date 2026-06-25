/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $ExportResponse = {
    description: `202 response for POST /books/{book_id}/export. (Contract 03)`,
    properties: {
        book_id: {
            type: 'string',
            isRequired: true,
        },
        task_id: {
            type: 'string',
            isRequired: true,
        },
        status: {
            type: 'string',
            isRequired: true,
        },
    },
} as const;
