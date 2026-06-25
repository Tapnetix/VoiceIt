/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Per-MCP-client voice binding — what voice / engine the server should
 * use when a given client_id calls voiceit.speak without args, plus an
 * opt-in personality-rewrite default.
 */
export type MCPClientBindingResponse = {
    client_id: string;
    label?: (string | null);
    profile_id?: (string | null);
    default_engine?: (string | null);
    default_personality?: boolean;
    last_seen_at?: (string | null);
    created_at: string;
    updated_at: string;
};

