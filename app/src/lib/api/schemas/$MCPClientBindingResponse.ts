/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $MCPClientBindingResponse = {
    description: `Per-MCP-client voice binding — what voice / engine the server should
    use when a given client_id calls voiceit.speak without args, plus an
    opt-in personality-rewrite default.`,
    properties: {
        client_id: {
            type: 'string',
            isRequired: true,
        },
        label: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        profile_id: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        default_engine: {
            type: 'any-of',
            contains: [{
                type: 'string',
                pattern: '^(qwen|qwen_custom_voice|luxtts|chatterbox|chatterbox_turbo|tada|kokoro)$',
            }, {
                type: 'null',
            }],
        },
        default_personality: {
            type: 'boolean',
        },
        last_seen_at: {
            type: 'any-of',
            contains: [{
                type: 'string',
                format: 'date-time',
            }, {
                type: 'null',
            }],
        },
        created_at: {
            type: 'string',
            isRequired: true,
            format: 'date-time',
        },
        updated_at: {
            type: 'string',
            isRequired: true,
            format: 'date-time',
        },
    },
} as const;
