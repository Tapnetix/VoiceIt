/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { SegmentResponse } from '../models/SegmentResponse';
import type { SegmentsMergeRequest } from '../models/SegmentsMergeRequest';
import type { SegmentSplitRequest } from '../models/SegmentSplitRequest';
import type { CancelablePromise } from '../core/CancelablePromise';
import { OpenAPI } from '../core/OpenAPI';
import { request as __request } from '../core/request';
export class BookSegmentStructureService {
    /**
     * Split Segment Endpoint
     * Split a segment into two at a character offset.
     *
     * - 400 if at_offset <= 0 or >= len(text).
     * - 404 if the segment does not exist.
     * - 409 if the owning book is currently generating.
     * - Returns [first_segment, second_segment].
     * @returns SegmentResponse Successful Response
     * @throws ApiError
     */
    public static splitSegmentEndpointSegmentsSegmentIdSplitPost({
        segmentId,
        requestBody,
    }: {
        segmentId: string,
        requestBody: SegmentSplitRequest,
    }): CancelablePromise<Array<SegmentResponse>> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/segments/{segment_id}/split',
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
    /**
     * Merge Segments Endpoint
     * Merge adjacent segments into one.
     *
     * - 400 if fewer than 2 segment_ids, non-adjacent, or mixed chapters.
     * - 404 if any segment_id is unknown.
     * - 409 if the owning book is currently generating.
     * - Returns the merged segment.
     * @returns SegmentResponse Successful Response
     * @throws ApiError
     */
    public static mergeSegmentsEndpointSegmentsMergePost({
        requestBody,
    }: {
        requestBody: SegmentsMergeRequest,
    }): CancelablePromise<SegmentResponse> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/segments/merge',
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
}
