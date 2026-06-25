/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $ChapterGenerationStatus = {
    description: `Per-chapter generation counts within GenerationStatusResponse. (Contract 03)`,
    properties: {
        chapter_id: {
            type: 'string',
            isRequired: true,
        },
        total: {
            type: 'number',
            isRequired: true,
        },
        completed: {
            type: 'number',
            isRequired: true,
        },
        errors: {
            type: 'number',
            isRequired: true,
        },
        state: {
            type: 'string',
            isRequired: true,
        },
    },
} as const;
