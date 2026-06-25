/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Per-chapter generation counts within GenerationStatusResponse. (Contract 03)
 */
export type ChapterGenerationStatus = {
    chapter_id: string;
    total: number;
    completed: number;
    errors: number;
    state: string;
};

