/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { SegmentResponse } from '../models/SegmentResponse';
import type { SegmentUpdate } from '../models/SegmentUpdate';
import type { CancelablePromise } from '../core/CancelablePromise';
import { OpenAPI } from '../core/OpenAPI';
import { request as __request } from '../core/request';
export class BookSegmentsService {
    /**
     * List Segments
     * Return all segments for a chapter, ordered by `order` ascending.
     *
     * Returns 404 if the chapter does not exist or does not belong to book_id.
     * @returns SegmentResponse Successful Response
     * @throws ApiError
     */
    public static listSegmentsBooksBookIdChaptersChapterIdSegmentsGet({
        bookId,
        chapterId,
    }: {
        bookId: string,
        chapterId: string,
    }): CancelablePromise<Array<SegmentResponse>> {
        return __request(OpenAPI, {
            method: 'GET',
            url: '/books/{book_id}/chapters/{chapter_id}/segments',
            path: {
                'book_id': bookId,
                'chapter_id': chapterId,
            },
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Patch Segment
     * Apply a partial update to a segment.
     *
     * - 404 if the segment does not exist.
     * - 409 if the owning book is currently generating.
     * - When any content field changes and the segment already has a generation_id,
     * audio_status is set to "stale".
     * @returns SegmentResponse Successful Response
     * @throws ApiError
     */
    public static patchSegmentSegmentsSegmentIdPatch({
        segmentId,
        requestBody,
    }: {
        segmentId: string,
        requestBody: SegmentUpdate,
    }): CancelablePromise<SegmentResponse> {
        return __request(OpenAPI, {
            method: 'PATCH',
            url: '/segments/{segment_id}',
            path: {
                'segment_id': segmentId,
            },
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
}
