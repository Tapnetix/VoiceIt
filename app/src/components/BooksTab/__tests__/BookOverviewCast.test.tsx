/**
 * BookOverviewCast — focused interaction tests for merge/delete wiring.
 *
 * Renders C8's BookOverview with a fixture that has 1 narrator + 2 non-narrator
 * characters that represent the "same person" (Mira / Mira the woman). Asserts
 * observable outcomes:
 *   - Selecting 2 non-narrator checkboxes enables the merge-btn
 *   - Clicking merge-btn sends a merge payload with survivor=first selected,
 *     source=second selected, then clears the selection (checkboxes uncheck,
 *     merge-btn returns to disabled).
 *   - Selecting 1 non-narrator checkbox enables the delete-btn
 *   - Clicking delete-btn opens a confirm dialog (DOM); confirming sends a
 *     delete payload, closes the dialog, and clears the selection.
 *
 * C8 wiring lives in BookOverview.tsx — this test only asserts it.
 * Payload checks read mock.calls[0][0] (the value crossing the boundary)
 * rather than asserting on spy call counts, per the test quality bar.
 */
/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { BookOverview } from '@/components/BooksTab/BookOverview';

const wrap = (ui: React.ReactNode) => (
  <QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>
);

// ─── Mocks ────────────────────────────────────────────────────────────────────

const mergeMutate = vi.fn().mockResolvedValue(undefined);
const deleteMutate = vi.fn().mockResolvedValue(undefined);

vi.mock('@/stores/booksStore', () => ({
  useBooksStore: (s: (state: Record<string, unknown>) => unknown) =>
    s({
      selectedBookId: 'b1',
      setView: vi.fn(),
      setSelectedChapterId: vi.fn(),
      setSelectedCharacterId: vi.fn(),
    }),
}));

vi.mock('@/lib/hooks/useBooks', () => ({
  useBook: () => ({
    data: {
      id: 'b1',
      title: 'Silo 42',
      author: 'Zev Paiss',
      status: 'analyzed',
      source_format: 'epub',
      chapters: [],
    },
    isLoading: false,
  }),
  useCharacters: () => ({
    data: [
      {
        id: 'n',
        name: 'Narrator',
        is_narrator: true,
        color: '#6d8bff',
        dialogue_count: 0,
        confidence: 1,
        role: undefined,
        aliases: [],
      },
      {
        id: 'm1',
        name: 'Mira',
        is_narrator: false,
        color: '#34d399',
        dialogue_count: 80,
        confidence: 0.9,
        role: 'major',
        aliases: [],
      },
      {
        id: 'm2',
        name: 'Mira (the woman)',
        is_narrator: false,
        color: '#10b981',
        dialogue_count: 62,
        confidence: 0.5,
        role: 'major',
        aliases: [],
      },
    ],
    isLoading: false,
  }),
  useMergeCharacter: () => ({
    mutateAsync: mergeMutate,
    isPending: false,
  }),
  useDeleteCharacter: () => ({
    mutateAsync: deleteMutate,
    isPending: false,
  }),
  useGenerateChapter: () => ({
    mutateAsync: vi.fn().mockResolvedValue(undefined),
    isPending: false,
  }),
}));

vi.mock('@/lib/hooks/useBookProgress', () => ({
  useBookProgress: () => undefined,
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('BookOverview cast management', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders cast-roster with 2 non-narrator checkboxes (narrator has none)', () => {
    render(wrap(<BookOverview />));
    const roster = screen.getByTestId('cast-roster');
    const checkboxes = within(roster).getAllByRole('checkbox');
    // Only non-narrator characters (m1, m2) get checkboxes; Narrator does not
    expect(checkboxes).toHaveLength(2);
  });

  it('merge-btn is disabled when fewer than 2 characters are selected', () => {
    render(wrap(<BookOverview />));
    expect(screen.getByTestId('merge-btn')).toBeDisabled();

    // Select only 1 → still disabled
    const roster = screen.getByTestId('cast-roster');
    const [first] = within(roster).getAllByRole('checkbox');
    fireEvent.click(first);
    expect(screen.getByTestId('merge-btn')).toBeDisabled();
  });

  it('merges two selected characters via the B8 endpoint (survivor = first selected)', async () => {
    const u = userEvent.setup();
    render(wrap(<BookOverview />));

    const roster = screen.getByTestId('cast-roster');
    const checkboxes = within(roster).getAllByRole('checkbox');
    // Select both non-narrator characters
    await u.click(checkboxes[0]); // m1 (Mira)
    await u.click(checkboxes[1]); // m2 (Mira the woman)

    // merge-btn should be enabled now
    const mergeBtn = screen.getByTestId('merge-btn');
    expect(mergeBtn).not.toBeDisabled();

    await u.click(mergeBtn);

    // Outcome 1 — the boundary payload sent for merge identifies survivor=m1, source=m2.
    // We read the captured value rather than asserting "was the spy called", because the
    // observable outcome is the payload that crosses the API boundary.
    await waitFor(() => {
      expect(mergeMutate.mock.calls.length).toBeGreaterThan(0);
    });
    const mergePayload = mergeMutate.mock.calls[0][0];
    expect(mergePayload).toEqual({
      bookId: 'b1',
      charId: 'm1',
      data: { source_char_id: 'm2' },
    });

    // Outcome 2 — after a successful merge the selection is cleared, so the
    // merge-btn returns to its disabled state (selectedCharIds.size < 2) and
    // the previously checked boxes become unchecked.
    await waitFor(() => {
      expect(screen.getByTestId('merge-btn')).toBeDisabled();
    });
    const checkboxesAfter = within(screen.getByTestId('cast-roster')).getAllByRole('checkbox');
    for (const cb of checkboxesAfter) {
      expect(cb).not.toBeChecked();
    }
  });

  it('delete-btn is disabled when no character is selected', () => {
    render(wrap(<BookOverview />));
    expect(screen.getByTestId('delete-btn')).toBeDisabled();
  });

  it('delete-btn enables with exactly 1 selected and merge-btn stays disabled', () => {
    render(wrap(<BookOverview />));
    const roster = screen.getByTestId('cast-roster');
    const [first] = within(roster).getAllByRole('checkbox');
    fireEvent.click(first); // select m1 only

    expect(screen.getByTestId('delete-btn')).not.toBeDisabled();
    expect(screen.getByTestId('merge-btn')).toBeDisabled();
  });

  it('delete-btn opens confirm dialog and deletes the selected character on confirm', async () => {
    render(wrap(<BookOverview />));
    const roster = screen.getByTestId('cast-roster');
    const [first] = within(roster).getAllByRole('checkbox');
    fireEvent.click(first); // select m1

    const deleteBtn = screen.getByTestId('delete-btn');
    expect(deleteBtn).not.toBeDisabled();
    fireEvent.click(deleteBtn);

    // Outcome 1 — clicking delete opens the confirm dialog (observable DOM state).
    const dialog = await screen.findByRole('alertdialog');

    // Click the confirm action inside the dialog
    const confirmBtn = within(dialog).getByRole('button', { name: /delete|confirm/i });
    fireEvent.click(confirmBtn);

    // Outcome 2 — the boundary payload identifies the character to delete.
    // Read the captured value rather than asserting on spy call counts.
    await waitFor(() => {
      expect(deleteMutate.mock.calls.length).toBeGreaterThan(0);
    });
    const deletePayload = deleteMutate.mock.calls[0][0];
    expect(deletePayload).toEqual({
      bookId: 'b1',
      charId: 'm1',
    });

    // Outcome 3 — after a successful delete the confirm dialog closes and the
    // selection is cleared, so the delete-btn returns to disabled state.
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByTestId('delete-btn')).toBeDisabled();
    });
    const checkboxesAfter = within(screen.getByTestId('cast-roster')).getAllByRole('checkbox');
    for (const cb of checkboxesAfter) {
      expect(cb).not.toBeChecked();
    }
  });
});
