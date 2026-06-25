/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
export const $PersonalityTextResponse = {
    description: `Response returned by the \`\`/profiles/{id}/compose\`\` endpoint.`,
    properties: {
        text: {
            type: 'string',
            isRequired: true,
        },
        model_size: {
            type: 'string',
            isRequired: true,
        },
    },
} as const;
