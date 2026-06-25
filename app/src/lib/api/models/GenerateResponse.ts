/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * 202 response for generate / chapter-generate. (Contract 03)
 */
export type GenerateResponse = {
    book_id: string;
    chapter_id?: (string | null);
    task_id: string;
    queued_segments: number;
};

