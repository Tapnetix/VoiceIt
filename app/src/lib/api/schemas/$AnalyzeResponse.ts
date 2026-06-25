/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $AnalyzeResponse = {
    description: `Response for POST /books/{book_id}/analyze (202). (Contract 01)`,
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
