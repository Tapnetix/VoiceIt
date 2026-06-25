/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * PATCH body for /books/{book_id}/characters/{char_id}. (Contract 02)
 */
export type CharacterUpdate = {
    name?: (string | null);
    color?: (string | null);
    profile_id?: (string | null);
    design_prompt?: (string | null);
    preset_voice_id?: (string | null);
    is_narrator?: (boolean | null);
};

