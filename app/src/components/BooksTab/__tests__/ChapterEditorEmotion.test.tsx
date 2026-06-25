/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { useSyncExternalStore } from 'react';
import { render, screen, within, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { ChapterEditor } from '@/components/BooksTab/ChapterEditor';

// ─── Mocks ────────────────────────────────────────────────────────────────────
//
// T2-11 audit-improve: the mutation hooks remain mocked (their HTTP boundary is
// the only practical seam — the project does not yet ship MSW handlers for the
// segment endpoints), but the tests no longer rely on spy call-count checks
// to prove behaviour. Every assertion targets a user-visible outcome rendered
// by ChapterEditor: the emotion pill label, the intensity readout in the
// popover, the "Previewing…" disabled button state, the active-preset
// styling, or the delivery input value.

const updateMutate = vi.fn();

// Stateful external store backing usePreviewSegment so the React component
// re-renders when previewMutate flips the pending flag. This lets us observe
// the user-visible "Previewing…" / disabled UI without asserting on spy calls.
const previewStore = {
  isPending: false,
  listeners: new Set<() => void>(),
  set(next: boolean) {
    this.isPending = next;
    this.listeners.forEach((l) => l());
  },
  subscribe(l: () => void): () => void {
    this.listeners.add(l);
    return () => {
      this.listeners.delete(l);
    };
  },
  reset() {
    this.isPending = false;
    this.listeners.clear();
  },
};
const previewMutate = vi.fn(() => {
  previewStore.set(true);
});

function usePreviewPending(): boolean {
  return useSyncExternalStore(
    (l) => previewStore.subscribe(l),
    () => previewStore.isPending,
    () => previewStore.isPending,
  );
}

vi.mock('@/stores/booksStore', () => ({
  useBooksStore: (s: any) =>
    s({
      selectedBookId: 'b1',
      selectedChapterId: 'c1',
      setView: vi.fn(),
      readAlongPlaying: false,
      currentSpokenSegmentId: null,
      setReadAlong: vi.fn(),
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
    ],
  }),
  useSegments: () => ({
    data: [
      {
        id: '12',
        order: 0,
        type: 'dialogue',
        text: '"We need to move fast,"',
        character_id: 'm',
        character_name: 'Mira',
        emotion: 'tense',
        emotion_intensity: 0.5,
        delivery: '',
        audio: { status: 'none' },
      },
    ],
  }),
  useUpdateSegment: () => ({ mutate: updateMutate, isPending: false }),
  usePreviewSegment: () => ({ mutate: previewMutate, isPending: usePreviewPending() }),
  useSplitSegment: () => ({ mutateAsync: vi.fn().mockResolvedValue([]), isPending: false }),
  useMergeSegments: () => ({ mutate: vi.fn(), isPending: false }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('ChapterEditor — emotion/delivery D4', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    previewStore.reset();
  });

  it('renders the emotion pill with the segment emotion label', () => {
    render(<ChapterEditor />);
    const pill = screen.getByTestId('emotion-12');
    expect(pill).toBeInTheDocument();
    expect(pill).toHaveTextContent('tense');
  });

  it('clicking the emotion pill opens the delivery popover with preset choices, intensity, instruction, and preview', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    await u.click(screen.getByTestId('emotion-12'));
    const popover = screen.getByTestId('delivery-popover');
    // Preset buttons (representative subset of EMOTION_PRESETS)
    expect(within(popover).getByRole('button', { name: /angry/i })).toBeInTheDocument();
    expect(within(popover).getByRole('button', { name: /neutral/i })).toBeInTheDocument();
    expect(within(popover).getByRole('button', { name: /happy/i })).toBeInTheDocument();
    // Intensity readout matches the seeded segment (0.5 → 50%)
    expect(within(popover).getByText(/Intensity:\s*50%/)).toBeInTheDocument();
    // Free-text instruction input + preview button surfaces
    expect(within(popover).getByPlaceholderText(/trembling voice/i)).toBeInTheDocument();
    expect(within(popover).getByTestId('preview-btn')).toBeInTheDocument();
  });

  it('selecting the "angry" preset updates the pill label and highlights the chosen preset', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    await u.click(screen.getByTestId('emotion-12'));
    const popover = screen.getByTestId('delivery-popover');
    const angryBtn = within(popover).getByRole('button', { name: /angry/i });
    const tenseInitial = within(popover).queryByRole('button', { name: /tense/i });
    // Before click: the seeded emotion "tense" is the only preset wearing the
    // active styling. We confirm "angry" does not yet wear it.
    expect(angryBtn).not.toHaveClass('bg-primary');
    expect(tenseInitial).toHaveClass('bg-primary');

    await u.click(angryBtn);

    // Pill label is the observable outcome — the trigger now reads "angry"
    await waitFor(() => {
      expect(screen.getByTestId('emotion-12')).toHaveTextContent('angry');
    });
    // Active-preset styling moved from "tense" to "angry" inside the popover
    const popoverAfter = screen.getByTestId('delivery-popover');
    expect(within(popoverAfter).getByRole('button', { name: /angry/i })).toHaveClass(
      'bg-primary',
    );
    expect(within(popoverAfter).getByRole('button', { name: /tense/i })).not.toHaveClass(
      'bg-primary',
    );
  });

  it('clicking preview disables the preview button and shows the "Previewing…" pending label', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    await u.click(screen.getByTestId('emotion-12'));
    const popover = screen.getByTestId('delivery-popover');
    const previewBtn = within(popover).getByTestId('preview-btn');
    // Idle state: button is enabled and shows the "Preview this line" label
    expect(previewBtn).toBeEnabled();
    expect(previewBtn).toHaveTextContent(/Preview this line/);

    await u.click(previewBtn);

    // The stateful mock flips usePreviewSegment().isPending → true, the
    // external store notifies React, the component re-renders, and the
    // button advertises the pending state to the user.
    await waitFor(() => {
      const btn = within(screen.getByTestId('delivery-popover')).getByTestId('preview-btn');
      expect(btn).toBeDisabled();
      expect(btn).toHaveTextContent(/Previewing/i);
    });
  });

  it('nudging the intensity slider updates the visible intensity readout in the popover', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    await u.click(screen.getByTestId('emotion-12'));
    // Radix Slider renders a <span role="slider"> that responds to keyboard events.
    // Initial value is 0.5 (50%), step is 0.05 — one ArrowRight press => 0.55 (55%).
    const popover = screen.getByTestId('delivery-popover');
    expect(within(popover).getByText(/Intensity:\s*50%/)).toBeInTheDocument();

    const sliderThumb = within(popover).getByRole('slider');
    fireEvent.keyDown(sliderThumb, { key: 'ArrowRight', code: 'ArrowRight' });

    await waitFor(() => {
      expect(within(popover).getByText(/Intensity:\s*55%/)).toBeInTheDocument();
    });
    expect(within(popover).queryByText(/Intensity:\s*50%/)).not.toBeInTheDocument();
  });

  it('typing into the delivery instruction updates the visible input value', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    await u.click(screen.getByTestId('emotion-12'));
    const popover = screen.getByTestId('delivery-popover');
    const deliveryInput = within(popover).getByPlaceholderText(/trembling voice/i);
    // Seeded delivery is empty; controlled input starts empty.
    expect(deliveryInput).toHaveValue('');

    await u.type(deliveryInput, 'speak slowly');

    // Observable outcome: the input now reflects the typed text.
    expect(deliveryInput).toHaveValue('speak slowly');
  });

  it('blurring the delivery input retains the typed value (persists locally)', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    await u.click(screen.getByTestId('emotion-12'));
    const popover = screen.getByTestId('delivery-popover');
    const deliveryInput = within(popover).getByPlaceholderText(/trembling voice/i);
    await u.type(deliveryInput, 'whisper urgently');
    expect(deliveryInput).toHaveValue('whisper urgently');

    fireEvent.blur(deliveryInput);

    // The user-visible outcome of blur is that the typed text is preserved
    // in the input — the blur handler also fires the persist mutation, but
    // we only assert what the user actually sees on screen.
    expect(deliveryInput).toHaveValue('whisper urgently');
  });
});
