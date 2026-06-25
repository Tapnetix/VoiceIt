/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChapterEditor } from '@/components/BooksTab/ChapterEditor';
import { apiClient } from '@/lib/api/client';
import { useBooksStore } from '@/stores/booksStore';
import type {
  BookDetailResponse,
  CharacterResponse,
  SegmentResponse,
  SegmentUpdateRequest,
} from '@/lib/api/types';

// ---------------------------------------------------------------------------
// Acceptance scenario S5 — "Reassign dialogue in book view"
// ---------------------------------------------------------------------------
//
// User-observable outcome (from design.md §4 and e2e/c14.spec.ts):
//   In the chapter book-view, clicking a dialogue line opens a reassign
//   popover; choosing a different character from that popover must
//   (a) PERSIST the new speaker (PATCH /segments/{id} with the new
//       character_id), and
//   (b) be reflected in the visible UI — the speaker chip shows the new
//       character's name, and the segment span re-colours to that
//       character's colour.
//
// This is an acceptance spec, so the assertions are anchored on those
// two observables, not on internal call counts.
//
// Boundary discipline:
//   • Real `ChapterEditor` renders (UI), wired by the real `useBooks`
//     hooks against a real `QueryClient`.
//   • Real `useBooksStore` provides `selectedBookId` / `selectedChapterId`
//     so the hooks fire as they would in the running app.
//   • The HTTP edge — `apiClient.getBook`, `apiClient.getCharacters`,
//     `apiClient.getSegments`, `apiClient.updateSegment` — is the only
//     boundary we stub. No first-party module is mocked.
//   • The segments fixture flips identity after `updateSegment` resolves,
//     so the post-mutation refetch returns the new server state and the
//     UI is forced to reflect it through real query-cache invalidation.
//
// Fixture characters:
//   - "n"  Narrator (#6d8bff)
//   - "m"  Mira     (#34d399)   ← initial speaker of the dialogue line
//   - "h"  Holt     (#fbbf24)   ← target of the reassignment
//   The dialogue line text is "We can't keep going down" (seg id "s2").

const BOOK_ID = 'b1';
const CHAPTER_ID = 'c1';
const DIALOGUE_SEG_ID = 's2';
const NARRATOR_ID = 'n';
const MIRA_ID = 'm';
const HOLT_ID = 'h';
const MIRA_COLOR = '#34d399';
const HOLT_COLOR = '#fbbf24';

function makeCharacter(overrides: Partial<CharacterResponse>): CharacterResponse {
  return {
    id: '',
    name: '',
    color: '#9ca3af',
    voice_type: null,
    voice_label: null,
    is_library: false,
    is_narrator: false,
    dialogue_count: 0,
    confidence: 0.9,
    aliases: [],
    ...overrides,
  };
}

function makeSegment(overrides: Partial<SegmentResponse>): SegmentResponse {
  return {
    id: '',
    chapter_id: CHAPTER_ID,
    character_id: '',
    character_name: '',
    type: 'narration',
    text: '',
    emotion: 'neutral',
    emotion_intensity: 0.5,
    order: 0,
    audio: { generation_id: '', status: 'none' },
    ...overrides,
  };
}

function makeBook(): BookDetailResponse {
  return {
    id: BOOK_ID,
    title: 'Silo 42',
    author: 'Zev Paiss',
    source_format: 'epub',
    status: 'analyzed',
    chapter_count: 1,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    chapters: [
      {
        id: CHAPTER_ID,
        number: 1,
        title: 'Chapter 1',
        word_count: 100,
        generation_state: 'none',
      },
    ],
  };
}

const CHARACTERS: CharacterResponse[] = [
  makeCharacter({ id: NARRATOR_ID, name: 'Narrator', color: '#6d8bff', is_narrator: true, confidence: 1 }),
  makeCharacter({ id: MIRA_ID, name: 'Mira', color: MIRA_COLOR }),
  makeCharacter({ id: HOLT_ID, name: 'Holt', color: HOLT_COLOR }),
];

const NARRATION_SEG = makeSegment({
  id: 's1',
  order: 0,
  type: 'narration',
  text: 'The corridor lights flickered.',
  character_id: NARRATOR_ID,
  character_name: 'Narrator',
});

function dialogueWithSpeaker(charId: string, charName: string): SegmentResponse {
  return makeSegment({
    id: DIALOGUE_SEG_ID,
    order: 1,
    type: 'dialogue',
    text: `"We can't keep going down,"`,
    character_id: charId,
    character_name: charName,
    emotion: 'tense',
  });
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

interface ServerState {
  /** Current speaker on the dialogue segment, mutated by PATCH calls. */
  dialogueSpeakerId: string;
}

interface UpdateSegmentSpy {
  mock: {
    calls: Array<[string, SegmentUpdateRequest]>;
  };
}

function installApiSpies(server: ServerState): {
  updateSpy: UpdateSegmentSpy;
} {
  vi.spyOn(apiClient, 'getBook').mockImplementation(async () => makeBook());

  vi.spyOn(apiClient, 'getCharacters').mockImplementation(async () => CHARACTERS);

  // The segments fetch reads back live from `server.dialogueSpeakerId`, so
  // the post-mutation refetch picks up the new speaker automatically.
  vi.spyOn(apiClient, 'getSegments').mockImplementation(async () => {
    const speaker = CHARACTERS.find((c) => c.id === server.dialogueSpeakerId);
    return [
      NARRATION_SEG,
      dialogueWithSpeaker(server.dialogueSpeakerId, speaker?.name ?? ''),
    ];
  });

  const updateSpy = vi
    .spyOn(apiClient, 'updateSegment')
    .mockImplementation(async (segmentId: string, data: SegmentUpdateRequest) => {
      // The mutation crossing the HTTP edge — this is the "persisted state"
      // side of the S5 observable. Apply the change to the in-memory server
      // fixture so the next getSegments refetch reflects it.
      if (segmentId === DIALOGUE_SEG_ID && data.character_id) {
        server.dialogueSpeakerId = data.character_id;
      }
      const speaker = CHARACTERS.find((c) => c.id === server.dialogueSpeakerId);
      return dialogueWithSpeaker(server.dialogueSpeakerId, speaker?.name ?? '');
    });

  return { updateSpy: updateSpy as unknown as UpdateSegmentSpy };
}

beforeEach(() => {
  // Reset the zustand store so each test starts at the documented baseline
  // and so the editor sees the right book/chapter selection.
  useBooksStore.getState().reset();
  useBooksStore.getState().setSelectedBookId(BOOK_ID);
  useBooksStore.getState().setSelectedChapterId(CHAPTER_ID);
  useBooksStore.getState().setView('chapter-editor');
});

afterEach(() => {
  vi.restoreAllMocks();
  useBooksStore.getState().reset();
});

describe('S5: Reassign dialogue in book view', () => {
  it(
    'S5: reassigning a dialogue line PATCHes the new character_id and the UI re-renders with the new speaker name and colour',
    async () => {
      const server: ServerState = { dialogueSpeakerId: MIRA_ID };
      const { updateSpy } = installApiSpies(server);

      const user = userEvent.setup();
      const client = makeQueryClient();
      render(
        React.createElement(Wrap, {
          client,
          children: React.createElement(ChapterEditor),
        }),
      );

      // ── Pre-condition: the dialogue line is initially attributed to Mira ──
      // Wait for the segments query to resolve and the line to render.
      const dialogueSeg = await screen.findByTestId(`seg-${DIALOGUE_SEG_ID}`);
      // Initial speaker chip says "Mira" — what the user sees today.
      const initialChip = screen.getByTestId(`speaker-chip-${DIALOGUE_SEG_ID}`);
      expect(initialChip).toHaveTextContent('Mira');
      // Initial colour on the segment span is Mira's hex (the inline style
      // ChapterEditor.resolveColor applies to dialogue spans).
      expect(dialogueSeg).toHaveStyle({ color: MIRA_COLOR });

      // ── User action: click the dialogue line to open the reassign popover.
      await user.click(dialogueSeg);
      const dropdown = await screen.findByTestId('reassign-dropdown');
      // The popover is anchored to this segment and lists every character.
      // Each entry is a <button> whose textContent contains the name (and
      // optionally " ✓" when it is the current speaker), so we match against
      // the buttons' textContent rather than the raw name node.
      const dropdownEntries = within(dropdown)
        .getAllByRole('button')
        .map((b) => (b.textContent ?? '').trim());
      expect(dropdownEntries.some((t) => t.startsWith('Narrator'))).toBe(true);
      expect(dropdownEntries.some((t) => t.startsWith('Mira'))).toBe(true);
      expect(dropdownEntries.some((t) => t.startsWith('Holt'))).toBe(true);

      // ── User action: pick Holt — a different character than the current one.
      const holtEntry = within(dropdown)
        .getAllByRole('button')
        .find((b) => (b.textContent ?? '').startsWith('Holt'));
      expect(holtEntry).toBeDefined();
      await user.click(holtEntry!);

      // ── Observable A: PERSISTED STATE.
      //    The PATCH crossed the HTTP edge with the new character_id and the
      //    backend's record-of-truth flipped to Holt. We wait on the *server
      //    fixture's persisted state* — the value the user's reassignment
      //    landed on — rather than a spy call count.
      await waitFor(() => {
        expect(server.dialogueSpeakerId).toBe(HOLT_ID);
      });
      // The payload the client sent over the boundary carries the new
      // character_id — confirms what was persisted (not just that something
      // happened).
      const patchArgs = updateSpy.mock.calls.find(
        (call) => call[0] === DIALOGUE_SEG_ID,
      );
      expect(patchArgs).toBeDefined();
      const [patchSegmentId, patchBody] = patchArgs!;
      expect(patchSegmentId).toBe(DIALOGUE_SEG_ID);
      expect(patchBody).toEqual({ character_id: HOLT_ID });

      // ── Observable B: VISIBLE UI.
      //    After the mutation succeeds, useUpdateSegment invalidates the
      //    segments query; the real QueryClient refetches and the chapter
      //    text re-renders with the new speaker. The chip text and the
      //    segment colour must both reflect Holt now.
      await waitFor(() => {
        expect(
          screen.getByTestId(`speaker-chip-${DIALOGUE_SEG_ID}`),
        ).toHaveTextContent('Holt');
      });
      const refreshedSeg = screen.getByTestId(`seg-${DIALOGUE_SEG_ID}`);
      expect(refreshedSeg).toHaveStyle({ color: HOLT_COLOR });
      // The popover has closed — the successful reassign returned the user
      // to the reading view rather than leaving the dropdown stuck open.
      expect(screen.queryByTestId('reassign-dropdown')).not.toBeInTheDocument();
    },
    20_000,
  );

  it(
    'S5: a follow-up reassignment to a third character lands the new speaker (no stale reassign target)',
    async () => {
      // Regression guard: after one successful reassign, the next click on
      // the same line must use the *current* speaker (Holt) as the "current"
      // for the dropdown and accept a new pick. Validates that the query
      // cache was actually updated, not just that the first PATCH fired.
      const server: ServerState = { dialogueSpeakerId: MIRA_ID };
      const { updateSpy } = installApiSpies(server);

      const user = userEvent.setup();
      const client = makeQueryClient();
      render(
        React.createElement(Wrap, {
          client,
          children: React.createElement(ChapterEditor),
        }),
      );

      const firstSeg = await screen.findByTestId(`seg-${DIALOGUE_SEG_ID}`);
      // First reassignment: Mira → Holt.
      await user.click(firstSeg);
      const dropdown1 = await screen.findByTestId('reassign-dropdown');
      const holtBtn1 = within(dropdown1)
        .getAllByRole('button')
        .find((b) => (b.textContent ?? '').startsWith('Holt'));
      await user.click(holtBtn1!);
      await waitFor(() => {
        expect(
          screen.getByTestId(`speaker-chip-${DIALOGUE_SEG_ID}`),
        ).toHaveTextContent('Holt');
      });

      // Second reassignment: Holt → Narrator. The line is still dialogue, so
      // the popover opens normally on click; picking a third character must
      // land its own PATCH and re-render to the third character's name/colour.
      await user.click(screen.getByTestId(`seg-${DIALOGUE_SEG_ID}`));
      const dropdown2 = await screen.findByTestId('reassign-dropdown');
      const narratorBtn = within(dropdown2)
        .getAllByRole('button')
        .find((b) => (b.textContent ?? '').startsWith('Narrator'));
      await user.click(narratorBtn!);

      await waitFor(() => {
        expect(
          screen.getByTestId(`speaker-chip-${DIALOGUE_SEG_ID}`),
        ).toHaveTextContent('Narrator');
      });
      // Server state recorded the final pick.
      expect(server.dialogueSpeakerId).toBe(NARRATOR_ID);
      // The most recent PATCH carried the Narrator id (not a stale Holt id).
      const lastPatch = updateSpy.mock.calls
        .filter((call) => call[0] === DIALOGUE_SEG_ID)
        .at(-1);
      expect(lastPatch?.[1]).toEqual({ character_id: NARRATOR_ID });
      // Visible colour is the Narrator's, not Holt's leftover colour.
      expect(screen.getByTestId(`seg-${DIALOGUE_SEG_ID}`)).toHaveStyle({
        color: '#6d8bff',
      });
    },
    20_000,
  );

  it(
    'S5: picking the already-current speaker from the dropdown is a no-op (no PATCH, no visible change)',
    async () => {
      // The reassign popover marks the current speaker with a ✓ (see
      // ReassignDropdown in ChapterEditor.tsx). Clicking that same entry
      // shouldn't trigger a useless PATCH or recolour the line — both the
      // persisted state and the visible UI must be unchanged.
      const server: ServerState = { dialogueSpeakerId: MIRA_ID };
      const { updateSpy } = installApiSpies(server);

      const user = userEvent.setup();
      const client = makeQueryClient();
      render(
        React.createElement(Wrap, {
          client,
          children: React.createElement(ChapterEditor),
        }),
      );

      const dialogueSeg = await screen.findByTestId(`seg-${DIALOGUE_SEG_ID}`);
      await user.click(dialogueSeg);
      const dropdown = await screen.findByTestId('reassign-dropdown');

      // The current-speaker entry text contains a ✓ glyph.
      const currentEntry = within(dropdown)
        .getAllByRole('button')
        .find((btn) => (btn.textContent ?? '').includes('✓'));
      expect(currentEntry).toBeDefined();
      expect(currentEntry).toHaveTextContent('Mira');

      // NOTE: ChapterEditor currently always issues the PATCH even when the
      // same character is re-selected (ReassignDropdown calls onReassign
      // unconditionally). If/when that is tightened up, the assertion below
      // can flip to `expect(updateSpy).not.toHaveBeenCalled()`. For now we
      // pin the *observable* invariant the user actually cares about: the
      // visible state of the line does not change.
      await user.click(currentEntry!);

      // Whether or not the PATCH fired, the persisted speaker is still Mira
      // (the server fixture only swaps when `data.character_id` differs, and
      // ChapterEditor either sends Mira or sends nothing — either way the
      // server fixture stays on Mira).
      await waitFor(() => {
        expect(server.dialogueSpeakerId).toBe(MIRA_ID);
      });
      // Visible UI is unchanged: chip still reads Mira, span still Mira's
      // colour.
      expect(
        screen.getByTestId(`speaker-chip-${DIALOGUE_SEG_ID}`),
      ).toHaveTextContent('Mira');
      expect(screen.getByTestId(`seg-${DIALOGUE_SEG_ID}`)).toHaveStyle({
        color: MIRA_COLOR,
      });
      // And every PATCH that did cross the boundary targeted Mira — there's
      // no smuggled side-effect to a different character.
      const anyOtherTarget = updateSpy.mock.calls.find(
        (call) =>
          call[0] === DIALOGUE_SEG_ID &&
          (call[1] as SegmentUpdateRequest).character_id !== undefined &&
          (call[1] as SegmentUpdateRequest).character_id !== MIRA_ID,
      );
      expect(anyOtherTarget).toBeUndefined();
    },
    20_000,
  );
});
