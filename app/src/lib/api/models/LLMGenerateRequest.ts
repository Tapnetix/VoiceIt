/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Request model for LLM text generation.
 */
export type LLMGenerateRequest = {
    prompt: string;
    system?: (string | null);
    model_size?: (string | null);
    max_tokens?: number;
    temperature?: number;
    examples?: (Array<Array<string>> | null);
};

