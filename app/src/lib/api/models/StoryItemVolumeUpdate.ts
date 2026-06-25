/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Request model for adjusting a story item's playback volume.
 *
 * Linear gain. ``1.0`` is the original level, ``0.0`` is silent. Capped
 * above 1.0 so a too-aggressive boost can't blow out the mix or clip
 * the export.
 */
export type StoryItemVolumeUpdate = {
    volume: number;
};

