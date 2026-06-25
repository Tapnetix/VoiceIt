/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { ChapterGenerationStatus } from './ChapterGenerationStatus';
/**
 * Response for GET /books/{book_id}/generation-status. (Contract 03)
 */
export type GenerationStatusResponse = {
    chapters: Array<ChapterGenerationStatus>;
    overall_progress: number;
};

