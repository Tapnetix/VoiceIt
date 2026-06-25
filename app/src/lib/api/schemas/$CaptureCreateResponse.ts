/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $CaptureCreateResponse = {
    description: `Response model for \`\`POST /captures\`\`.
    Adds \`\`auto_refine\`\` and \`\`allow_auto_paste\`\` — the server-side settings
    captured at the moment the capture was created. The client reads these to
    decide whether to chain a refinement request and whether to fire the
    synthetic-paste pipeline, so it doesn't need a synced local copy of the
    capture_settings table across sibling Tauri webviews.`,
    properties: {
        id: {
            type: 'string',
            isRequired: true,
        },
        audio_path: {
            type: 'string',
            isRequired: true,
        },
        source: {
            type: 'string',
            isRequired: true,
        },
        language: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        duration_ms: {
            type: 'any-of',
            contains: [{
                type: 'number',
            }, {
                type: 'null',
            }],
        },
        transcript_raw: {
            type: 'string',
            isRequired: true,
        },
        transcript_refined: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        stt_model: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        llm_model: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        refinement_flags: {
            type: 'any-of',
            contains: [{
                type: 'RefinementFlagsModel',
            }, {
                type: 'null',
            }],
        },
        created_at: {
            type: 'string',
            isRequired: true,
            format: 'date-time',
        },
        auto_refine: {
            type: 'boolean',
            isRequired: true,
        },
        allow_auto_paste: {
            type: 'boolean',
            isRequired: true,
        },
    },
} as const;
