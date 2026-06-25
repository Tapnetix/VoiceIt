/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Book metadata response (list view). (Contract 01)
 */
export type BookResponse = {
    id: string;
    title: string;
    author?: (string | null);
    source_format: string;
    cover_path?: (string | null);
    status: string;
    chapter_count: number;
    created_at: string;
    updated_at: string;
};

