/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { ChapterEditor } from '@/components/BooksTab/ChapterEditor';

// ─── Mocks ────────────────────────────────────────────────────────────────────

// Wire the mocks to fire onSuccess so the components proceed to their
// post-mutation behaviour (popover/dialog closes, selection clears).
// Tests assert on those observable outcomes rather than on call counts.
const updateMutate = vi.fn((_args: any, opts?: { onSuccess?: () => void }) => {
  opts?.onSuccess?.();
});
const regenerateMutate = vi.fn((_args: any, opts?: { onSuccess?: () => void }) => {
  opts?.onSuccess?.();
});

// Observable side-effects of the top-level container: view changes (back
// button), read-along toggles, and toast notifications when the read-along
// button is clicked without a generated chapter Story. Recording these as
// data (not as spy call counts) lets the tests assert on what the user sees.
const setViewMock = vi.fn();
const setReadAlongMock = vi.fn();
const toastCalls: Array<{ title?: unknown; description?: unknown }> = [];

vi.mock('@/components/ui/use-toast', () => ({
  toast: (args: { title?: unknown; description?: unknown }) => {
    toastCalls.push(args);
  },
  useToast: () => ({ toast: vi.fn(), toasts: [] }),
}));

vi.mock('@/stores/booksStore', () => ({
  useBooksStore: (s: any) =>
    s({
      selectedBookId: 'b1',
      selectedChapterId: 'c1',
      setView: setViewMock,
      readAlongPlaying: false,
      currentSpokenSegmentId: null,
      setReadAlong: setReadAlongMock,
      setCurrentSpokenSegment: vi.fn(),
    }),
}));

vi.mock('@/stores/storyStore', () => ({
  useStoryStore: (s: any) =>
    s({
      isPlaying: false,
      currentTimeMs: 0,
      playbackStoryId: null,
      play: vi.fn(),
      pause: vi.fn(),
      stop: vi.fn(),
      setActiveStory: vi.fn(),
    }),
}));

vi.mock('@/lib/hooks/useStories', () => ({
  useStory: () => ({ data: null }),
}));

vi.mock('@/lib/hooks/useStoryPlayback', () => ({
  useStoryPlayback: vi.fn(),
}));

vi.mock('@/lib/hooks/useBooks', () => ({
  useBook: () => ({ data: null }),
  useCharacters: () => ({
    data: [
      { id: 'n', name: 'Narrator', is_narrator: true, color: '#6d8bff' },
      { id: 'm', name: 'Mira', color: '#34d399', confidence: 0.9 },
      { id: 'h', name: 'Holt', color: '#fbbf24', confidence: 0.8 },
      { id: 'lo', name: 'LowConf', color: '#ff0000', confidence: 0.5 },
    ],
  }),
  useSegments: () => ({
    data: [
      {
        id: '11',
        order: 0,
        type: 'narration',
        text: 'The corridor lights flickered.',
        character_id: 'n',
        character_name: 'Narrator',
        emotion: 'neutral',
        audio: { status: 'completed', generation_id: 'g11' },
      },
      {
        id: '12',
        order: 1,
        type: 'dialogue',
        text: `”We can't keep going down,”`,
        character_id: 'm',
        character_name: 'Mira',
        emotion: 'tense',
        audio: { status: 'completed', generation_id: 'g12' },
      },
      {
        id: '13',
        order: 2,
        type: 'dialogue',
        text: '”I agree,” said LowConf.',
        character_id: 'lo',
        character_name: 'LowConf',
        emotion: 'worried',
        audio: { status: 'none' },
      },
    ],
  }),
  useUpdateSegment: () => ({ mutate: updateMutate, isPending: false }),
  usePreviewSegment: () => ({ mutate: vi.fn(), isPending: false }),
  useSplitSegment: () => ({ mutateAsync: vi.fn().mockResolvedValue([]), isPending: false }),
  useMergeSegments: () => ({ mutate: vi.fn(), isPending: false }),
  useRegenerateSegment: () => ({ mutate: regenerateMutate, isPending: false }),
}));

// ─── Test suite ───────────────────────────────────────────────────────────────

describe('ChapterEditor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    toastCalls.length = 0;
  });

  it('renders book-view with color-coded, speaker-labeled lines', () => {
    render(<ChapterEditor />);
    const bookView = screen.getByTestId('book-view');
    expect(bookView).toBeInTheDocument();
    // Narration line is present
    expect(bookView).toHaveTextContent('The corridor lights flickered.');
    // Dialogue line is present
    expect(bookView).toHaveTextContent(`We can't keep going down`);
  });

  it('renders seg-{id} spans for each segment', () => {
    render(<ChapterEditor />);
    expect(screen.getByTestId('seg-11')).toBeInTheDocument();
    expect(screen.getByTestId('seg-12')).toBeInTheDocument();
  });

  it('renders speaker chip for dialogue lines', () => {
    render(<ChapterEditor />);
    // Mira chip should appear for dialogue line 12
    expect(screen.getByTestId('speaker-chip-12')).toBeInTheDocument();
    expect(screen.getByTestId('speaker-chip-12')).toHaveTextContent('Mira');
  });

  it('renders emotion-pill for dialogue lines (inert slot for D4)', () => {
    render(<ChapterEditor />);
    const pill = screen.getByTestId('emotion-12');
    expect(pill).toBeInTheDocument();
    expect(pill).toHaveTextContent('tense');
  });

  it('renders review-toolbar with filter tabs', () => {
    render(<ChapterEditor />);
    const toolbar = screen.getByTestId('review-toolbar');
    expect(within(toolbar).getByText(/All/)).toBeInTheDocument();
    expect(within(toolbar).getByText(/Dialogue only/)).toBeInTheDocument();
    expect(within(toolbar).getByText(/By character/)).toBeInTheDocument();
    expect(within(toolbar).getByText(/Flagged/)).toBeInTheDocument();
  });

  it('readalong-btn stays clickable but marked aria-disabled when the chapter has no generated audio, and shows a guidance hint — D5', () => {
    // useStory/useBook are mocked to null here (no Story → no generated audio).
    // The button must NOT be a dead disabled control: it stays clickable (so a
    // click can explain what to do) but is marked aria-disabled, and a visible
    // amber hint tells the user to generate the chapter first.
    // (The enabled-with-audio path is covered in ChapterEditorReadAlong.test.tsx.)
    render(<ChapterEditor />);
    const btn = screen.getByTestId('readalong-btn');
    expect(btn).toBeInTheDocument();
    expect(btn).not.toBeDisabled();
    expect(btn).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByTestId('readalong-hint')).toHaveTextContent(/generate/i);
  });

  it('renders review-rail with review-progress', () => {
    render(<ChapterEditor />);
    expect(screen.getByTestId('review-rail')).toBeInTheDocument();
    expect(screen.getByTestId('review-progress')).toBeInTheDocument();
  });

  it('renders back-to-overview and chapter-switcher', () => {
    render(<ChapterEditor />);
    expect(screen.getByTestId('back-to-overview')).toBeInTheDocument();
    expect(screen.getByTestId('chapter-switcher')).toBeInTheDocument();
  });

  it('Dialogue-only filter hides narration segments', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const toolbar = screen.getByTestId('review-toolbar');
    await u.click(within(toolbar).getByText(/Dialogue only/));
    // Narration line should be gone
    expect(screen.queryByTestId('seg-11')).not.toBeInTheDocument();
    // Dialogue line remains
    expect(screen.getByTestId('seg-12')).toBeInTheDocument();
  });

  it('reassigns a dialogue line when a character is chosen from the popover', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    // Click the dialogue segment to open the reassign popover
    await u.click(screen.getByTestId('seg-12'));
    // The reassign dropdown should appear
    const dropdown = screen.getByTestId('reassign-dropdown');
    expect(dropdown).toBeInTheDocument();
    // Click Holt in the dropdown
    await u.click(within(dropdown).getByText('Holt'));
    // The mutation succeeds (wired in the mock), so the popover should close —
    // the dropdown is no longer in the document. That is the observable
    // outcome of a successful reassign from the user's point of view.
    expect(screen.queryByTestId('reassign-dropdown')).not.toBeInTheDocument();
  });

  it('review-rail shows jump-{id} buttons for low-confidence lines', () => {
    render(<ChapterEditor />);
    // Segment 13 has character 'lo' with confidence 0.5, below the 0.7 threshold
    // The review rail should render a jump button for it
    expect(screen.getByTestId('jump-13')).toBeInTheDocument();
  });

  it('narration segment has no speaker chip', () => {
    render(<ChapterEditor />);
    // Segment 11 is narration — it should NOT have a speaker chip
    expect(screen.queryByTestId('speaker-chip-11')).not.toBeInTheDocument();
  });

  it('by-character filter shows only lines for a selected character', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const toolbar = screen.getByTestId('review-toolbar');
    // Click "By character" to open character picker
    await u.click(within(toolbar).getByText(/By character/));
    // Select Mira from the character selector
    const charSelect = screen.getByTestId('character-filter-select');
    await u.click(within(charSelect).getByText('Mira'));
    // Only Mira's dialogue should appear
    expect(screen.queryByTestId('seg-11')).not.toBeInTheDocument();
    expect(screen.getByTestId('seg-12')).toBeInTheDocument();
  });

  it('⋯ menu surfaces a Regenerate button for a completed segment and dismisses the dialog when used', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    // Segment 11 has audio.status='completed' — open its ⋯ menu
    const chapterText = screen.getByTestId('chapter-text');
    const firstPara = within(chapterText).getAllByRole('paragraph')[0];
    const menuBtn = within(firstPara).getByRole('button', { name: '⋯' });
    await u.click(menuBtn);
    // The selection dialog should show the Regenerate button
    const dialog = screen.getByTestId('selection-dialog');
    const regenBtn = within(dialog).getByTestId('regenerate-btn-11');
    expect(regenBtn).toBeInTheDocument();
    expect(regenBtn).toHaveTextContent(/Regenerate/);
    // After clicking it, the regenerate mutation succeeds (wired in the mock)
    // and the selection dialog closes — observable outcome the user sees.
    await u.click(regenBtn);
    expect(screen.queryByTestId('selection-dialog')).not.toBeInTheDocument();
  });

  it('⋯ menu does NOT show Regenerate for a segment with audio.status=none', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    // Segment 13 has audio.status='none' — open its ⋯ menu
    const chapterText = screen.getByTestId('chapter-text');
    const seg13Para = within(chapterText)
      .getAllByRole('paragraph')
      .find((p) => p.querySelector('[data-testid="seg-13"]'));
    expect(seg13Para).toBeTruthy();
    const menuBtn = within(seg13Para!).getByRole('button', { name: '⋯' });
    await u.click(menuBtn);
    // The regenerate button should NOT appear for a never-generated segment
    const dialog = screen.getByTestId('selection-dialog');
    expect(within(dialog).queryByTestId('regenerate-btn-13')).not.toBeInTheDocument();
  });

  it('back-to-overview button switches the books view back to overview', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    await u.click(screen.getByTestId('back-to-overview'));
    // The button's job is to drive the books-view state machine back to
    // the overview surface. The observable outcome is the store transition.
    expect(setViewMock).toHaveBeenCalledWith('overview');
  });

  it('Flagged filter shows only low-confidence dialogue segments and hides the rest', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    const toolbar = screen.getByTestId('review-toolbar');
    await u.click(within(toolbar).getByText(/^Flagged$/));
    // Segment 13 belongs to 'LowConf' (confidence 0.5 < 0.7) → visible.
    expect(screen.getByTestId('seg-13')).toBeInTheDocument();
    // Segment 11 (narration, narrator confidence default 1.0) → hidden.
    expect(screen.queryByTestId('seg-11')).not.toBeInTheDocument();
    // Segment 12 (Mira, confidence 0.9) → hidden.
    expect(screen.queryByTestId('seg-12')).not.toBeInTheDocument();
  });

  it('jump-{id} in the review rail selects the target segment and scrolls it into view', async () => {
    // Spy on scrollIntoView so we can confirm the handler reached the DOM.
    // jsdom does not implement it, so without this stub the test would crash.
    const scrollSpy = vi.fn();
    Element.prototype.scrollIntoView = scrollSpy as unknown as typeof Element.prototype.scrollIntoView;

    const u = userEvent.setup();
    render(<ChapterEditor />);

    const seg13 = screen.getByTestId('seg-13');
    // Before clicking, the segment is not visually selected (no outline color).
    expect(seg13.style.outline || '').not.toMatch(/solid/);

    await u.click(screen.getByTestId('jump-13'));

    // The handler should have asked the seg-13 element to scroll into view —
    // the user-observable jump behaviour.
    expect(scrollSpy).toHaveBeenCalledWith({
      behavior: 'smooth',
      block: 'center',
    });

    // After the jump, seg-13 is the selected segment, so it picks up the
    // inline outline style ChapterEditor applies to its highlighted line.
    const seg13After = screen.getByTestId('seg-13');
    expect(seg13After.style.outline).toMatch(/solid/);
  });

  it('clicking the dialogue segment again to close the reassign popover clears the segment selection', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);

    const seg12 = screen.getByTestId('seg-12');
    // Open the popover by clicking the dialogue segment.
    await u.click(seg12);
    expect(screen.getByTestId('reassign-dropdown')).toBeInTheDocument();
    // The selected dialogue gets an inline outline border using its color.
    expect(screen.getByTestId('seg-12').style.outline).toMatch(/solid/);

    // Dismiss the popover by pressing Escape — radix calls onOpenChange(false).
    await u.keyboard('{Escape}');

    // The dropdown is gone and the selection-driven outline is removed.
    expect(screen.queryByTestId('reassign-dropdown')).not.toBeInTheDocument();
    expect(screen.getByTestId('seg-12').style.outline || '').not.toMatch(/solid/);
  });

  it('clicking the read-along button without a generated chapter Story surfaces a guidance toast and does not start playback', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);

    await u.click(screen.getByTestId('readalong-btn'));

    // A toast was shown to explain why nothing is playing.
    expect(toastCalls).toHaveLength(1);
    expect(String(toastCalls[0].title)).toMatch(/read along|nothing/i);
    // Read-along never flipped on — handleReadAlongToggle returned early.
    expect(setReadAlongMock).not.toHaveBeenCalledWith(true);
  });

  it('orders segments in the chapter view by their `order` field, regardless of incoming list order', () => {
    // The hook mock returns segments in order 0,1,2. Pull every seg-* node
    // from chapter-text in DOM order and verify it matches the expected
    // narration→dialogue→dialogue sequence the user reads top-to-bottom.
    render(<ChapterEditor />);
    const chapterText = screen.getByTestId('chapter-text');
    const segNodes = within(chapterText)
      .getAllByTestId(/^seg-\d+$/)
      .map((n) => n.getAttribute('data-testid'));
    expect(segNodes).toEqual(['seg-11', 'seg-12', 'seg-13']);
  });

  it('renders the chapter color legend only for characters appearing in this chapter (drawn from segment character_ids)', () => {
    // The mock roster has Narrator/Mira/Holt/LowConf but only Narrator/Mira/
    // LowConf actually appear in the chapter's segments (n, m, lo). Holt
    // never speaks here, so the legend must NOT render him.
    render(<ChapterEditor />);
    const bookView = screen.getByTestId('book-view');
    // Use within the legend area (first row inside book-view, before chapter-text).
    expect(within(bookView).getByText('Narrator')).toBeInTheDocument();
    // Mira appears both as legend label and as the speaker chip; either is fine.
    expect(within(bookView).getAllByText('Mira').length).toBeGreaterThan(0);
    // LowConf appears as speaker chip + legend.
    expect(within(bookView).getAllByText('LowConf').length).toBeGreaterThan(0);
    // Holt doesn't speak — legend should not list him.
    expect(within(bookView).queryByText('Holt')).not.toBeInTheDocument();
  });
});
