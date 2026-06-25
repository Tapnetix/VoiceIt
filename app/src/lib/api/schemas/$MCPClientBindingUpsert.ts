/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $MCPClientBindingUpsert = {
    description: `Create or update a binding. Matched by \`\`client_id\`\`.`,
    properties: {
        client_id: {
            type: 'string',
            isRequired: true,
            maxLength: 64,
            minLength: 1,
        },
        label: {
            type: 'any-of',
            contains: [{
                type: 'string',
                maxLength: 128,
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
    },
} as const;
