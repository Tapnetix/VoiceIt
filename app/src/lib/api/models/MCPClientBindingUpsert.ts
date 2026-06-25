/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
/**
 * Create or update a binding. Matched by ``client_id``.
 */
export type MCPClientBindingUpsert = {
    client_id: string;
    label?: (string | null);
    profile_id?: (string | null);
    default_engine?: (string | null);
    default_personality?: boolean;
};

