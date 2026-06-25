/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $SegmentResponse = {
    description: `Ordered segment with inline audio state. (Contract 02)`,
    properties: {
        id: {
            type: 'string',
            isRequired: true,
        },
        chapter_id: {
            type: 'string',
            isRequired: true,
        },
        character_id: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        character_name: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        type: {
            type: 'Enum',
            isRequired: true,
        },
        text: {
            type: 'string',
            isRequired: true,
        },
        emotion: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        emotion_intensity: {
            type: 'any-of',
            contains: [{
                type: 'number',
            }, {
                type: 'null',
            }],
        },
        delivery: {
            type: 'any-of',
            contains: [{
                type: 'string',
            }, {
                type: 'null',
            }],
        },
        order: {
            type: 'number',
            isRequired: true,
        },
        audio: {
            type: 'SegmentAudio',
            isRequired: true,
        },
    },
} as const;
