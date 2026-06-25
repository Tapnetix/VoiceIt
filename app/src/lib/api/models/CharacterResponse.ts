/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Full character roster entry. (Contract 02)
 */
export type CharacterResponse = {
    id: string;
    name: string;
    color?: (string | null);
    profile_id?: (string | null);
    voice_type?: (string | null);
    voice_label?: (string | null);
    is_library?: boolean;
    is_narrator?: boolean;
    role?: (string | null);
    gender?: (string | null);
    age_range?: (string | null);
    vocal_description?: (string | null);
    archetype?: (string | null);
    dialogue_count?: number;
    confidence?: (number | null);
    aliases?: Array<string>;
};

