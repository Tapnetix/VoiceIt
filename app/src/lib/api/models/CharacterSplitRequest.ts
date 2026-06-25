/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Body for POST /books/{book_id}/characters/{char_id}/split. (Contract 02)
 */
export type CharacterSplitRequest = {
    new_name: string;
    segment_ids: Array<string>;
};

