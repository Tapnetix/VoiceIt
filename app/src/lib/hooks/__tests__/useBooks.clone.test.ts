import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import React from 'react';

import { useCloneVoiceForCharacter } from '@/lib/hooks/useBooks';

// SC8 boundary: stub the HTTP boundary (globalThis.fetch), not the first-party
// apiClient module. Each test asserts on the observable outcome — the request
// body submitted to POST /profiles/{id}/samples — rather than internal call
// counts on a project-owned mock.

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return React.createElement(QueryClientProvider, { client }, children);
}

const file = new File(['x'], 'clip.wav', { type: 'audio/wav' });

const ok = (body: unknown) =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve(body),
  } as Response);

type SamplesRequest = { url: string; formData: FormData };

function spyOnFetch(): { samplesRequests: SamplesRequest[] } {
  const samplesRequests: SamplesRequest[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : (input as Request).url ?? String(input);
    if (url.endsWith('/profiles') && (init as RequestInit | undefined)?.method === 'POST') {
      return ok({ id: 'prof-1', name: 'Hero (cloned)' }) as unknown as Response;
    }
    if (/\/profiles\/[^/]+\/samples$/.test(url)) {
      samplesRequests.push({ url, formData: (init as RequestInit).body as FormData });
      return ok({ id: 'sample-1' }) as unknown as Response;
    }
    throw new Error(`unstubbed fetch: ${url}`);
  });
  return { samplesRequests };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('useCloneVoiceForCharacter reference_text submission (SC5)', () => {
  it('submits the user-provided transcript verbatim to the samples endpoint', async () => {
    const { samplesRequests } = spyOnFetch();
    const { result } = renderHook(() => useCloneVoiceForCharacter(), { wrapper });

    const profile = await result.current.mutateAsync({
      bookId: 'b1',
      charId: 'c1',
      name: 'Hero (cloned)',
      file,
      referenceText: 'the real spoken words',
    });

    expect(profile).toMatchObject({ id: 'prof-1' });
    expect(samplesRequests).toHaveLength(1);
    expect(samplesRequests[0].url).toMatch(/\/profiles\/prof-1\/samples$/);
    expect(samplesRequests[0].formData.get('reference_text')).toBe('the real spoken words');
    expect(samplesRequests[0].formData.get('file')).toBe(file);
  });

  it('substitutes the placeholder when the transcript is whitespace-only', async () => {
    const { samplesRequests } = spyOnFetch();
    const { result } = renderHook(() => useCloneVoiceForCharacter(), { wrapper });

    await result.current.mutateAsync({
      bookId: 'b1',
      charId: 'c1',
      name: 'Hero (cloned)',
      file,
      referenceText: '   ',
    });

    expect(samplesRequests).toHaveLength(1);
    expect(samplesRequests[0].formData.get('reference_text')).toBe(
      'Reference voice sample for cloning.',
    );
  });

  it('substitutes the placeholder when no transcript is provided', async () => {
    const { samplesRequests } = spyOnFetch();
    const { result } = renderHook(() => useCloneVoiceForCharacter(), { wrapper });

    await result.current.mutateAsync({
      bookId: 'b1',
      charId: 'c1',
      name: 'Hero (cloned)',
      file,
    });

    expect(samplesRequests).toHaveLength(1);
    expect(samplesRequests[0].formData.get('reference_text')).toBe(
      'Reference voice sample for cloning.',
    );
  });
});
