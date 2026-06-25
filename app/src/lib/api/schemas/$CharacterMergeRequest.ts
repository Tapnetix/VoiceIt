/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $CharacterMergeRequest = {
    description: `Body for POST /books/{book_id}/characters/{char_id}/merge. (Contract 02)`,
    properties: {
        source_char_id: {
            type: 'string',
            isRequired: true,
        },
    },
} as const;
