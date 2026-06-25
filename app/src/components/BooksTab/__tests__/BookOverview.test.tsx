/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within, fireEvent, waitFor, act } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { BookOverview } from '@/components/BooksTab/BookOverview';
import { Toaster } from '@/components/ui/toaster';
import { useBooksStore } from '@/stores/booksStore';

// ─── HTTP boundary mock ───────────────────────────────────────────────────────
// We mock `@/lib/api/client` (the fetch wrapper) — that is the project's HTTP
// boundary, the same surface a real backend would expose. Everything above it
// (hooks, store, component) runs as real code so we assert on real outcomes
// (store state, DOM, request payloads recorded at the boundary), not on spy
// call counts of first-party collaborators.

// Records of calls landing at the apiClient layer. Tests assert against
// these arrays to verify the network contract (URL params + body shape)
// via behavior-shape checks on the captured request payloads — never
// via spy call-count matchers on first-party hook layers.
const mergeCalls: Array<{ bookId: string; charId: string; data: { source_char_id: string } }> = [];
const deleteCalls: Array<{ bookId: string; charId: string }> = [];
const generateCalls: Array<{ bookId: string; chapterId: string; data?: unknown }> = [];

// Per-test overrides for what the mocked HTTP boundary returns.
const apiState: {
  characters: Array<Record<string, unknown>>;
  generateChapterImpl: (() => Promise<{ task_id: string; queued_segments: number }>) | null;
} = {
  characters: [],
  generateChapterImpl: null,
};

const baseBook = {
  id: 'b1',
  title: 'Silo 42',
  author: 'Zev Paiss',
  status: 'analyzed',
  source_format: 'epub',
  chapter_count: 2,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
  chapters: [
    { id: 'c1', number: 1, title: 'Descent', word_count: 3410, generation_state: 'none' },
    { id: 'c2', number: 2, title: 'The Lower Levels', word_count: 4002, generation_state: 'none' },
  ],
};

vi.mock('@/lib/api/client', () => ({
  apiClient: {
    getBook: vi.fn(async (_id: string) => baseBook),
    getCharacters: vi.fn(async (_id: string) => apiState.characters),
    mergeCharacter: vi.fn(async (bookId: string, charId: string, data: { source_char_id: string }) => {
      mergeCalls.push({ bookId, charId, data });
      return { id: charId };
    }),
    deleteCharacter: vi.fn(async (bookId: string, charId: string) => {
      deleteCalls.push({ bookId, charId });
    }),
    generateChapter: vi.fn(async (bookId: string, chapterId: string, data?: unknown) => {
      generateCalls.push({ bookId, chapterId, data });
      if (apiState.generateChapterImpl) {
        return apiState.generateChapterImpl();
      }
      return { task_id: 't1', queued_segments: 2 };
    }),
    getBookEventsUrl: (bookId: string) => `http://test.local/books/${bookId}/events`,
  },
}));

// ─── useBookProgress mock ─────────────────────────────────────────────────────
// SSE is an external boundary (EventSource → backend). We capture the
// handlers passed by BookOverview so tests can drive them synchronously
// without spinning up a real EventSource.
let mockProgressHandlers: Record<string, ((ev: unknown) => void) | undefined> = {};
vi.mock('@/lib/hooks/useBookProgress', () => ({
  useBookProgress: (
    _bookId: string,
    handlers: Record<string, ((ev: unknown) => void) | undefined>,
  ) => {
    mockProgressHandlers = handlers;
  },
}));

// ─── Helpers ──────────────────────────────────────────────────────────────────

const wrap = (ui: React.ReactNode) => (
  // Each test gets its own QueryClient so cached responses from prior tests
  // never leak in.
  <QueryClientProvider client={new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })}>
    {ui}
  </QueryClientProvider>
);

// Default character list (1 narrator + 1 non-narrator)
const defaultCharacters = [
  {
    id: 'n',
    name: 'Narrator',
    is_narrator: true,
    color: '#6d8bff',
    dialogue_count: 0,
    confidence: 1,
    voice_type: 'designed',
    role: undefined,
    aliases: [],
  },
  {
    id: 'm',
    name: 'Mira',
    is_narrator: false,
    role: 'major',
    color: '#34d399',
    dialogue_count: 142,
    confidence: 0.9,
    voice_type: 'designed',
    aliases: [],
  },
];

/** Renders BookOverview and waits for the loading state to clear. */
async function renderOverview() {
  const result = render(wrap(<BookOverview />));
  await screen.findByTestId('book-header');
  return result;
}

describe('BookOverview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset captured network calls
    mergeCalls.length = 0;
    deleteCalls.length = 0;
    generateCalls.length = 0;
    apiState.characters = [...defaultCharacters];
    apiState.generateChapterImpl = null;
    mockProgressHandlers = {};
    // Reset the real store and prime selectedBookId so queries enable
    useBooksStore.getState().reset();
    useBooksStore.getState().setSelectedBookId('b1');
  });

  // ── Book header ──────────────────────────────────────────────────────────
  it('renders book-header with title, status badge, and summary meta', async () => {
    await renderOverview();
    const header = screen.getByTestId('book-header');
    expect(within(header).getByText('Silo 42')).toBeInTheDocument();
    expect(within(header).getByTestId('book-status')).toBeInTheDocument();
    expect(within(header).getByTestId('book-summary')).toBeInTheDocument();
  });

  it('renders the book-wide cast and per-chapter list (from spec)', async () => {
    await renderOverview();
    const cast = await screen.findByTestId('cast-roster');
    await waitFor(() => {
      expect(within(cast).getByText('Narrator')).toBeInTheDocument();
    });
    expect(within(cast).getByText('Mira')).toBeInTheDocument();
    const chapters = screen.getByTestId('chapter-list');
    expect(within(chapters).getByText(/Descent/)).toBeInTheDocument();
    expect(within(chapters).getByText(/The Lower Levels/)).toBeInTheDocument();
    expect(within(chapters).getByText(/3[,.]?410/)).toBeInTheDocument();
  });

  // ── Cast roster ──────────────────────────────────────────────────────────
  it('renders cast-summary with cast-roster and cast-actions', async () => {
    await renderOverview();
    expect(screen.getByTestId('cast-summary')).toBeInTheDocument();
    expect(screen.getByTestId('cast-roster')).toBeInTheDocument();
    expect(screen.getByTestId('cast-actions')).toBeInTheDocument();
    expect(screen.getByTestId('merge-btn')).toBeInTheDocument();
    expect(screen.getByTestId('delete-btn')).toBeInTheDocument();
  });

  it('renders one char-card per character', async () => {
    await renderOverview();
    const roster = await screen.findByTestId('cast-roster');
    await waitFor(() => {
      const cards = within(roster).getAllByTestId(/^char-card/);
      expect(cards).toHaveLength(2);
    });
  });

  it('narrator has no checkbox (not selectable)', async () => {
    await renderOverview();
    const roster = await screen.findByTestId('cast-roster');
    await waitFor(() => {
      // Only Mira (non-narrator) gets a checkbox, not Narrator
      const checkboxes = within(roster).getAllByRole('checkbox');
      expect(checkboxes).toHaveLength(1);
    });
  });

  it('non-narrator character has a selectable checkbox', async () => {
    await renderOverview();
    const roster = await screen.findByTestId('cast-roster');
    await waitFor(() => {
      const checkboxes = within(roster).getAllByRole('checkbox');
      expect(checkboxes).toHaveLength(1);
    });
  });

  // ── Chapter list ─────────────────────────────────────────────────────────
  it('renders chapter-list with word count and generation_state badge', async () => {
    await renderOverview();
    const chapterList = screen.getByTestId('chapter-list');
    expect(within(chapterList).getByText(/Descent/)).toBeInTheDocument();
    expect(within(chapterList).getByText(/The Lower Levels/)).toBeInTheDocument();
    const noneBadges = within(chapterList).getAllByText('none');
    expect(noneBadges).toHaveLength(2);
  });

  it('chapter list has Edit links', async () => {
    await renderOverview();
    const chapterList = screen.getByTestId('chapter-list');
    const editBtns = within(chapterList).getAllByText(/edit/i);
    expect(editBtns.length).toBeGreaterThanOrEqual(2);
  });

  // ── Header action slots ──────────────────────────────────────────────────
  it('renders generate-all-btn, export-btn, and audio-settings-btn', async () => {
    await renderOverview();
    expect(screen.getByTestId('generate-all-btn')).toBeInTheDocument();
    expect(screen.getByTestId('export-btn')).toBeInTheDocument();
    expect(screen.getByTestId('audio-settings-btn')).toBeInTheDocument();
  });

  it('renders per-chapter generate buttons', async () => {
    await renderOverview();
    expect(screen.getByTestId('generate-chapter-1')).toBeInTheDocument();
    expect(screen.getByTestId('generate-chapter-2')).toBeInTheDocument();
  });

  // ── Drill-in navigation ──────────────────────────────────────────────────
  it('clicking a character name navigates to voice-editor for that character', async () => {
    await renderOverview();
    await screen.findByTestId('char-link-m');
    fireEvent.click(screen.getByTestId('char-link-m'));
    // Outcome: real booksStore reflects the navigation (view + selectedCharacterId).
    await waitFor(() => {
      const state = useBooksStore.getState();
      expect(state.view).toBe('voice-editor');
      expect(state.selectedCharacterId).toBe('m');
    });
  });

  it('clicking a chapter Edit navigates to chapter-editor for that chapter', async () => {
    await renderOverview();
    const chapterList = screen.getByTestId('chapter-list');
    const editBtns = within(chapterList).getAllByText(/edit/i);
    fireEvent.click(editBtns[0]);
    // Outcome: real booksStore reflects the navigation (view + selectedChapterId).
    await waitFor(() => {
      const state = useBooksStore.getState();
      expect(state.view).toBe('chapter-editor');
      expect(state.selectedChapterId).toBe('c1');
    });
  });

  it('clicking export-btn navigates to export view', async () => {
    await renderOverview();
    fireEvent.click(screen.getByTestId('export-btn'));
    await waitFor(() => {
      expect(useBooksStore.getState().view).toBe('export');
    });
  });

  // ── Cast merge/delete wiring ─────────────────────────────────────────────
  it('merge-btn is disabled when fewer than 2 characters selected', async () => {
    await renderOverview();
    const mergeBtn = screen.getByTestId('merge-btn');
    expect(mergeBtn).toBeDisabled();
  });

  it('delete-btn is disabled when no character selected', async () => {
    await renderOverview();
    const deleteBtn = screen.getByTestId('delete-btn');
    expect(deleteBtn).toBeDisabled();
  });

  it('merge-btn disabled with only 1 selected', async () => {
    await renderOverview();
    const roster = await screen.findByTestId('cast-roster');
    let checkboxes: HTMLElement[] = [];
    await waitFor(() => {
      checkboxes = within(roster).getAllByRole('checkbox');
      expect(checkboxes.length).toBeGreaterThanOrEqual(1);
    });
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('merge-btn')).toBeDisabled();
  });

  it('merging two non-narrator characters submits the right survivor/source pair and clears the selection', async () => {
    // Override fixture to have 2 non-narrator characters
    apiState.characters = [
      defaultCharacters[0], // Narrator
      defaultCharacters[1], // Mira ('m')
      {
        id: 'j',
        name: 'Juliette',
        is_narrator: false,
        role: 'major',
        color: '#f59e0b',
        dialogue_count: 98,
        confidence: 0.85,
        voice_type: 'designed',
        aliases: [],
      },
    ];

    await renderOverview();
    const roster = await screen.findByTestId('cast-roster');

    let checkboxes: HTMLElement[] = [];
    await waitFor(() => {
      checkboxes = within(roster).getAllByRole('checkbox');
      expect(checkboxes).toHaveLength(2);
    });

    // Select both non-narrator characters
    fireEvent.click(checkboxes[0]); // Mira (id: 'm')
    fireEvent.click(checkboxes[1]); // Juliette (id: 'j')

    // merge-btn should now be enabled — an outcome of the selection state.
    const mergeBtn = screen.getByTestId('merge-btn');
    await waitFor(() => {
      expect(mergeBtn).not.toBeDisabled();
    });

    fireEvent.click(mergeBtn);

    // Outcome 1: the HTTP boundary received exactly one merge request
    // with the first-selected as survivor and second-selected as source.
    await waitFor(() => {
      expect(mergeCalls).toHaveLength(1);
    });
    expect(mergeCalls[0]).toEqual({
      bookId: 'b1',
      charId: 'm',
      data: { source_char_id: 'j' },
    });

    // Outcome 2: post-merge the cast selection clears, so every visible
    // checkbox is back to unchecked (also reflected by merge-btn going
    // disabled again because <2 are selected).
    await waitFor(() => {
      const cbs = within(roster).getAllByRole('checkbox') as HTMLInputElement[];
      for (const cb of cbs) {
        expect(cb).not.toBeChecked();
      }
      expect(screen.getByTestId('merge-btn')).toBeDisabled();
    });
  });

  it('delete-btn enables when exactly 1 non-narrator character is selected', async () => {
    await renderOverview();
    const roster = await screen.findByTestId('cast-roster');
    let checkboxes: HTMLElement[] = [];
    await waitFor(() => {
      checkboxes = within(roster).getAllByRole('checkbox');
      expect(checkboxes.length).toBeGreaterThanOrEqual(1);
    });
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('delete-btn')).not.toBeDisabled();
  });

  it('confirming the delete dialog issues the delete request, clears selection, and closes the dialog', async () => {
    await renderOverview();
    const roster = await screen.findByTestId('cast-roster');
    let checkboxes: HTMLElement[] = [];
    await waitFor(() => {
      checkboxes = within(roster).getAllByRole('checkbox');
      expect(checkboxes.length).toBeGreaterThanOrEqual(1);
    });
    fireEvent.click(checkboxes[0]); // select Mira

    const deleteBtn = screen.getByTestId('delete-btn');
    expect(deleteBtn).not.toBeDisabled();
    fireEvent.click(deleteBtn);

    // Confirmation dialog must appear before we can confirm.
    await screen.findByRole('alertdialog');
    const confirmBtns = screen.getAllByRole('button', { name: /delete/i });
    // The confirm button in the dialog is the last "delete"-labelled button.
    fireEvent.click(confirmBtns[confirmBtns.length - 1]);

    // Outcome 1: a delete request reached the HTTP boundary with the right ids.
    await waitFor(() => {
      expect(deleteCalls).toHaveLength(1);
    });
    expect(deleteCalls[0]).toEqual({ bookId: 'b1', charId: 'm' });

    // Outcome 2: the alert dialog is no longer in the document.
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });

    // Outcome 3: cast selection cleared — checkbox unchecked + delete-btn re-disabled.
    await waitFor(() => {
      const cbs = within(roster).getAllByRole('checkbox') as HTMLInputElement[];
      for (const cb of cbs) {
        expect(cb).not.toBeChecked();
      }
      expect(screen.getByTestId('delete-btn')).toBeDisabled();
    });
  });

  // ── Summary derivation ───────────────────────────────────────────────────
  it('derives summary stats (chapter count, character count) from queries', async () => {
    await renderOverview();
    const summary = screen.getByTestId('book-summary');
    await waitFor(() => {
      expect(within(summary).getByText(/2 chapter/i)).toBeInTheDocument();
      expect(within(summary).getByText(/2 character/i)).toBeInTheDocument();
    });
  });

  // ── Generate chapter wiring (D2) ─────────────────────────────────────────

  it('generate-chapter buttons are rendered for each chapter', async () => {
    await renderOverview();
    expect(screen.getByTestId('generate-chapter-1')).toBeInTheDocument();
    expect(screen.getByTestId('generate-chapter-2')).toBeInTheDocument();
  });

  it('generate-chapter button is enabled when chapter is not generating', async () => {
    await renderOverview();
    const btn1 = screen.getByTestId('generate-chapter-1');
    expect(btn1).not.toBeDisabled();
  });

  it('clicking generate-chapter-1 sends a generate request for that book + chapter', async () => {
    await renderOverview();
    const btn1 = screen.getByTestId('generate-chapter-1');
    fireEvent.click(btn1);
    // Outcome: a single generate request hit the HTTP boundary scoped to
    // bookId 'b1' + chapterId 'c1'. We assert on the captured payload
    // (not on a spy call-count matcher).
    await waitFor(() => {
      expect(generateCalls).toHaveLength(1);
    });
    expect(generateCalls[0]).toMatchObject({ bookId: 'b1', chapterId: 'c1' });
  });

  it('generation_progress event updates chapter row to show "generating n/m"', async () => {
    await renderOverview();
    // Simulate useBookProgress firing a generation_progress event
    act(() => {
      mockProgressHandlers.onGenerationProgress?.({
        type: 'generation_progress',
        chapter_id: 'c1',
        completed: 1,
        errors: 0,
        total: 3,
        overall_progress: 0.33,
      });
    });
    await waitFor(() => {
      const chapterList = screen.getByTestId('chapter-list');
      expect(within(chapterList).getByText(/generating 1\/3/i)).toBeInTheDocument();
    });
  });

  it('generation_complete event flips chapter row to done badge', async () => {
    await renderOverview();
    // First simulate a progress event to get into generating state
    act(() => {
      mockProgressHandlers.onGenerationProgress?.({
        type: 'generation_progress',
        chapter_id: 'c1',
        completed: 2,
        errors: 0,
        total: 2,
        overall_progress: 1.0,
      });
    });
    // Then complete
    act(() => {
      mockProgressHandlers.onGenerationComplete?.({
        type: 'generation_complete',
        chapter_id: 'c1',
      });
    });
    await waitFor(() => {
      const chapterList = screen.getByTestId('chapter-list');
      expect(within(chapterList).getByText('done')).toBeInTheDocument();
    });
  });

  it('generate-chapter button is disabled while that chapter is generating', async () => {
    // Keep the generate mutation pending so the chapter stays in the in-flight
    // set (the finally-clause that re-enables the button never runs).
    apiState.generateChapterImpl = () => new Promise(() => {});
    await renderOverview();
    const btn1 = screen.getByTestId('generate-chapter-1');
    expect(btn1).not.toBeDisabled();
    // Click to trigger generation — the row is marked in-flight synchronously.
    fireEvent.click(btn1);
    // While the mutation is pending the button must be disabled.
    await waitFor(() => {
      expect(btn1).toBeDisabled();
    });
  });

  // ── Selection toggle off (deselect path) ─────────────────────────────────
  it('clicking a selected character checkbox a second time deselects it', async () => {
    await renderOverview();
    const roster = await screen.findByTestId('cast-roster');
    let checkboxes: HTMLInputElement[] = [];
    await waitFor(() => {
      checkboxes = within(roster).getAllByRole('checkbox') as HTMLInputElement[];
      expect(checkboxes.length).toBeGreaterThanOrEqual(1);
    });
    // First click: selects → delete-btn enables.
    fireEvent.click(checkboxes[0]);
    expect(screen.getByTestId('delete-btn')).not.toBeDisabled();
    // Second click: deselects → delete-btn disabled again, checkbox unchecked.
    fireEvent.click(checkboxes[0]);
    await waitFor(() => {
      expect(screen.getByTestId('delete-btn')).toBeDisabled();
    });
    const cbs = within(roster).getAllByRole('checkbox') as HTMLInputElement[];
    for (const cb of cbs) {
      expect(cb).not.toBeChecked();
    }
  });

  // ── Confidence label branches (high/medium/low) ──────────────────────────
  it('renders a "low" confidence badge for a character with score < 0.5', async () => {
    apiState.characters = [
      defaultCharacters[0], // Narrator
      {
        ...defaultCharacters[1],
        id: 'lowc',
        name: 'Whisper',
        confidence: 0.1,
      },
    ];
    await renderOverview();
    const card = await screen.findByTestId('char-card-lowc');
    // Outcome: visible "low" confidence badge rendered for the character.
    expect(within(card).getByText('low')).toBeInTheDocument();
  });

  it('renders a "medium" confidence badge for a character with 0.5 <= score < 0.8', async () => {
    apiState.characters = [
      defaultCharacters[0],
      {
        ...defaultCharacters[1],
        id: 'medc',
        name: 'Echo',
        confidence: 0.6,
      },
    ];
    await renderOverview();
    const card = await screen.findByTestId('char-card-medc');
    expect(within(card).getByText('medium')).toBeInTheDocument();
  });

  // ── Voice assignment affordance ──────────────────────────────────────────
  it('renders an "assign voice" CTA when a non-narrator character has no voice', async () => {
    apiState.characters = [
      defaultCharacters[0],
      {
        ...defaultCharacters[1],
        id: 'nov',
        name: 'Silent',
        voice_type: undefined,
        voice_label: undefined,
      },
    ];
    await renderOverview();
    const assignBtn = await screen.findByTestId('assign-voice-nov');
    // Outcome: button shows the "Assign voice" CTA label rather than a voice name.
    expect(within(assignBtn).getByText(/assign voice/i)).toBeInTheDocument();
  });

  it('renders the assigned voice label when the character has a voice_label', async () => {
    apiState.characters = [
      defaultCharacters[0],
      {
        ...defaultCharacters[1],
        id: 'voicedc',
        name: 'Spoken',
        voice_label: 'Brian (warm)',
        voice_type: 'designed',
      },
    ];
    await renderOverview();
    const assignBtn = await screen.findByTestId('assign-voice-voicedc');
    expect(within(assignBtn).getByText('Brian (warm)')).toBeInTheDocument();
  });

  it('clicking the assign-voice button navigates to voice-editor for that character', async () => {
    await renderOverview();
    const assignBtn = await screen.findByTestId('assign-voice-m');
    fireEvent.click(assignBtn);
    await waitFor(() => {
      const state = useBooksStore.getState();
      expect(state.view).toBe('voice-editor');
      expect(state.selectedCharacterId).toBe('m');
    });
  });

  // ── No-book null render path ─────────────────────────────────────────────
  it('renders nothing when the book query returns null/undefined', async () => {
    // Clear the primed selectedBookId; useBook(null) returns {data:undefined,isLoading:false}
    useBooksStore.getState().setSelectedBookId(null);
    const { container } = render(wrap(<BookOverview />));
    // Outcome: no book-header is ever rendered (component returned null).
    await waitFor(() => {
      expect(screen.queryByTestId('book-header')).not.toBeInTheDocument();
    });
    // And the QueryClientProvider rendered no DOM content (BookOverview returned null).
    expect(container.textContent).toBe('');
  });

  // ── Generate error paths (toast surfaces) ────────────────────────────────
  it('shows an "already generating" toast when the generate request fails with 409', async () => {
    apiState.generateChapterImpl = async () => {
      throw new Error('HTTP error! status: 409');
    };
    render(
      wrap(
        <>
          <BookOverview />
          <Toaster />
        </>,
      ),
    );
    await screen.findByTestId('book-header');
    fireEvent.click(screen.getByTestId('generate-chapter-1'));

    // Outcome 1: a toast with the "already generating" title appears in the DOM.
    await waitFor(() => {
      expect(screen.getByText(/already generating/i)).toBeInTheDocument();
    });
    // Outcome 2: the button re-enables (in-flight set cleared by the error path).
    await waitFor(() => {
      expect(screen.getByTestId('generate-chapter-1')).not.toBeDisabled();
    });
  });

  it('shows a generic "generate failed" toast when the generate request fails with a non-409 error', async () => {
    apiState.generateChapterImpl = async () => {
      throw new Error('HTTP error! status: 500');
    };
    render(
      wrap(
        <>
          <BookOverview />
          <Toaster />
        </>,
      ),
    );
    await screen.findByTestId('book-header');
    fireEvent.click(screen.getByTestId('generate-chapter-1'));

    // Outcome: a toast with the failure title + the underlying error message appears.
    await waitFor(() => {
      expect(screen.getByText(/generate failed/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/status:\s*500/)).toBeInTheDocument();
  });

  // ── SSE + chapter row state derivations ──────────────────────────────────
  it('shows a play indicator when a chapter receives generation_complete', async () => {
    await renderOverview();
    act(() => {
      mockProgressHandlers.onGenerationComplete?.({
        type: 'generation_complete',
        chapter_id: 'c1',
      });
    });
    await waitFor(() => {
      // The aria-label exposes a "play" affordance for the first chapter row.
      expect(screen.getByLabelText('play-chapter-1')).toBeInTheDocument();
    });
  });

  it('shows a retry control when generation_progress reports errors > 0', async () => {
    await renderOverview();
    act(() => {
      mockProgressHandlers.onGenerationProgress?.({
        type: 'generation_progress',
        chapter_id: 'c1',
        completed: 1,
        errors: 2,
        total: 3,
        overall_progress: 0.33,
      });
    });
    await waitFor(() => {
      expect(screen.getByTestId('retry-chapter-1')).toBeInTheDocument();
    });
    // Clicking retry issues another generate request for that chapter.
    fireEvent.click(screen.getByTestId('retry-chapter-1'));
    await waitFor(() => {
      expect(generateCalls.length).toBeGreaterThanOrEqual(1);
    });
    expect(generateCalls[generateCalls.length - 1]).toMatchObject({
      bookId: 'b1',
      chapterId: 'c1',
    });
  });
});
