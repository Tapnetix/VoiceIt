/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { CaptureSettingsResponse } from '../models/CaptureSettingsResponse';
import type { CaptureSettingsUpdate } from '../models/CaptureSettingsUpdate';
import type { GenerationSettingsResponse } from '../models/GenerationSettingsResponse';
import type { GenerationSettingsUpdate } from '../models/GenerationSettingsUpdate';
import type { CancelablePromise } from '../core/CancelablePromise';
import { OpenAPI } from '../core/OpenAPI';
import { request as __request } from '../core/request';
export class SettingsService {
    /**
     * Get Capture Settings Endpoint
     * @returns CaptureSettingsResponse Successful Response
     * @throws ApiError
     */
    public static getCaptureSettingsEndpointSettingsCapturesGet(): CancelablePromise<CaptureSettingsResponse> {
        return __request(OpenAPI, {
            method: 'GET',
            url: '/settings/captures',
        });
    }
    /**
     * Update Capture Settings Endpoint
     * @returns CaptureSettingsResponse Successful Response
     * @throws ApiError
     */
    public static updateCaptureSettingsEndpointSettingsCapturesPut({
        requestBody,
    }: {
        requestBody: CaptureSettingsUpdate,
    }): CancelablePromise<CaptureSettingsResponse> {
        return __request(OpenAPI, {
            method: 'PUT',
            url: '/settings/captures',
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Get Generation Settings Endpoint
     * @returns GenerationSettingsResponse Successful Response
     * @throws ApiError
     */
    public static getGenerationSettingsEndpointSettingsGenerationGet(): CancelablePromise<GenerationSettingsResponse> {
        return __request(OpenAPI, {
            method: 'GET',
            url: '/settings/generation',
        });
    }
    /**
     * Update Generation Settings Endpoint
     * @returns GenerationSettingsResponse Successful Response
     * @throws ApiError
     */
    public static updateGenerationSettingsEndpointSettingsGenerationPut({
        requestBody,
    }: {
        requestBody: GenerationSettingsUpdate,
    }): CancelablePromise<GenerationSettingsResponse> {
        return __request(OpenAPI, {
            method: 'PUT',
            url: '/settings/generation',
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
}
