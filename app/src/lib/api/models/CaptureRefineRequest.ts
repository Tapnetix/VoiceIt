/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { RefinementFlagsModel } from './RefinementFlagsModel';
/**
 * Request to refine a capture's transcript via the LLM.
 */
export type CaptureRefineRequest = {
    flags?: (RefinementFlagsModel | null);
    model_size?: (string | null);
};

