/**
 * Behaviour-based tests for `useBooks` hooks.
 *
 * Test boundaries:
 *  - HTTP boundary (`globalThis.fetch`) is stubbed — not a first-party module.
 *  - Platform boundary (`@/platform/PlatformContext`) is provided via the real
 *    `PlatformProvider` with a stub `saveFile` for download-export tests.
 *  - The real `apiClient`, the real React Query stack, and the real hooks under
 *    test all run unmocked so observable outcomes (returned `data`, query cache
 *    state) reflect the production behaviour.
 *
 * Assertion style: WHAT the hook produces, not HOW it talks to its
 * collaborators. No call-count assertions on `apiClient` or on
 * `queryClient.invalidateQueries`. Cache invalidation is observed via
 * `queryClient.getQueryState(key)?.isInvalidated` after the mutation settles.
 */

import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import {
  useBooks,
  useBook,
  useBookCharacters,
  useCharacters,
  useBookSegments,
  useSegments,
  useBookVoiceOptions,
  useVoiceOptions,
  useBookGenerationStatus,
  useImportBook,
  useUpdateBook,
  useDeleteBook,
  useAnalyzeBook,
  useUpdateCharacter,
  useMergeCharacter,
  useSplitCharacter,
  useDeleteCharacter,
  usePreviewCharacter,
  useUpdateSegment,
  useSplitSegment,
  useMergeSegments,
  useRegenerateSegment,
  usePreviewSegment,
  useGenerateChapter,
  useGenerateBook,
  useSaveVoiceToLibrary,
  useCloneVoiceForCharacter,
  useStartExport,
  useDownloadExport,
} from '@/lib/hooks/useBooks';
import { PlatformProvider } from '@/platform/PlatformContext';
import type { Platform } from '@/platform/types';

// ─── HTTP boundary stub ───────────────────────────────────────────────────────

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

let fetchCalls: FetchCall[] = [];

/**
 * Stub `fetch` with a route table mapping `"METHOD path-suffix"` to a response
 * body (or response builder). Path suffix matching keeps tests agnostic about
 * the configured base URL.
 */
function installFetch(
  routes: Record<
    string,
    | unknown
    | ((req: { url: string; method: string; body: unknown }) => unknown | Response)
  >,
) {
  fetchCalls = [];
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method ?? 'GET').toUpperCase();
    let body: unknown = undefined;
    if (init?.body && typeof init.body === 'string') {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    } else if (init?.body instanceof FormData) {
      const fd = init.body;
      const obj: Record<string, unknown> = {};
      fd.forEach((v, k) => {
        obj[k] = v;
      });
      body = obj;
    }
    fetchCalls.push({ url, method, body });

    // Find first route whose suffix matches the URL path
    for (const [key, value] of Object.entries(routes)) {
      const [routeMethod, ...rest] = key.split(' ');
      const routePath = rest.join(' ');
      if (routeMethod !== method) continue;
      if (!url.endsWith(routePath)) continue;
      const resolved =
        typeof value === 'function' ? value({ url, method, body }) : value;
      if (resolved instanceof Response) return resolved;
      if (resolved instanceof Blob) {
        return new Response(resolved, { status: 200 });
      }
      return new Response(JSON.stringify(resolved ?? {}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({ detail: `No route for ${method} ${url}` }), {
      status: 404,
    });
  });
  vi.stubGlobal('fetch', handler);
  return handler;
}

// ─── Wrapper helpers ──────────────────────────────────────────────────────────

function makeWrapper(opts?: { platform?: Platform }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const platform = opts?.platform;
  const Wrapper = ({ children }: { children: React.ReactNode }) => {
    const tree = React.createElement(QueryClientProvider, { client: queryClient }, children);
    return platform
      ? React.createElement(PlatformProvider, { platform }, tree)
      : tree;
  };
  return { wrapper: Wrapper, queryClient };
}

function seedFreshQuery(queryClient: QueryClient, key: readonly unknown[]) {
  // Mark a query as observed-but-fresh so we can check that a mutation
  // invalidated it (fresh -> stale/invalidated is the observable cache change
  // that React Query subscribers — components — actually react to).
  queryClient.setQueryData(key, { __seeded: true });
}

function isInvalidated(queryClient: QueryClient, key: readonly unknown[]): boolean {
  return queryClient.getQueryState(key)?.isInvalidated === true;
}

// ─── Lifecycle ────────────────────────────────────────────────────────────────

beforeEach(() => {
  fetchCalls = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ─── Query hooks: return data from the server ─────────────────────────────────

describe('useBooks', () => {
  it('returns the list of books fetched from the server', async () => {
    installFetch({
      'GET /books': [
        {
          id: 'b1',
          title: 'Silo',
          author: 'Hugh Howey',
          source_format: 'epub',
          status: 'analyzed',
          chapter_count: 3,
          created_at: '',
          updated_at: '',
        },
      ],
    });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useBooks(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([
      expect.objectContaining({ id: 'b1', title: 'Silo' }),
    ]);
  });
});

describe('useBook', () => {
  it('returns the book detail for the given bookId', async () => {
    installFetch({
      'GET /books/b1': {
        id: 'b1',
        title: 'Silo',
        source_format: 'epub',
        status: 'analyzed',
        chapter_count: 3,
        created_at: '',
        updated_at: '',
        chapters: [],
      },
    });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useBook('b1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(
      expect.objectContaining({ id: 'b1', title: 'Silo' }),
    );
  });

  it('stays idle (no request issued) when bookId is null', async () => {
    installFetch({});
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useBook(null), { wrapper });

    // Observable outcome: hook reports the disabled-query state.
    expect(result.current.fetchStatus).toBe('idle');
    expect(result.current.data).toBeUndefined();
    // And the HTTP boundary saw no traffic for this hook.
    expect(fetchCalls).toEqual([]);
  });
});

describe('useBookCharacters / useCharacters alias', () => {
  it('returns characters for the given bookId', async () => {
    installFetch({
      'GET /books/b1/characters': [
        {
          id: 'c1',
          name: 'Mira',
          color: '#fff',
          voice_type: null,
          voice_label: null,
          is_library: false,
          is_narrator: false,
          dialogue_count: 5,
          confidence: 0.9,
          aliases: [],
        },
      ],
    });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useBookCharacters('b1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.[0]?.name).toBe('Mira');
  });

  it('useCharacters alias resolves to the same data', async () => {
    installFetch({
      'GET /books/b1/characters': [
        {
          id: 'c1',
          name: 'Mira',
          color: '#fff',
          voice_type: null,
          voice_label: null,
          is_library: false,
          is_narrator: false,
          dialogue_count: 5,
          confidence: 0.9,
          aliases: [],
        },
      ],
    });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCharacters('b1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.[0]?.id).toBe('c1');
  });

  it('stays idle when bookId is null', async () => {
    installFetch({});
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useBookCharacters(null), { wrapper });
    expect(result.current.fetchStatus).toBe('idle');
    expect(fetchCalls).toEqual([]);
  });
});

describe('useBookSegments / useSegments alias', () => {
  it('returns segments for the given chapter', async () => {
    installFetch({
      'GET /books/b1/chapters/ch1/segments': [
        {
          id: 's1',
          chapter_id: 'ch1',
          character_id: 'c1',
          character_name: 'Mira',
          type: 'dialogue',
          text: 'Hello',
          emotion: 'neutral',
          emotion_intensity: 0.5,
          order: 1,
          audio: { generation_id: 'g1', status: 'completed' },
        },
      ],
    });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSegments('b1', 'ch1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toHaveLength(1);
    expect(result.current.data?.[0]?.text).toBe('Hello');
  });

  it('stays idle when either bookId or chapterId is null', async () => {
    installFetch({});
    const { wrapper } = makeWrapper();
    const { result: nullBook } = renderHook(() => useBookSegments(null, 'ch1'), {
      wrapper,
    });
    const { result: nullChapter } = renderHook(() => useBookSegments('b1', null), {
      wrapper,
    });
    expect(nullBook.current.fetchStatus).toBe('idle');
    expect(nullChapter.current.fetchStatus).toBe('idle');
    expect(fetchCalls).toEqual([]);
  });
});

describe('useBookVoiceOptions / useVoiceOptions alias', () => {
  it('returns voice options for the book', async () => {
    installFetch({
      'GET /books/b1/voice-options': {
        library: [{ id: 'lib1' }],
        book: [],
        presets: [],
      },
    });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useVoiceOptions('b1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.library).toHaveLength(1);
  });

  it('stays idle when bookId is null', async () => {
    installFetch({});
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useBookVoiceOptions(null), { wrapper });
    expect(result.current.fetchStatus).toBe('idle');
    expect(fetchCalls).toEqual([]);
  });
});

describe('useBookGenerationStatus', () => {
  it('returns generation status for the book', async () => {
    installFetch({
      'GET /books/b1/generation-status': {
        chapters: [{ chapter_id: 'ch1', completed: 2, total: 5 }],
        overall_progress: 0.4,
      },
    });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useBookGenerationStatus('b1'), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.overall_progress).toBe(0.4);
  });

  it('stays idle when bookId is null', async () => {
    installFetch({});
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useBookGenerationStatus(null), { wrapper });
    expect(result.current.fetchStatus).toBe('idle');
    expect(fetchCalls).toEqual([]);
  });
});

// ─── Book mutations: observable cache invalidation ────────────────────────────

describe('useImportBook', () => {
  it('returns the imported book and invalidates the books list cache', async () => {
    const created = {
      id: 'b2',
      title: 'Wool',
      source_format: 'epub',
      status: 'imported',
      chapter_count: 0,
      created_at: '',
      updated_at: '',
      chapters: [],
    };
    installFetch({ 'POST /books/import': created });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books']);

    const { result } = renderHook(() => useImportBook(), { wrapper });
    result.current.mutate({ file: new File(['x'], 'wool.epub') });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(expect.objectContaining({ id: 'b2' }));
    // Observable cache effect: a subscribed `useBooks()` would now refetch.
    expect(isInvalidated(queryClient, ['books'])).toBe(true);
  });
});

describe('useUpdateBook', () => {
  it('returns the updated book and invalidates list + detail caches', async () => {
    const updated = {
      id: 'b1',
      title: 'Silo Updated',
      source_format: 'epub',
      status: 'analyzed',
      chapter_count: 3,
      created_at: '',
      updated_at: '',
    };
    installFetch({ 'PATCH /books/b1': updated });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books']);
    seedFreshQuery(queryClient, ['books', 'b1']);

    const { result } = renderHook(() => useUpdateBook(), { wrapper });
    result.current.mutate({ bookId: 'b1', data: { title: 'Silo Updated' } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.title).toBe('Silo Updated');
    expect(isInvalidated(queryClient, ['books'])).toBe(true);
    expect(isInvalidated(queryClient, ['books', 'b1'])).toBe(true);
  });
});

describe('useDeleteBook', () => {
  it('invalidates the books list cache after deletion succeeds', async () => {
    installFetch({ 'DELETE /books/b1': {} });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books']);

    const { result } = renderHook(() => useDeleteBook(), { wrapper });
    result.current.mutate('b1');

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(isInvalidated(queryClient, ['books'])).toBe(true);
  });
});

describe('useAnalyzeBook', () => {
  it('returns analysis kickoff response and invalidates detail + characters caches', async () => {
    installFetch({
      'POST /books/b1/analyze': {
        book_id: 'b1',
        task_id: 't1',
        status: 'analyzing',
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1']);
    seedFreshQuery(queryClient, ['books', 'b1', 'characters']);
    seedFreshQuery(queryClient, ['books']); // list should NOT be invalidated

    const { result } = renderHook(() => useAnalyzeBook(), { wrapper });
    result.current.mutate({ bookId: 'b1' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.task_id).toBe('t1');
    expect(isInvalidated(queryClient, ['books', 'b1'])).toBe(true);
    expect(isInvalidated(queryClient, ['books', 'b1', 'characters'])).toBe(true);
    // `useAnalyzeBook` narrowly invalidates — the list must NOT be touched.
    expect(isInvalidated(queryClient, ['books'])).toBe(false);
  });
});

// ─── Character mutations ──────────────────────────────────────────────────────

describe('useUpdateCharacter', () => {
  it('invalidates the characters cache for the affected book', async () => {
    const updated = {
      id: 'c1',
      name: 'Mira Updated',
      color: '#fff',
      voice_type: null,
      voice_label: null,
      is_library: false,
      is_narrator: false,
      dialogue_count: 5,
      confidence: 0.9,
      aliases: [],
    };
    installFetch({ 'PATCH /books/b1/characters/c1': updated });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'characters']);

    const { result } = renderHook(() => useUpdateCharacter(), { wrapper });
    result.current.mutate({
      bookId: 'b1',
      charId: 'c1',
      data: { name: 'Mira Updated' },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.name).toBe('Mira Updated');
    expect(isInvalidated(queryClient, ['books', 'b1', 'characters'])).toBe(true);
  });
});

describe('useMergeCharacter', () => {
  it('invalidates characters AND all chapter caches after merge', async () => {
    installFetch({
      'POST /books/b1/characters/c1/merge': {
        id: 'c1',
        name: 'Mira',
        color: '#fff',
        voice_type: null,
        voice_label: null,
        is_library: false,
        is_narrator: false,
        dialogue_count: 8,
        confidence: 0.9,
        aliases: [],
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'characters']);
    // Two distinct chapter caches — both must be invalidated by the broad
    // ['books', 'b1', 'chapters'] match (exact: false).
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']);
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch2', 'segments']);

    const { result } = renderHook(() => useMergeCharacter(), { wrapper });
    result.current.mutate({
      bookId: 'b1',
      charId: 'c1',
      data: { source_char_id: 'c2' },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(isInvalidated(queryClient, ['books', 'b1', 'characters'])).toBe(true);
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']),
    ).toBe(true);
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch2', 'segments']),
    ).toBe(true);
  });
});

describe('useSplitCharacter', () => {
  it('invalidates characters AND all chapter caches after split', async () => {
    installFetch({
      'POST /books/b1/characters/c1/split': {
        id: 'c3',
        name: 'New Mira',
        color: '#fff',
        voice_type: null,
        voice_label: null,
        is_library: false,
        is_narrator: false,
        dialogue_count: 2,
        confidence: 0.7,
        aliases: [],
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'characters']);
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']);

    const { result } = renderHook(() => useSplitCharacter(), { wrapper });
    result.current.mutate({
      bookId: 'b1',
      charId: 'c1',
      data: { new_name: 'New Mira', segment_ids: ['s1'] },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.name).toBe('New Mira');
    expect(isInvalidated(queryClient, ['books', 'b1', 'characters'])).toBe(true);
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']),
    ).toBe(true);
  });
});

describe('useDeleteCharacter', () => {
  it('invalidates characters AND chapter segments (deletion reassigns to narrator)', async () => {
    installFetch({ 'DELETE /books/b1/characters/c1': {} });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'characters']);
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']);

    const { result } = renderHook(() => useDeleteCharacter(), { wrapper });
    result.current.mutate({ bookId: 'b1', charId: 'c1' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(isInvalidated(queryClient, ['books', 'b1', 'characters'])).toBe(true);
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']),
    ).toBe(true);
  });
});

// ─── Character preview (non-cache-affecting) ─────────────────────────────────

describe('usePreviewCharacter', () => {
  it('returns the preview response from the server', async () => {
    installFetch({
      'POST /characters/c1/preview': {
        generation_id: 'g1',
        audio_path: '/audio/preview.wav',
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    // Preview is genuinely non-destructive — caches must not be invalidated.
    seedFreshQuery(queryClient, ['books', 'b1', 'characters']);
    seedFreshQuery(queryClient, ['books', 'b1', 'voice-options']);

    const { result } = renderHook(() => usePreviewCharacter(), { wrapper });
    result.current.mutate({ charId: 'c1', data: { text: 'Hi', emotion: 'happy' } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.audio_path).toBe('/audio/preview.wav');
    expect(isInvalidated(queryClient, ['books', 'b1', 'characters'])).toBe(false);
    expect(isInvalidated(queryClient, ['books', 'b1', 'voice-options'])).toBe(false);
  });

  it('forwards the emotion/text payload to the preview endpoint', async () => {
    installFetch({
      'POST /characters/c1/preview': {
        generation_id: 'g1',
        audio_path: '/audio/preview.wav',
      },
    });
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePreviewCharacter(), { wrapper });
    result.current.mutate({
      charId: 'c1',
      data: { text: 'Hello there', emotion: 'happy' },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The server contract — what the backend receives — IS the observable
    // outcome of this mutation. Asserted at the HTTP boundary, not via spy.
    const previewCall = fetchCalls.find((c) =>
      c.url.endsWith('/characters/c1/preview'),
    );
    expect(previewCall?.body).toEqual({ text: 'Hello there', emotion: 'happy' });
  });
});

// ─── Segment mutations ────────────────────────────────────────────────────────

describe('useUpdateSegment', () => {
  it('invalidates the chapter segments AND generation-status caches', async () => {
    installFetch({
      'PATCH /segments/s1': {
        id: 's1',
        chapter_id: 'ch1',
        character_id: 'c1',
        character_name: 'Mira',
        type: 'dialogue',
        text: 'Hello',
        emotion: 'angry',
        emotion_intensity: 0.5,
        order: 1,
        audio: { generation_id: 'g1', status: 'completed' },
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']);
    seedFreshQuery(queryClient, ['books', 'b1', 'generation-status']);
    // An unrelated chapter must NOT be invalidated (narrow invalidation).
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch2', 'segments']);

    const { result } = renderHook(() => useUpdateSegment(), { wrapper });
    result.current.mutate({
      segmentId: 's1',
      data: { emotion: 'angry' },
      bookId: 'b1',
      chapterId: 'ch1',
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']),
    ).toBe(true);
    expect(isInvalidated(queryClient, ['books', 'b1', 'generation-status'])).toBe(true);
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch2', 'segments']),
    ).toBe(false);
  });
});

describe('useSplitSegment', () => {
  it('invalidates characters AND the affected chapter segments', async () => {
    installFetch({ 'POST /segments/s1/split': [] });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'characters']);
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']);

    const { result } = renderHook(() => useSplitSegment(), { wrapper });
    result.current.mutate({
      segmentId: 's1',
      data: { at_offset: 10 },
      bookId: 'b1',
      chapterId: 'ch1',
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(isInvalidated(queryClient, ['books', 'b1', 'characters'])).toBe(true);
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']),
    ).toBe(true);
  });
});

describe('useMergeSegments', () => {
  it('invalidates characters AND the affected chapter segments', async () => {
    installFetch({
      'POST /segments/merge': {
        id: 's1',
        chapter_id: 'ch1',
        character_id: 'c1',
        character_name: 'Mira',
        type: 'dialogue',
        text: 'Merged',
        emotion: 'neutral',
        emotion_intensity: 0.5,
        order: 1,
        audio: { generation_id: 'g1', status: 'completed' },
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'characters']);
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']);

    const { result } = renderHook(() => useMergeSegments(), { wrapper });
    result.current.mutate({
      data: { segment_ids: ['s1', 's2'] },
      bookId: 'b1',
      chapterId: 'ch1',
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(isInvalidated(queryClient, ['books', 'b1', 'characters'])).toBe(true);
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']),
    ).toBe(true);
  });
});

describe('useRegenerateSegment', () => {
  it('invalidates chapter segments AND generation-status', async () => {
    installFetch({
      'POST /segments/s1/regenerate': {
        segment_id: 's1',
        generation_id: 'g2',
        version_id: 'v1',
        status: 'completed',
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']);
    seedFreshQuery(queryClient, ['books', 'b1', 'generation-status']);

    const { result } = renderHook(() => useRegenerateSegment(), { wrapper });
    result.current.mutate({ segmentId: 's1', bookId: 'b1', chapterId: 'ch1' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']),
    ).toBe(true);
    expect(isInvalidated(queryClient, ['books', 'b1', 'generation-status'])).toBe(true);
  });
});

describe('usePreviewSegment', () => {
  it('returns the preview response and does NOT invalidate any cache', async () => {
    installFetch({
      'POST /segments/s1/preview': {
        segment_id: 's1',
        audio_path: '/audio/seg-preview.wav',
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    // Preview is documented as genuinely non-destructive — no cache writes.
    seedFreshQuery(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']);
    seedFreshQuery(queryClient, ['books', 'b1', 'generation-status']);

    const { result } = renderHook(() => usePreviewSegment(), { wrapper });
    result.current.mutate({
      segmentId: 's1',
      data: { emotion: 'sad' },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.audio_path).toBe('/audio/seg-preview.wav');
    expect(
      isInvalidated(queryClient, ['books', 'b1', 'chapters', 'ch1', 'segments']),
    ).toBe(false);
    expect(isInvalidated(queryClient, ['books', 'b1', 'generation-status'])).toBe(false);
  });
});

// ─── Generation mutations ─────────────────────────────────────────────────────

describe('useGenerateChapter', () => {
  it('invalidates generation-status after kicking off a chapter generation', async () => {
    installFetch({
      'POST /books/b1/chapters/ch1/generate': {
        book_id: 'b1',
        chapter_id: 'ch1',
        task_id: 't1',
        queued_segments: 5,
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'generation-status']);

    const { result } = renderHook(() => useGenerateChapter(), { wrapper });
    result.current.mutate({ bookId: 'b1', chapterId: 'ch1' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.queued_segments).toBe(5);
    expect(isInvalidated(queryClient, ['books', 'b1', 'generation-status'])).toBe(true);
  });
});

describe('useGenerateBook', () => {
  it('invalidates generation-status after kicking off a book generation', async () => {
    installFetch({
      'POST /books/b1/generate': {
        book_id: 'b1',
        task_id: 't1',
        queued_segments: 50,
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'generation-status']);

    const { result } = renderHook(() => useGenerateBook(), { wrapper });
    result.current.mutate({ bookId: 'b1' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.queued_segments).toBe(50);
    expect(isInvalidated(queryClient, ['books', 'b1', 'generation-status'])).toBe(true);
  });
});

// ─── Save voice to library ────────────────────────────────────────────────────

describe('useSaveVoiceToLibrary', () => {
  it('invalidates book voice-options AND the global profiles list when bookId is set', async () => {
    installFetch({
      'POST /characters/c1/save-to-library': {
        id: 'p1',
        name: 'Mira',
        language: 'en',
        voice_type: 'cloned',
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'voice-options']);
    seedFreshQuery(queryClient, ['profiles']);

    const { result } = renderHook(() => useSaveVoiceToLibrary('b1'), { wrapper });
    result.current.mutate('c1');

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(isInvalidated(queryClient, ['books', 'b1', 'voice-options'])).toBe(true);
    expect(isInvalidated(queryClient, ['profiles'])).toBe(true);
  });

  it('only invalidates the global profiles list when bookId is null', async () => {
    installFetch({
      'POST /characters/c1/save-to-library': {
        id: 'p1',
        name: 'Mira',
        language: 'en',
        voice_type: 'cloned',
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    // A stray book voice-options cache must remain fresh when bookId is null.
    seedFreshQuery(queryClient, ['books', 'b1', 'voice-options']);
    seedFreshQuery(queryClient, ['profiles']);

    const { result } = renderHook(() => useSaveVoiceToLibrary(null), { wrapper });
    result.current.mutate('c1');

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(isInvalidated(queryClient, ['profiles'])).toBe(true);
    expect(isInvalidated(queryClient, ['books', 'b1', 'voice-options'])).toBe(false);
  });
});

// ─── Clone voice for character ────────────────────────────────────────────────

describe('useCloneVoiceForCharacter', () => {
  it('creates the profile then uploads the sample with the user-supplied reference text', async () => {
    installFetch({
      'POST /profiles': {
        id: 'p1',
        name: 'Mira',
        language: 'en',
        voice_type: 'cloned',
      },
      'POST /profiles/p1/samples': {
        id: 'samp1',
        profile_id: 'p1',
        file_path: '/x',
        reference_text: 'will be checked from the call body',
      },
    });

    const { wrapper, queryClient } = makeWrapper();
    seedFreshQuery(queryClient, ['books', 'b1', 'voice-options']);

    const { result } = renderHook(() => useCloneVoiceForCharacter(), { wrapper });
    result.current.mutate({
      bookId: 'b1',
      charId: 'c1',
      name: 'Mira',
      file: new File(['x'], 'sample.wav'),
      referenceText: 'My real transcript',
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // 1. The returned data is the created profile.
    expect(result.current.data?.id).toBe('p1');
    // 2. The voice-options cache (which the UI watches) is invalidated.
    expect(isInvalidated(queryClient, ['books', 'b1', 'voice-options'])).toBe(true);
    // 3. The user's transcript reached the sample endpoint verbatim.
    const sampleCall = fetchCalls.find((c) =>
      c.url.endsWith('/profiles/p1/samples'),
    );
    expect((sampleCall?.body as Record<string, unknown>)?.reference_text).toBe(
      'My real transcript',
    );
  });

  it('falls back to the placeholder reference text when the user did not provide one', async () => {
    installFetch({
      'POST /profiles': {
        id: 'p1',
        name: 'Mira',
        language: 'en',
        voice_type: 'cloned',
      },
      'POST /profiles/p1/samples': {
        id: 'samp1',
        profile_id: 'p1',
        file_path: '/x',
        reference_text: 'placeholder check',
      },
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCloneVoiceForCharacter(), { wrapper });
    result.current.mutate({
      bookId: 'b1',
      charId: 'c1',
      name: 'Mira',
      file: new File(['x'], 'sample.wav'),
      // referenceText omitted
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sampleCall = fetchCalls.find((c) =>
      c.url.endsWith('/profiles/p1/samples'),
    );
    // The samples endpoint rejects empty reference_text (HTTP 422), so the
    // hook MUST substitute a non-empty placeholder. Observable: the call body.
    const refText = (sampleCall?.body as Record<string, unknown>)
      ?.reference_text as string;
    expect(refText).toBeTruthy();
    expect(refText.length).toBeGreaterThan(0);
  });

  it('falls back to the placeholder when the user provided only whitespace', async () => {
    installFetch({
      'POST /profiles': {
        id: 'p1',
        name: 'Mira',
        language: 'en',
        voice_type: 'cloned',
      },
      'POST /profiles/p1/samples': { id: 'samp1' },
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCloneVoiceForCharacter(), { wrapper });
    result.current.mutate({
      bookId: 'b1',
      charId: 'c1',
      name: 'Mira',
      file: new File(['x'], 'sample.wav'),
      referenceText: '   \n  ',
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sampleCall = fetchCalls.find((c) =>
      c.url.endsWith('/profiles/p1/samples'),
    );
    const refText = (sampleCall?.body as Record<string, unknown>)
      ?.reference_text as string;
    // Whitespace-only is treated as "blank" by the production code.
    expect(refText.trim().length).toBeGreaterThan(0);
  });
});

// ─── Export mutations ─────────────────────────────────────────────────────────

describe('useStartExport', () => {
  it('returns the export task descriptor from the server', async () => {
    installFetch({
      'POST /books/b1/export': {
        book_id: 'b1',
        task_id: 't1',
        status: 'exporting',
      },
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useStartExport(), { wrapper });
    result.current.mutate({ bookId: 'b1', data: { format: 'm4b' } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.task_id).toBe('t1');
    expect(result.current.data?.status).toBe('exporting');
  });
});

describe('useDownloadExport', () => {
  function makePlatform(): { platform: Platform; saves: Array<{ filename: string; blob: Blob }> } {
    const saves: Array<{ filename: string; blob: Blob }> = [];
    const platform: Platform = {
      filesystem: {
        async saveFile(filename: string, blob: Blob) {
          saves.push({ filename, blob });
        },
        async openPath() {},
        async pickDirectory() {
          return null;
        },
      },
      updater: {} as never,
      audio: {} as never,
      lifecycle: {} as never,
      metadata: {} as never,
    };
    return { platform, saves };
  }

  it('saves an .m4b file when format is m4b', async () => {
    const blob = new Blob(['audio'], { type: 'audio/mp4' });
    installFetch({ 'GET /books/b1/export/download': blob });
    const { platform, saves } = makePlatform();
    const { wrapper } = makeWrapper({ platform });

    const { result } = renderHook(() => useDownloadExport(), { wrapper });
    result.current.mutate({ bookId: 'b1', bookTitle: 'My Book', format: 'm4b' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(saves).toHaveLength(1);
    expect(saves[0]!.filename).toMatch(/\.m4b$/);
    // The downloaded payload is forwarded to the platform as a non-empty
    // Blob-like object (duck-typed: jsdom's Response.blob() returns a Blob
    // from a different realm, so `instanceof Blob` does not hold here).
    expect(typeof saves[0]!.blob.size).toBe('number');
    expect(saves[0]!.blob.size).toBeGreaterThan(0);
    expect(typeof saves[0]!.blob.arrayBuffer).toBe('function');
  });

  it('saves an .mp3 file when format is mp3_single', async () => {
    const blob = new Blob(['audio'], { type: 'audio/mpeg' });
    installFetch({ 'GET /books/b1/export/download': blob });
    const { platform, saves } = makePlatform();
    const { wrapper } = makeWrapper({ platform });

    const { result } = renderHook(() => useDownloadExport(), { wrapper });
    result.current.mutate({ bookId: 'b1', bookTitle: 'Silo', format: 'mp3_single' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(saves[0]!.filename).toMatch(/\.mp3$/);
  });

  it('saves a .zip file when format is mp3_per_chapter', async () => {
    const blob = new Blob(['zip'], { type: 'application/zip' });
    installFetch({ 'GET /books/b1/export/download': blob });
    const { platform, saves } = makePlatform();
    const { wrapper } = makeWrapper({ platform });

    const { result } = renderHook(() => useDownloadExport(), { wrapper });
    result.current.mutate({
      bookId: 'b1',
      bookTitle: 'Wool',
      format: 'mp3_per_chapter',
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(saves[0]!.filename).toMatch(/\.zip$/);
  });

  it('strips special characters from the book title and lowercases the filename', async () => {
    const blob = new Blob(['audio']);
    installFetch({ 'GET /books/b1/export/download': blob });
    const { platform, saves } = makePlatform();
    const { wrapper } = makeWrapper({ platform });

    const { result } = renderHook(() => useDownloadExport(), { wrapper });
    result.current.mutate({
      bookId: 'b1',
      bookTitle: 'My Book: Special!',
      format: 'm4b',
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const filename = saves[0]!.filename;
    // Only lowercase letters, digits, dashes, and the extension dot survive.
    expect(filename).toMatch(/^[a-z0-9-]+\.m4b$/);
    expect(filename).not.toMatch(/[A-Z!:]/);
  });

  it('falls back to a stable default name when the title is empty', async () => {
    const blob = new Blob(['audio']);
    installFetch({ 'GET /books/b1/export/download': blob });
    const { platform, saves } = makePlatform();
    const { wrapper } = makeWrapper({ platform });

    const { result } = renderHook(() => useDownloadExport(), { wrapper });
    result.current.mutate({ bookId: 'b1', bookTitle: '', format: 'm4b' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // When the sanitized name is empty, the hook substitutes a stable default
    // ('book') so the OS save dialog still has a non-empty suggested filename.
    expect(saves[0]!.filename).toBe('book.m4b');
  });

  it('truncates a very long title to the first 50 characters before sanitizing', async () => {
    const blob = new Blob(['audio']);
    installFetch({ 'GET /books/b1/export/download': blob });
    const { platform, saves } = makePlatform();
    const { wrapper } = makeWrapper({ platform });

    const longTitle = 'a'.repeat(120); // 120 alphabetic chars, all survive sanitize
    const { result } = renderHook(() => useDownloadExport(), { wrapper });
    result.current.mutate({ bookId: 'b1', bookTitle: longTitle, format: 'm4b' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const baseName = saves[0]!.filename.replace(/\.m4b$/, '');
    // Production rule: substring(0, 50) → max 50 characters in the base name.
    expect(baseName.length).toBeLessThanOrEqual(50);
  });
});
