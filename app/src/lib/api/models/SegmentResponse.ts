/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { SegmentAudio } from './SegmentAudio';
/**
 * Ordered segment with inline audio state. (Contract 02)
 */
export type SegmentResponse = {
    id: string;
    chapter_id: string;
    character_id?: (string | null);
    character_name?: (string | null);
    type: SegmentResponse.type;
    text: string;
    emotion?: (string | null);
    emotion_intensity?: (number | null);
    delivery?: (string | null);
    order: number;
    audio: SegmentAudio;
};
export namespace SegmentResponse {
    export enum type {
        NARRATION = 'narration',
        DIALOGUE = 'dialogue',
    }
}

