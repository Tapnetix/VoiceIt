/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * PATCH body for /segments/{segment_id}. (Contract 02)
 */
export type SegmentUpdate = {
    character_id?: (string | null);
    emotion?: (string | null);
    emotion_intensity?: (number | null);
    delivery?: (string | null);
    text?: (string | null);
    type?: ('dialogue' | 'narration' | null);
};

