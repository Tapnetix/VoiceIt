/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Body for POST /books/{book_id}/export. (Contract 03)
 */
export type ExportRequest = {
    format: ExportRequest.format;
    bitrate?: ('64k' | '128k' | null);
    target_lufs?: (number | null);
    channels?: ('mono' | 'stereo' | null);
    title?: (string | null);
    author?: (string | null);
    cover_path?: (string | null);
};
export namespace ExportRequest {
    export enum format {
        M4B = 'm4b',
        MP3_SINGLE = 'mp3_single',
        MP3_PER_CHAPTER = 'mp3_per_chapter',
    }
}

