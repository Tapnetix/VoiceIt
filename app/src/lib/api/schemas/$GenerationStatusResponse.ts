/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $GenerationStatusResponse = {
    description: `Response for GET /books/{book_id}/generation-status. (Contract 03)`,
    properties: {
        chapters: {
            type: 'array',
            contains: {
                type: 'ChapterGenerationStatus',
            },
            isRequired: true,
        },
        overall_progress: {
            type: 'number',
            isRequired: true,
        },
    },
} as const;
