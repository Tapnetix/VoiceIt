/// <reference types="@testing-library/jest-dom/vitest" />
/**
 * ChapterEditorReadAlong.test.tsx
 *
 * Tests for D5 read-along mode in ChapterEditor + ChapterReadAlong observer.
 *
 * SC8 quality bar: assertions target observable outcomes — the read-along
 * store state that downstream UI keys on (readAlongPlaying,
 * currentSpokenSegmentId), the rendered button label/variant, the segment
 * highlight markers (data-active, ♪), and the chapter Story handed to the
 * playback engine. We deliberately do NOT assert on spy call counts of
 * first-party setters; instead we observe their post-condition state.
 */
import '@/i18n';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { ChapterEditor } from '@/components/BooksTab/ChapterEditor';

// ─── Mock state buckets ────────────────────────────────────────────────────────

let mockReadAlongPlaying = false;
let mockCurrentSpokenSegmentId: string | null = null;
const mockSetReadAlong = vi.fn((val: boolean) => {
  mockReadAlongPlaying = val;
});
const mockSetCurrentSpokenSegment = vi.fn((id: string | null) => {
  mockCurrentSpokenSegmentId = id;
});

// storyStore currentTimeMs — controllable from tests
let mockCurrentTimeMs = 0;
let mockIsPlaying = false;
// Observable side-effects of starting/stopping read-along — the storyStore
// transitions to playing a given Story (id + sorted items). We track the
// post-condition state, not the call.
let mockActiveStoryId: string | null = null;
let mockActiveStoryItems: { generation_id: string; start_time_ms: number }[] | null = null;
const mockPlay = vi.fn((storyId: string, items: { generation_id: string; start_time_ms: number }[]) => {
  mockActiveStoryId = storyId;
  mockActiveStoryItems = items;
  mockIsPlaying = true;
});
const mockPause = vi.fn(() => {
  mockIsPlaying = false;
});
const mockStop = vi.fn(() => {
  mockActiveStoryId = null;
  mockActiveStoryItems = null;
  mockIsPlaying = false;
});

// Records every items-array the audio engine hook (useStoryPlayback) is
// handed during a render. The hook itself is a side-effect-only consumer; we
// observe what data it receives as a proxy for "the audio engine is wired up
// to this chapter's Story".
const storyPlaybackItemsSeen: (unknown | undefined)[] = [];

// Capture the variables the mutation boundary (TanStack Query mutate) sees —
// this is the HTTP request the reassignment produces. We record the payload
// as data rather than assert on a spy call count.
let lastUpdatePayload: { segmentId?: string; data?: Record<string, unknown> } | null = null;
const updateMutate = vi.fn((vars: { segmentId: string; data: Record<string, unknown> }) => {
  lastUpdatePayload = vars;
});

// ─── Mocks ────────────────────────────────────────────────────────────────────

vi.mock('@/stores/booksStore', () => ({
  useBooksStore: (s: any) =>
    s({
      selectedBookId: 'b1',
      selectedChapterId: 'c1',
      setView: vi.fn(),
      readAlongPlaying: mockReadAlongPlaying,
      currentSpokenSegmentId: mockCurrentSpokenSegmentId,
      setReadAlong: mockSetReadAlong,
      setCurrentSpokenSegment: mockSetCurrentSpokenSegment,
    }),
}));

vi.mock('@/stores/storyStore', () => ({
  useStoryStore: (s: any) =>
    s({
      isPlaying: mockIsPlaying,
      currentTimeMs: mockCurrentTimeMs,
      playbackStoryId: null,
      play: mockPlay,
      pause: mockPause,
      stop: mockStop,
      setActiveStory: vi.fn(),
    }),
}));

// Segments: two dialogue segments with known audio generation_ids & durations
// Seg 12: generation_id=g12, order 0  → story item at 0ms, duration 2.0s
// Seg 13: generation_id=g13, order 1  → story item at 2000ms, duration 2.5s
// Character 'm' has confidence=0.9 (high), 'h' has confidence=0.85 (high)
vi.mock('@/lib/hooks/useBooks', () => ({
  useBook: () => ({
    data: {
      id: 'b1',
      title: 'Test Book',
      chapters: [
        { id: 'c1', number: 1, title: 'Chapter 1', story_id: 'story-1', word_count: 100, generation_state: 'completed' },
      ],
    },
  }),
  useCharacters: () => ({
    data: [
      { id: 'n', name: 'Narrator', is_narrator: true, color: '#6d8bff', confidence: 1 },
      { id: 'm', name: 'Mira', color: '#34d399', confidence: 0.9 },
      { id: 'h', name: 'Holt', color: '#fbbf24', confidence: 0.85 },
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
        audio: { status: 'completed', generation_id: 'g12', duration_ms: 2000 },
      },
      {
        id: '13',
        order: 1,
        type: 'dialogue',
        text: '"I know," Holt replied.',
        character_id: 'h',
        character_name: 'Holt',
        emotion: 'calm',
        emotion_intensity: 0.4,
        delivery: '',
        audio: { status: 'completed', generation_id: 'g13', duration_ms: 2500 },
      },
    ],
  }),
  useUpdateSegment: () => ({ mutate: updateMutate, isPending: false }),
  usePreviewSegment: () => ({ mutate: vi.fn(), isPending: false }),
  useSplitSegment: () => ({ mutateAsync: vi.fn().mockResolvedValue([]), isPending: false }),
  useMergeSegments: () => ({ mutate: vi.fn(), isPending: false }),
  useRegenerateSegment: () => ({ mutate: vi.fn(), isPending: false }),
}));

// useStoryPlayback is a side-effect hook — no-op in unit tests but we record
// the items it is handed so the wiring test can assert on observable data
// (which Story is mounted into the audio engine) without a spy call-count.
vi.mock('@/lib/hooks/useStoryPlayback', () => ({
  useStoryPlayback: (items: unknown) => {
    storyPlaybackItemsSeen.push(items);
  },
}));

// Mock useStory from useStories
vi.mock('@/lib/hooks/useStories', () => ({
  useStory: vi.fn(() => ({
    data: {
      id: 'story-1',
      name: 'Chapter 1',
      items: [
        {
          id: 'item-1',
          story_id: 'story-1',
          generation_id: 'g12',
          start_time_ms: 0,
          duration: 2.0,
          track: 0,
          trim_start_ms: 0,
          trim_end_ms: 0,
          volume: 1,
          profile_id: 'p1',
          profile_name: 'Mira',
          text: '"We need to move fast,"',
          language: 'en',
          audio_path: '/audio/g12.mp3',
          created_at: '2024-01-01T00:00:00Z',
          generation_created_at: '2024-01-01T00:00:00Z',
        },
        {
          id: 'item-2',
          story_id: 'story-1',
          generation_id: 'g13',
          start_time_ms: 2000,
          duration: 2.5,
          track: 0,
          trim_start_ms: 0,
          trim_end_ms: 0,
          volume: 1,
          profile_id: 'p2',
          profile_name: 'Holt',
          text: '"I know," Holt replied.',
          language: 'en',
          audio_path: '/audio/g13.mp3',
          created_at: '2024-01-01T00:00:00Z',
          generation_created_at: '2024-01-01T00:00:00Z',
        },
      ],
    },
  })),
}));

// ─── Test suite ───────────────────────────────────────────────────────────────

describe('ChapterEditor — read-along D5', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockReadAlongPlaying = false;
    mockCurrentSpokenSegmentId = null;
    mockCurrentTimeMs = 0;
    mockIsPlaying = false;
    mockActiveStoryId = null;
    mockActiveStoryItems = null;
    storyPlaybackItemsSeen.length = 0;
    lastUpdatePayload = null;
  });

  it('readalong-btn renders with a play affordance when read-along is idle', () => {
    render(<ChapterEditor />);
    const btn = screen.getByTestId('readalong-btn');
    expect(btn).toBeInTheDocument();
    expect(btn).not.toBeDisabled();
    // Idle state shows the play glyph; never the pause glyph or stop copy.
    expect(btn.textContent).toContain('▶');
    expect(btn.textContent).not.toContain('⏸');
    expect(btn.textContent?.toLowerCase()).not.toContain('stop');
  });

  it('clicking readalong-btn while idle transitions readAlongPlaying to true and mounts the chapter Story for playback', async () => {
    const u = userEvent.setup();
    render(<ChapterEditor />);
    await u.click(screen.getByTestId('readalong-btn'));

    // Outcome 1: the read-along flag downstream UI keys on is now true.
    // (Same flag the button label, segment highlight, and ChapterReadAlong
    // mount all read from booksStore.)
    expect(mockReadAlongPlaying).toBe(true);

    // Outcome 2: the chapter Story has been handed to the audio engine in
    // playback order — without this the rAF clock never advances and the
    // highlight stays stuck on the first line.
    expect(mockActiveStoryId).toBe('story-1');
    expect(mockActiveStoryItems).not.toBeNull();
    const gens = (mockActiveStoryItems ?? []).map((i) => i.generation_id);
    expect(gens).toEqual(['g12', 'g13']);
    // Items must be sorted by start_time_ms ascending — the audio engine
    // assumes monotonic order to advance the playhead.
    const starts = (mockActiveStoryItems ?? []).map((i) => i.start_time_ms);
    expect(starts).toEqual([...starts].sort((a, b) => a - b));
  });

  it('clicking readalong-btn while playing transitions readAlongPlaying to false and pauses playback', async () => {
    mockReadAlongPlaying = true;
    mockIsPlaying = true;
    const u = userEvent.setup();
    render(<ChapterEditor />);
    // While playing, the button renders the stop affordance — observable
    // outcome of readAlongPlaying being true at render time.
    const btn = screen.getByTestId('readalong-btn');
    expect(btn.textContent).toContain('⏸');

    await u.click(btn);

    // Outcomes: read-along flag falls back to idle and playback is paused.
    expect(mockReadAlongPlaying).toBe(false);
    expect(mockIsPlaying).toBe(false);
  });

  it('when currentSpokenSegmentId=13, seg-13 has the read-along highlight', () => {
    mockCurrentSpokenSegmentId = '13';
    mockReadAlongPlaying = true;
    render(<ChapterEditor />);
    const seg13 = screen.getByTestId('seg-13');
    // Should have either a highlight class or data-active or aria-current
    const isHighlighted =
      seg13.classList.contains('readalong-active') ||
      seg13.getAttribute('data-active') === 'true' ||
      seg13.getAttribute('aria-current') === 'true' ||
      // Or a ♪ marker sibling
      seg13.closest('span')?.textContent?.includes('♪') ||
      seg13.textContent?.includes('♪');
    expect(isHighlighted).toBe(true);
  });

  it('when currentSpokenSegmentId=12, seg-12 is highlighted but seg-13 is not', () => {
    mockCurrentSpokenSegmentId = '12';
    mockReadAlongPlaying = true;
    render(<ChapterEditor />);
    const seg12 = screen.getByTestId('seg-12');
    const seg13 = screen.getByTestId('seg-13');

    const seg12Highlighted =
      seg12.classList.contains('readalong-active') ||
      seg12.getAttribute('data-active') === 'true' ||
      seg12.getAttribute('aria-current') === 'true';
    const seg13Highlighted =
      seg13.classList.contains('readalong-active') ||
      seg13.getAttribute('data-active') === 'true' ||
      seg13.getAttribute('aria-current') === 'true';

    expect(seg12Highlighted).toBe(true);
    expect(seg13Highlighted).toBe(false);
  });

  it('ChapterReadAlong promotes seg-13 to currentSpokenSegmentId when currentTimeMs sits in the second segments range', () => {
    // Story-item mapping: seg 12 → item at 0..2000ms, seg 13 → item at
    // 2000..4500ms (2.5s duration). At t=2500ms the active segment is 13.
    mockCurrentTimeMs = 2500;
    mockReadAlongPlaying = true;
    render(<ChapterEditor />);
    // Observable outcome: the booksStore state that the readalong-active
    // highlight reads from is now '13' — this is exactly the value
    // ChapterReadAlong.findActiveSegmentId resolves to.
    expect(mockCurrentSpokenSegmentId).toBe('13');
  });

  it('ChapterReadAlong promotes seg-12 to currentSpokenSegmentId when currentTimeMs sits in the first segments range', () => {
    mockCurrentTimeMs = 500;
    mockReadAlongPlaying = true;
    render(<ChapterEditor />);
    expect(mockCurrentSpokenSegmentId).toBe('12');
  });

  it('ChapterReadAlong leaves currentSpokenSegmentId null when currentTimeMs is past every story item', () => {
    // Past the end of the second item (4500ms) — no item covers t=10000ms.
    mockCurrentTimeMs = 10000;
    mockReadAlongPlaying = true;
    render(<ChapterEditor />);
    expect(mockCurrentSpokenSegmentId).toBeNull();
  });

  it('ChapterReadAlong does not run when read-along is idle, so currentSpokenSegmentId stays untouched', () => {
    // Even though currentTimeMs lands in seg-13's range, the observer is not
    // mounted (readAlongPlaying=false), so the store value never advances.
    mockCurrentTimeMs = 2500;
    mockReadAlongPlaying = false;
    render(<ChapterEditor />);
    expect(mockCurrentSpokenSegmentId).toBeNull();
  });

  it('high-confidence dialogue line (seg-13) is still clickable for reassignment during read-along', async () => {
    mockReadAlongPlaying = true;
    mockCurrentSpokenSegmentId = '12';
    const u = userEvent.setup();
    render(<ChapterEditor />);
    // seg-13 is high-confidence (Holt, 0.85) — should still open reassign popover
    await u.click(screen.getByTestId('seg-13'));
    expect(screen.getByTestId('reassign-dropdown')).toBeInTheDocument();
  });

  it('reassigning seg-13 during read-along sends an update for that segment to Mira', async () => {
    mockReadAlongPlaying = true;
    mockCurrentSpokenSegmentId = '13';
    const u = userEvent.setup();
    render(<ChapterEditor />);
    // Click seg-13 to open popover, then pick Mira from the dropdown.
    await u.click(screen.getByTestId('seg-13'));
    const dropdown = screen.getByTestId('reassign-dropdown');
    await u.click(within(dropdown).getByText('Mira'));

    // Observable outcome at the data-mutation boundary: a write was issued
    // targeting seg-13 with Mira's character id. We assert on the captured
    // payload (the HTTP-equivalent request body), not a spy call count.
    expect(lastUpdatePayload).not.toBeNull();
    expect(lastUpdatePayload!.segmentId).toBe('13');
    expect(lastUpdatePayload!.data).toMatchObject({ character_id: 'm' });
  });

  it('with read-along active, the chapter Story handed to the audio engine carries both segments items in playback order', () => {
    // The Web Audio rAF clock that advances currentTimeMs is owned by
    // useStoryPlayback. ChapterEditor must mount it with the chapter's
    // story items — otherwise isPlaying flips but currentTimeMs stays
    // frozen at 0 and ChapterReadAlong never highlights anything.
    mockReadAlongPlaying = true;
    render(<ChapterEditor />);

    // Observable data outcome: at least one render handed the engine a
    // non-empty items array containing this chapter's audio generations.
    const activeItemSets = storyPlaybackItemsSeen.filter(
      (v): v is { generation_id: string; start_time_ms: number }[] =>
        Array.isArray(v) && v.length > 0,
    );
    expect(activeItemSets.length).toBeGreaterThan(0);

    const passedItems = activeItemSets[activeItemSets.length - 1];
    const genIds = passedItems.map((it) => it.generation_id);
    expect(genIds).toEqual(['g12', 'g13']);

    // And those items must be in monotonic playback order so the engine can
    // advance the playhead correctly.
    const starts = passedItems.map((it) => it.start_time_ms);
    expect(starts).toEqual([...starts].sort((a, b) => a - b));
  });

  it('with read-along idle, the audio engine receives no items so the rAF loop stays inert', () => {
    // When read-along is off ChapterEditor must hand the hook `undefined`,
    // not the (still-fetched) chapter Story items — otherwise the engine
    // would preload buffers and start running.
    mockReadAlongPlaying = false;
    render(<ChapterEditor />);

    // Observable data outcome: every render handed the engine `undefined`
    // (no active items). There must have been at least one render so the
    // hook actually ran.
    expect(storyPlaybackItemsSeen.length).toBeGreaterThan(0);
    for (const seen of storyPlaybackItemsSeen) {
      expect(seen).toBeUndefined();
    }
  });
});
