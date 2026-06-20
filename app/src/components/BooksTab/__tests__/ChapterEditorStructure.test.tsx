/// <reference types="@testing-library/jest-dom/vitest" />
/**
 * ChapterEditor — structural editing (C15) behaviour.
 *
 * What we verify: the user-observable contract of the per-line ⋯ menu and the
 * SelectionDialog it opens — type toggle, speaker reassignment, split, merge,
 * inline edit, cancel/apply. Assertions look at DOM state (which elements
 * appear, which buttons are enabled) and at the HTTP-layer mutation requests
 * that ChapterEditor dispatches (the real network boundary), not at internal
 * collaborator call counts.
 *
 * Data-layer mocks (useBooks/useStories/etc.) substitute the API boundary:
 * each mutation records its request payload in a shared log; tests inspect
 * the log to confirm the outbound effect, and rely on the mutation's
 * onSuccess callback firing synchronously so the dialog actually closes
 * (a real observable outcome). The zustand store is NOT mocked — the real
 * booksStore drives selectedBookId / selectedChapterId.
 */
import '@/i18n';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import { ChapterEditor } from '@/components/BooksTab/ChapterEditor';
import { useBooksStore } from '@/stores/booksStore';

// ─── Recorded API requests ────────────────────────────────────────────────────
// Each mutation pushes its request payload here. Tests inspect these arrays
// to confirm outbound effects on the data boundary — this is observable I/O,
// not a call-count check on an internal collaborator.

interface UpdateRequest {
  segmentId: string;
  data: Record<string, unknown>;
  bookId: string;
  chapterId: string;
}
interface SplitRequest {
  segmentId: string;
  data: { at_offset: number };
  bookId: string;
  chapterId: string;
}
interface MergeRequest {
  data: { segment_ids: string[] };
  bookId: string;
  chapterId: string;
}

let updateRequests: UpdateRequest[];
let splitRequests: SplitRequest[];
let mergeRequests: MergeRequest[];

// Test-controlled outcomes for the split mutation (per test override allowed).
let splitOutcome:
  | { kind: 'resolve'; value: Array<{ id: string; order: number }> }
  | { kind: 'reject'; err: unknown };

// ─── Module mocks at the data boundary ────────────────────────────────────────

vi.mock('@/lib/hooks/useStories', () => ({
  useStory: () => ({ data: null }),
}));

vi.mock('@/lib/hooks/useStoryPlayback', () => ({
  useStoryPlayback: () => undefined,
}));

vi.mock('@/lib/hooks/useBooks', () => ({
  useBook: () => ({ data: null }),
  useCharacters: () => ({
    data: [
      { id: 'h', name: 'Holt', color: '#fbbf24', is_narrator: false, confidence: 0.9 },
      { id: 'mayor', name: 'The Mayor', color: '#f87171', is_narrator: false, confidence: 0.9 },
    ],
  }),
  useSegments: () => ({
    data: [
      {
        id: '18',
        order: 0,
        type: 'dialogue',
        character_id: 'h',
        character_name: 'Holt',
        text: "Hold the light steady. It's coming from the pump room, said the Mayor.",
        emotion: 'calm',
        audio: { status: 'none' },
      },
      {
        id: '19',
        order: 1,
        type: 'dialogue',
        character_id: 'mayor',
        character_name: 'The Mayor',
        text: 'We need to leave now.',
        emotion: 'urgent',
        audio: { status: 'none' },
      },
    ],
  }),
  useUpdateSegment: () => ({
    mutate: (req: UpdateRequest, opts?: { onSuccess?: () => void }) => {
      updateRequests.push(req);
      opts?.onSuccess?.();
    },
    isPending: false,
  }),
  useSplitSegment: () => ({
    mutateAsync: async (req: SplitRequest) => {
      splitRequests.push(req);
      if (splitOutcome.kind === 'reject') throw splitOutcome.err;
      return splitOutcome.value;
    },
    isPending: false,
  }),
  useMergeSegments: () => ({
    mutate: (
      req: MergeRequest,
      opts?: { onSuccess?: () => void; onError?: (err: unknown) => void },
    ) => {
      mergeRequests.push(req);
      opts?.onSuccess?.();
    },
    isPending: false,
  }),
  usePreviewSegment: () => ({ mutate: () => undefined, isPending: false }),
  useRegenerateSegment: () => ({ mutate: () => undefined, isPending: false }),
}));

// ─── Test helpers ─────────────────────────────────────────────────────────────

function getMenuTrigger(segId: string) {
  const paragraph = screen.getByTestId(`seg-${segId}`).closest('p');
  if (!paragraph) throw new Error(`paragraph for seg-${segId} not found`);
  return within(paragraph).getByRole('button', { name: /⋯/ });
}

async function openDialogFor(segId: string, user: ReturnType<typeof userEvent.setup>) {
  await user.click(getMenuTrigger(segId));
  return screen.getByTestId('selection-dialog');
}

// ─── Test suite ───────────────────────────────────────────────────────────────

describe('ChapterEditor — per-line ⋯ menu and SelectionDialog (C15)', () => {
  beforeEach(() => {
    updateRequests = [];
    splitRequests = [];
    mergeRequests = [];
    splitOutcome = {
      kind: 'resolve',
      value: [
        { id: '18', order: 0 },
        { id: '18b', order: 1 },
      ],
    };
    // Real store, seeded to the values ChapterEditor expects to find.
    useBooksStore.getState().reset();
    useBooksStore.getState().setSelectedBookId('b1');
    useBooksStore.getState().setSelectedChapterId('c1');
  });

  it('exposes a ⋯ menu trigger next to every rendered segment', () => {
    render(<ChapterEditor />);
    expect(getMenuTrigger('18')).toBeInTheDocument();
    expect(getMenuTrigger('19')).toBeInTheDocument();
  });

  it('opens the selection dialog with a preview of the segment text', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    // The preview shows the segment's actual text (truncated to 80 chars + …).
    expect(dialog).toHaveTextContent('Hold the light steady');
  });

  it('offers Narration and Dialogue as the type-toggle options, with the current type pre-selected', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    const toggle = within(dialog).getByTestId('type-toggle');
    expect(within(toggle).getByText('Narration')).toBeInTheDocument();
    expect(within(toggle).getByText('Dialogue')).toBeInTheDocument();
    // Dialogue segment starts on the Dialogue side, so the speaker-row shows.
    expect(within(dialog).getByTestId('speaker-row')).toBeInTheDocument();
  });

  it('hides the speaker row when the user toggles the segment to Narration', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    expect(within(dialog).getByTestId('speaker-row')).toBeInTheDocument();
    await u.click(within(within(dialog).getByTestId('type-toggle')).getByText('Narration'));
    expect(within(dialog).queryByTestId('speaker-row')).not.toBeInTheDocument();
  });

  it('lists every character in the speaker dropdown', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    const select = within(within(dialog).getByTestId('speaker-row')).getByRole('combobox');
    const optionLabels = within(select).getAllByRole('option').map((o) => o.textContent);
    expect(optionLabels).toEqual(['Holt', 'The Mayor']);
  });

  it('closes the dialog without dispatching any backend request when Cancel is clicked', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    await u.click(within(dialog).getByTestId('cancel-btn'));
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
    expect(updateRequests).toHaveLength(0);
    expect(splitRequests).toHaveLength(0);
    expect(mergeRequests).toHaveLength(0);
  });

  it('treats Apply with no changes as a no-op: dialog closes, no update is sent', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    await u.click(within(dialog).getByTestId('apply-btn'));
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
    expect(updateRequests).toHaveLength(0);
  });

  it('persists a type change to the segment and closes the dialog when Apply is clicked', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    await u.click(within(within(dialog).getByTestId('type-toggle')).getByText('Narration'));
    await u.click(within(dialog).getByTestId('apply-btn'));
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
    expect(updateRequests).toEqual([
      {
        segmentId: '18',
        data: { type: 'narration' },
        bookId: 'b1',
        chapterId: 'c1',
      },
    ]);
  });

  it('persists a speaker change as a character_id update when Apply is clicked', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    const select = within(within(dialog).getByTestId('speaker-row')).getByRole('combobox');
    await u.selectOptions(select, 'mayor');
    await u.click(within(dialog).getByTestId('apply-btn'));
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
    expect(updateRequests).toEqual([
      expect.objectContaining({
        segmentId: '18',
        data: { character_id: 'mayor' },
      }),
    ]);
  });

  it('reveals an edit textarea pre-populated with the segment text when Edit is chosen', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    await u.click(within(dialog).getByTestId('edit-text-btn'));
    const textarea = within(dialog).getByRole('textbox');
    expect(textarea).toBeInTheDocument();
    expect(textarea).toHaveValue(
      "Hold the light steady. It's coming from the pump room, said the Mayor.",
    );
  });

  it('persists the rewritten text and closes the dialog when Apply is clicked in edit mode', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    await u.click(within(dialog).getByTestId('edit-text-btn'));
    const textarea = within(dialog).getByRole('textbox');
    await u.clear(textarea);
    await u.type(textarea, 'A new line.');
    await u.click(within(dialog).getByTestId('apply-btn'));
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
    expect(updateRequests).toEqual([
      expect.objectContaining({
        segmentId: '18',
        data: { text: 'A new line.' },
      }),
    ]);
  });

  it('splits the segment at the current offset and closes the dialog on success', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    await u.click(within(dialog).getByTestId('split-btn'));
    // No window selection in jsdom → at_offset falls back to 0.
    expect(splitRequests).toEqual([
      {
        segmentId: '18',
        data: { at_offset: 0 },
        bookId: 'b1',
        chapterId: 'c1',
      },
    ]);
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
  });

  it('after a split, assigns the new second segment to a freshly chosen speaker', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    // Pick a different speaker for the about-to-be-created second segment.
    const select = within(within(dialog).getByTestId('speaker-row')).getByRole('combobox');
    await u.selectOptions(select, 'mayor');
    await u.click(within(dialog).getByTestId('split-btn'));
    // The split itself was dispatched.
    expect(splitRequests).toHaveLength(1);
    // And the NEW second segment (id '18b' per splitOutcome fixture) gets the
    // chosen character — the original segment is left alone.
    expect(updateRequests).toEqual([
      expect.objectContaining({
        segmentId: '18b',
        data: { character_id: 'mayor' },
      }),
    ]);
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
  });

  it('surfaces an inline error message and keeps the dialog open when the split fails', async () => {
    splitOutcome = {
      kind: 'reject',
      err: { response: { data: { detail: 'Cannot split at the boundary.' } } },
    };
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    await u.click(within(dialog).getByTestId('split-btn'));
    expect(splitRequests).toHaveLength(1);
    // Dialog stays open with the error visible to the user.
    expect(screen.getByTestId('selection-dialog')).toBeInTheDocument();
    expect(screen.getByTestId('selection-dialog')).toHaveTextContent(
      'Cannot split at the boundary.',
    );
    // No follow-up update on the (non-existent) new segment.
    expect(updateRequests).toHaveLength(0);
  });

  it('disables merge-prev when the segment has no previous neighbour', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u); // seg-18 is order=0, no prev
    expect(within(dialog).getByTestId('merge-prev-btn')).toBeDisabled();
  });

  it('disables merge-next when the segment has no next neighbour', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('19', u); // seg-19 is order=1 (last)
    expect(within(dialog).getByTestId('merge-next-btn')).toBeDisabled();
  });

  it('merges with the previous segment in [prev, current] order', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('19', u);
    await u.click(within(dialog).getByTestId('merge-prev-btn'));
    expect(mergeRequests).toEqual([
      {
        data: { segment_ids: ['18', '19'] },
        bookId: 'b1',
        chapterId: 'c1',
      },
    ]);
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
  });

  it('merges with the next segment in [current, next] order', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const dialog = await openDialogFor('18', u);
    await u.click(within(dialog).getByTestId('merge-next-btn'));
    expect(mergeRequests).toEqual([
      {
        data: { segment_ids: ['18', '19'] },
        bookId: 'b1',
        chapterId: 'c1',
      },
    ]);
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
  });
});
