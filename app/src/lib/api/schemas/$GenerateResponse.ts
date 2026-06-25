/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $GenerateResponse = {
    description: `202 response for generate / chapter-generate. (Contract 03)`,
    properties: {
        book_id: {
            type: 'string',
            isRequired: true,
        },
        chapter_id: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        task_id: {
            type: 'string',
            isRequired: true,
        },
        queued_segments: {
            type: 'number',
            isRequired: true,
        },
    },
} as const;
