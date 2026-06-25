/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $SpeakRequest = {
    description: `Body for POST /speak — non-MCP REST surface that mirrors voiceit.speak.`,
    properties: {
        text: {
            type: 'string',
            isRequired: true,
            maxLength: 10000,
            minLength: 1,
        },
        profile: {
            type: 'any-of',
            description: `Voice profile name or id. Falls back to per-client binding, then default.`,
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        engine: {
            type: 'any-of',
            contains: [{
                type: 'string',
                pattern: '^(qwen|qwen_custom_voice|luxtts|chatterbox|chatterbox_turbo|tada|kokoro)$',
            }, {
                type: 'null',
            }],
        },
        personality: {
            type: 'any-of',
            description: `When true and the profile has a personality prompt, the input text is rewritten in-character before TTS. When null, the per-client binding's default_personality flag decides.`,
            contains: [{
                type: 'boolean',
            }, {
                type: 'null',
            }],
        },
        language: {
            type: 'any-of',
            contains: [{
                type: 'string',
                pattern: '^(zh|en|ja|ko|de|fr|ru|pt|es|it|he|ar|da|el|fi|hi|ms|nl|no|pl|sv|sw|tr)$',
            }, {
                type: 'null',
            }],
        },
    },
} as const;
