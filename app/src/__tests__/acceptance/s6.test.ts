/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BookOverview } from '@/components/BooksTab/BookOverview';
import { apiClient } from '@/lib/api/client';
import { useBooksStore } from '@/stores/booksStore';
import type {
  BookDetailResponse,
  BookProgressEvent,
  CharacterResponse,
  GenerateChapterRequest,
  GenerateChapterResponse,
} from '@/lib/api/types';

// ---------------------------------------------------------------------------
// Acceptance scenario S6 — "Generate audio for a chapter / book"
// ---------------------------------------------------------------------------
//
// User-observable outcome (from design.md §4 row S6):
//   The reader opens the book overview, clicks "Generate" on a chapter row,
//   and observes:
//     (a) the chapter's row badge advances from `none` → `generating n/m`
//         while audio is being synthesized,
//     (b) on completion, the badge flips to `done` and a play affordance
//         appears (the user can now listen to the chapter), and
//     (c) the request that crossed the HTTP boundary targeted the right
//         book + chapter — i.e. the audio is being generated for THIS
//         chapter, not some other one.
//
// These are the assertions for an S6 acceptance spec. Internal call counts
// are not asserted; we assert on the captured HTTP payload (what the user's
// click actually sent to the server) and on the DOM state the user sees.
//
// Boundary discipline:
//   • The real `BookOverview` renders against a real `QueryClient` and the
//     real `useBooksStore`.
//   • The HTTP edge — `apiClient.getBook`, `apiClient.getCharacters`,
//     `apiClient.generateChapter`, `apiClient.getBookEventsUrl` — is the
//     only first-party boundary we stub. No internal hook layer is mocked.
//   • SSE is the OS/browser `EventSource` boundary. jsdom does not implement
//     `EventSource`, so we install a tiny global stub that lets the test
//     fire well-typed `BookProgressEvent`s into the real `useBookProgress`
//     hook synchronously. The hook itself runs as production code — only
//     the browser API at its edge is doubled, equivalent in spirit to
//     stubbing `fetch`.

// ─── HTTP boundary capture ───────────────────────────────────────────────────

const generateCalls: Array<{
  bookId: string;
  chapterId: string;
  data?: GenerateChapterRequest;
}> = [];

interface ApiState {
  /** Pending `generateChapter` impl; when null the default fast-success runs. */
  generateChapterImpl:
    | (() => Promise<GenerateChapterResponse>)
    | null;
}
const apiState: ApiState = { generateChapterImpl: null };

const BOOK_ID = 'b-silo-42';
const CHAPTER_1_ID = 'c-1';
const CHAPTER_2_ID = 'c-2';

const BOOK_FIXTURE: BookDetailResponse = {
  id: BOOK_ID,
  title: 'Silo 42',
  author: 'Zev Paiss',
  source_format: 'epub',
  status: 'analyzed',
  chapter_count: 2,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
  chapters: [
    {
      id: CHAPTER_1_ID,
      number: 1,
      title: 'Descent',
      word_count: 3410,
      generation_state: 'none',
    },
    {
      id: CHAPTER_2_ID,
      number: 2,
      title: 'The Lower Levels',
      word_count: 4002,
      generation_state: 'none',
    },
  ],
};

const NARRATOR_FIXTURE: CharacterResponse = {
  id: 'n',
  name: 'Narrator',
  color: '#6d8bff',
  voice_type: 'designed',
  voice_label: 'Brian (warm)',
  is_library: false,
  is_narrator: true,
  dialogue_count: 0,
  confidence: 1,
  aliases: [],
};

const MIRA_FIXTURE: CharacterResponse = {
  id: 'm',
  name: 'Mira',
  color: '#34d399',
  voice_type: 'designed',
  voice_label: 'Aurora',
  is_library: false,
  is_narrator: false,
  role: 'major',
  dialogue_count: 142,
  confidence: 0.9,
  aliases: [],
};

// ─── Browser EventSource stub ────────────────────────────────────────────────
//
// jsdom does not implement `EventSource`. We install a minimal global
// implementation that records the live instance so tests can call
// `emitProgressEvent(...)` to drive a real `MessageEvent` through the same
// `source.onmessage` handler the real `useBookProgress` registers.

interface FakeMessageEvent {
  data: string;
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onmessage: ((event: FakeMessageEvent) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
}

function emitProgressEvent(event: BookProgressEvent): void {
  const live = FakeEventSource.instances.filter((s) => !s.closed);
  if (live.length === 0) {
    throw new Error(
      'emitProgressEvent called but no live EventSource exists — did BookOverview mount?',
    );
  }
  const data = JSON.stringify(event);
  for (const source of live) {
    source.onmessage?.({ data });
  }
}

function installApiSpies(): void {
  vi.spyOn(apiClient, 'getBook').mockImplementation(async () => BOOK_FIXTURE);
  vi.spyOn(apiClient, 'getCharacters').mockImplementation(async () => [
    NARRATOR_FIXTURE,
    MIRA_FIXTURE,
  ]);
  vi.spyOn(apiClient, 'getBookEventsUrl').mockImplementation(
    (bookId: string) => `http://test.local/books/${bookId}/events`,
  );
  vi.spyOn(apiClient, 'generateChapter').mockImplementation(
    async (
      bookId: string,
      chapterId: string,
      data?: GenerateChapterRequest,
    ): Promise<GenerateChapterResponse> => {
      generateCalls.push({ bookId, chapterId, data });
      if (apiState.generateChapterImpl) {
        return apiState.generateChapterImpl();
      }
      return {
        book_id: bookId,
        chapter_id: chapterId,
        task_id: 'task-1',
        queued_segments: 3,
      };
    },
  );
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

function Wrap({
  client,
  children,
}: {
  client: QueryClient;
  children: React.ReactNode;
}) {
  return React.createElement(QueryClientProvider, { client }, children);
}

async function renderOverview(): Promise<void> {
  render(
    React.createElement(Wrap, {
      client: makeQueryClient(),
      children: React.createElement(BookOverview),
    }),
  );
  await screen.findByTestId('book-header');
}

let realEventSource: typeof globalThis.EventSource | undefined;

beforeEach(() => {
  generateCalls.length = 0;
  apiState.generateChapterImpl = null;
  FakeEventSource.instances = [];
  realEventSource = globalThis.EventSource;
  // Install browser EventSource stub so the real `useBookProgress` hook can
  // construct one without exploding in jsdom.
  (globalThis as { EventSource: unknown }).EventSource =
    FakeEventSource as unknown as typeof globalThis.EventSource;
  useBooksStore.getState().reset();
  useBooksStore.getState().setSelectedBookId(BOOK_ID);
  useBooksStore.getState().setView('overview');
  installApiSpies();
});

afterEach(() => {
  vi.restoreAllMocks();
  useBooksStore.getState().reset();
  if (realEventSource !== undefined) {
    (globalThis as { EventSource: unknown }).EventSource = realEventSource;
  } else {
    delete (globalThis as { EventSource?: unknown }).EventSource;
  }
  FakeEventSource.instances = [];
});

describe('S6: Generate audio for a chapter', () => {
  it(
    'S6: clicking Generate on a chapter row sends a generate request for THAT chapter and a progress event flips the row to "generating n/m"',
    async () => {
      await renderOverview();

      // ── Pre-condition: the chapter row shows the baseline `none` badge.
      const chapterList = screen.getByTestId('chapter-list');
      const noneBadges = within(chapterList).getAllByText('none');
      expect(noneBadges).toHaveLength(2);
      // No play affordance is offered yet — audio doesn't exist.
      expect(
        within(chapterList).queryByLabelText('play-chapter-1'),
      ).not.toBeInTheDocument();

      // ── User action: click "Generate" on chapter 1.
      const user = userEvent.setup();
      await user.click(screen.getByTestId('generate-chapter-1'));

      // ── Observable A: the HTTP request that crossed the boundary targeted
      //    BOOK_ID + CHAPTER_1_ID. This is the "audio is being generated for
      //    the chapter the user clicked" half of S6.
      await waitFor(() => {
        expect(generateCalls).toHaveLength(1);
      });
      expect(generateCalls[0]).toMatchObject({
        bookId: BOOK_ID,
        chapterId: CHAPTER_1_ID,
      });
      // The request did NOT also target the unrelated chapter — the click
      // was scoped to the row the user actually pressed.
      expect(
        generateCalls.find((c) => c.chapterId === CHAPTER_2_ID),
      ).toBeUndefined();

      // ── Observable B: SSE delivers an in-progress event. The visible badge
      //    must reflect generation status (the user sees their generation is
      //    making progress on THIS chapter).
      act(() => {
        emitProgressEvent({
          type: 'generation_progress',
          chapter_id: CHAPTER_1_ID,
          completed: 1,
          errors: 0,
          total: 3,
          overall_progress: 0.33,
        });
      });
      await waitFor(() => {
        expect(
          within(chapterList).getByText(/generating 1\/3/i),
        ).toBeInTheDocument();
      });
      // The OTHER chapter (which the user did not click) is unaffected — it
      // still shows the baseline state, proving the progress event only
      // flipped the targeted row.
      expect(
        within(chapterList).getAllByText('none').length,
      ).toBeGreaterThanOrEqual(1);
    },
    20_000,
  );

  it(
    'S6: a generation_complete event flips the chapter row to "done" and surfaces the play affordance — the user can now listen',
    async () => {
      await renderOverview();
      const chapterList = screen.getByTestId('chapter-list');

      // Kick off generation so the row is in the in-flight state first.
      const user = userEvent.setup();
      await user.click(screen.getByTestId('generate-chapter-1'));
      await waitFor(() => {
        expect(generateCalls).toHaveLength(1);
      });

      // Stream a progress tick (the user sees motion before completion).
      act(() => {
        emitProgressEvent({
          type: 'generation_progress',
          chapter_id: CHAPTER_1_ID,
          completed: 2,
          errors: 0,
          total: 3,
          overall_progress: 0.66,
        });
      });
      await waitFor(() => {
        expect(
          within(chapterList).getByText(/generating 2\/3/i),
        ).toBeInTheDocument();
      });

      // ── Final SSE event: generation_complete.
      act(() => {
        emitProgressEvent({
          type: 'generation_complete',
          chapter_id: CHAPTER_1_ID,
        });
      });

      // ── Observable A: the visible badge says `done` — generation finished
      //    for this chapter, and the user knows audio is ready.
      await waitFor(() => {
        expect(within(chapterList).getByText('done')).toBeInTheDocument();
      });

      // ── Observable B: the play affordance is now in the document — the
      //    user can listen to the generated audio. This is the concrete
      //    "audio appears" half of S6.
      expect(
        within(chapterList).getByLabelText('play-chapter-1'),
      ).toBeInTheDocument();

      // ── Observable C: the unrelated chapter has neither badge nor play
      //    affordance — completion was scoped to the clicked row.
      expect(
        within(chapterList).queryByLabelText('play-chapter-2'),
      ).not.toBeInTheDocument();
    },
    20_000,
  );

  it(
    'S6: while a chapter is in-flight its Generate button is disabled so the user cannot re-queue the same job',
    async () => {
      // Keep the mutation pending so the row stays in the in-flight set.
      apiState.generateChapterImpl = () => new Promise(() => {});
      await renderOverview();

      const btn1 = screen.getByTestId('generate-chapter-1');
      // Pre-condition: clickable before any generation starts.
      expect(btn1).not.toBeDisabled();

      const user = userEvent.setup();
      await user.click(btn1);

      // Observable: the button disables — the user can't double-submit a
      // chapter that's already generating (a real-world UX guard on S6).
      await waitFor(() => {
        expect(btn1).toBeDisabled();
      });

      // And the unrelated chapter's button remains enabled — disable is
      // scoped to the in-flight row only.
      expect(screen.getByTestId('generate-chapter-2')).not.toBeDisabled();

      // Exactly one request was sent — no duplicate enqueued for the same
      // chapter.
      expect(
        generateCalls.filter((c) => c.chapterId === CHAPTER_1_ID),
      ).toHaveLength(1);
    },
    20_000,
  );

  it(
    'S6: generating two chapters one after the other lands two distinct requests and both rows reach `done`',
    async () => {
      // Whole-book generation in the current surface is exercised one
      // chapter at a time via the per-row Generate button (the
      // generate-all-btn is intentionally disabled in this build — see
      // BookOverview comments). This case verifies the "book" half of S6
      // by walking through both chapters in sequence and confirming each
      // one independently reaches its observable end state.
      await renderOverview();
      const chapterList = screen.getByTestId('chapter-list');

      const user = userEvent.setup();

      // ── Chapter 1.
      await user.click(screen.getByTestId('generate-chapter-1'));
      await waitFor(() => {
        expect(generateCalls).toHaveLength(1);
      });
      act(() => {
        emitProgressEvent({
          type: 'generation_complete',
          chapter_id: CHAPTER_1_ID,
        });
      });
      await waitFor(() => {
        expect(
          within(chapterList).getByLabelText('play-chapter-1'),
        ).toBeInTheDocument();
      });

      // ── Chapter 2.
      await user.click(screen.getByTestId('generate-chapter-2'));
      await waitFor(() => {
        expect(generateCalls).toHaveLength(2);
      });
      act(() => {
        emitProgressEvent({
          type: 'generation_complete',
          chapter_id: CHAPTER_2_ID,
        });
      });
      await waitFor(() => {
        expect(
          within(chapterList).getByLabelText('play-chapter-2'),
        ).toBeInTheDocument();
      });

      // ── Both requests targeted the right chapter ids. The order of
      //    captured calls matches the click order — there's no smuggled
      //    cross-talk where a click on row 1 enqueued row 2 or vice versa.
      expect(generateCalls.map((c) => c.chapterId)).toEqual([
        CHAPTER_1_ID,
        CHAPTER_2_ID,
      ]);
      expect(generateCalls.every((c) => c.bookId === BOOK_ID)).toBe(true);

      // ── Final observable: both chapters are playable — the book as a
      //    whole now has audio for every chapter.
      expect(
        within(chapterList).getByLabelText('play-chapter-1'),
      ).toBeInTheDocument();
      expect(
        within(chapterList).getByLabelText('play-chapter-2'),
      ).toBeInTheDocument();
      // No chapter is still showing the baseline `none` badge — every row
      // has advanced past the pre-generation state.
      expect(within(chapterList).queryByText('none')).not.toBeInTheDocument();
    },
    20_000,
  );

  it(
    'S6: a generation error on one chapter does not crash the overview — the row surfaces a Retry affordance',
    async () => {
      await renderOverview();
      const chapterList = screen.getByTestId('chapter-list');

      // Kick off generation; once in-flight, an SSE progress event with
      // `errors > 0` is the contract-04 way a partially-failed batch is
      // reported. The row must show a Retry control so the user can recover
      // — this is the "audio generation" path's failure-mode UX.
      const user = userEvent.setup();
      await user.click(screen.getByTestId('generate-chapter-1'));
      await waitFor(() => {
        expect(generateCalls).toHaveLength(1);
      });

      act(() => {
        emitProgressEvent({
          type: 'generation_progress',
          chapter_id: CHAPTER_1_ID,
          completed: 1,
          errors: 2,
          total: 3,
          overall_progress: 0.33,
        });
      });

      // Observable: the row is still in the document (the overview did not
      // crash on the error event) and the Retry affordance is visible so
      // the user can re-attempt generation on the failed segments.
      await waitFor(() => {
        expect(
          within(chapterList).getByTestId('retry-chapter-1'),
        ).toBeInTheDocument();
      });
      // The unrelated chapter is unaffected by the error report.
      expect(
        within(chapterList).queryByTestId('retry-chapter-2'),
      ).not.toBeInTheDocument();
    },
    20_000,
  );
});
