/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $ChapterSummary = {
    description: `Summary of a single chapter within a book. (Contract 01)`,
    properties: {
        id: {
            type: 'string',
            isRequired: true,
        },
        number: {
            type: 'number',
            isRequired: true,
        },
        title: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        word_count: {
            type: 'number',
            isRequired: true,
        },
        story_id: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        generation_state: {
            type: 'string',
            isRequired: true,
        },
    },
} as const;
