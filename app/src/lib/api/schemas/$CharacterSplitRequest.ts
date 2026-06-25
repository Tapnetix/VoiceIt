/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $CharacterSplitRequest = {
    description: `Body for POST /books/{book_id}/characters/{char_id}/split. (Contract 02)`,
    properties: {
        new_name: {
            type: 'string',
            isRequired: true,
        },
        segment_ids: {
            type: 'array',
            contains: {
                type: 'string',
            },
            isRequired: true,
        },
    },
} as const;
