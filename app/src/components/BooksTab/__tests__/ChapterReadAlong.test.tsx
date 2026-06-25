/// <reference types="@testing-library/jest-dom/vitest" />
/**
 * ChapterReadAlong.test.tsx
 *
 * Direct unit tests for the headless ChapterReadAlong observer component.
 *
 * SC8 quality bar: assertions target the observable outcomes the rest of the
 * app keys on — the booksStore.currentSpokenSegmentId value (which downstream
 * UI reads to render the active-segment highlight) and the DOM scroll side
 * effect on the seg-{id} element. We use the REAL zustand stores so we
 * exercise the actual integration path, with no first-party module mocks and
 * no call-count assertions.
 */
import { render, act } from '@testing-library/react';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import { ChapterReadAlong } from '@/components/BooksTab/ChapterReadAlong';
import { useBooksStore } from '@/stores/booksStore';
import { useStoryStore } from '@/stores/storyStore';
import type { SegmentResponse, StoryDetailResponse, StoryItemDetail } from '@/lib/api/types';

// ─── Fixture helpers ──────────────────────────────────────────────────────────

function makeItem(overrides: Partial<StoryItemDetail> & {
  generation_id: string;
  start_time_ms: number;
  duration: number;
}): StoryItemDetail {
  return {
    id: `item-${overrides.generation_id}`,
    story_id: 'story-1',
    track: 0,
    trim_start_ms: overrides.trim_start_ms ?? 0,
    trim_end_ms: overrides.trim_end_ms ?? 0,
    volume: 1,
    profile_id: 'p1',
    profile_name: 'Voice',
    text: 'line',
    language: 'en',
    audio_path: `/audio/${overrides.generation_id}.mp3`,
    created_at: '2024-01-01T00:00:00Z',
    generation_created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeSegment(id: string, generation_id: string, order: number): SegmentResponse {
  return {
    id,
    chapter_id: 'c1',
    character_id: 'm',
    character_name: 'Mira',
    type: 'dialogue',
    text: `seg ${id}`,
    emotion: 'calm',
    emotion_intensity: 0.5,
    order,
    audio: { status: 'completed', generation_id, duration_ms: 2000 },
  };
}

// Two consecutive items: g12 covers 0..2000ms, g13 covers 2000..4500ms.
function makeStory(items?: StoryItemDetail[]): StoryDetailResponse {
  return {
    id: 'story-1',
    name: 'Chapter 1',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    items: items ?? [
      makeItem({ generation_id: 'g12', start_time_ms: 0, duration: 2.0 }),
      makeItem({ generation_id: 'g13', start_time_ms: 2000, duration: 2.5 }),
    ],
  };
}

const seg12 = makeSegment('12', 'g12', 0);
const seg13 = makeSegment('13', 'g13', 1);

// Set currentTimeMs on the real storyStore. We use setState (zustand's public
// API) rather than play()/seek() because seek() clamps to totalDurationMs and
// we want freedom to set arbitrary times (including past the end).
function setCurrentTimeMs(ms: number) {
  act(() => {
    useStoryStore.setState({ currentTimeMs: ms });
  });
}

describe('ChapterReadAlong — headless playback-to-segment observer', () => {
  beforeEach(() => {
    // Reset both real stores between tests.
    useBooksStore.getState().reset();
    useStoryStore.setState({
      isPlaying: false,
      currentTimeMs: 0,
      totalDurationMs: 0,
      playbackStoryId: null,
      playbackItems: null,
      playbackStartContextTime: null,
      playbackStartStoryTime: null,
    });
  });

  it('renders nothing — it is a headless DOM-free observer', () => {
    const { container } = render(
      <ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('sets currentSpokenSegmentId to the segment whose item covers currentTimeMs (first item)', () => {
    useStoryStore.setState({ currentTimeMs: 500 });
    render(<ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('12');
  });

  it('sets currentSpokenSegmentId to the segment whose item covers currentTimeMs (second item)', () => {
    useStoryStore.setState({ currentTimeMs: 2500 });
    render(<ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('13');
  });

  it('treats item boundaries as half-open [start, end): the start instant belongs to the next item', () => {
    // currentTimeMs=2000ms falls exactly on the seg-13 start. The component
    // uses currentTimeMs < itemEnd / currentTimeMs >= itemStart, which means
    // the boundary instant belongs to seg-13, not seg-12.
    useStoryStore.setState({ currentTimeMs: 2000 });
    render(<ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('13');
  });

  it('leaves currentSpokenSegmentId null when currentTimeMs is past every items effective end', () => {
    useStoryStore.setState({ currentTimeMs: 10_000 });
    render(<ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBeNull();
  });

  it('leaves currentSpokenSegmentId null when no segment matches the items generation_id at that time', () => {
    // Story has an item at 0..2000ms with generation_id=g99, but no segment
    // references g99 — so even though the time covers an item, the lookup
    // resolves to null.
    const story = makeStory([
      makeItem({ generation_id: 'g99', start_time_ms: 0, duration: 2.0 }),
    ]);
    useStoryStore.setState({ currentTimeMs: 500 });
    render(<ChapterReadAlong story={story} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBeNull();
  });

  it('returns null active segment when the storys items array is empty', () => {
    const story = makeStory([]);
    useStoryStore.setState({ currentTimeMs: 500 });
    render(<ChapterReadAlong story={story} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBeNull();
  });

  it('honours trim_start_ms and trim_end_ms when computing each items effective window', () => {
    // Item duration 5.0s but trim 1000ms off the head and 1000ms off the tail
    // → effective duration 3.0s. Start at 0 → effective window [0, 3000).
    const trimmed = makeStory([
      makeItem({
        generation_id: 'g12',
        start_time_ms: 0,
        duration: 5.0,
        trim_start_ms: 1000,
        trim_end_ms: 1000,
      }),
    ]);

    // Inside the effective window → maps to seg-12.
    useStoryStore.setState({ currentTimeMs: 2_500 });
    const { unmount } = render(
      <ChapterReadAlong story={trimmed} segments={[seg12, seg13]} />,
    );
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('12');
    unmount();

    // Past the effective end (3000ms) but well before the raw duration end
    // (5000ms) → no item covers it.
    useBooksStore.getState().setCurrentSpokenSegment(null);
    useStoryStore.setState({ currentTimeMs: 4_000 });
    render(<ChapterReadAlong story={trimmed} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBeNull();
  });

  it('does nothing when story is null — segments and time are irrelevant', () => {
    // Pre-seed a stale id; the observer must NOT touch it because the
    // ref-tracked last id is also null (idle path: no-op).
    useBooksStore.getState().setCurrentSpokenSegment('stale-id');
    useStoryStore.setState({ currentTimeMs: 500 });
    render(<ChapterReadAlong story={null} segments={[seg12, seg13]} />);
    // Idle path on first render: lastSegmentIdRef starts null, story is null
    // → effect returns early without touching the store.
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('stale-id');
  });

  it('does nothing when segments are empty and no segment was previously active', () => {
    useBooksStore.getState().setCurrentSpokenSegment('stale-id');
    useStoryStore.setState({ currentTimeMs: 500 });
    render(<ChapterReadAlong story={makeStory()} segments={[]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('stale-id');
  });

  it('clears currentSpokenSegmentId when the story becomes null after a segment was active (cleanup path)', () => {
    // First render: time inside seg-12s window → currentSpokenSegmentId='12'.
    useStoryStore.setState({ currentTimeMs: 500 });
    const { rerender } = render(
      <ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />,
    );
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('12');

    // Rerender with story=null → the cleanup branch must reset the store.
    rerender(<ChapterReadAlong story={null} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBeNull();
  });

  it('clears currentSpokenSegmentId when segments becomes empty after a segment was active', () => {
    useStoryStore.setState({ currentTimeMs: 500 });
    const { rerender } = render(
      <ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />,
    );
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('12');

    rerender(<ChapterReadAlong story={makeStory()} segments={[]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBeNull();
  });

  it('updates currentSpokenSegmentId as currentTimeMs advances across item boundaries', () => {
    useStoryStore.setState({ currentTimeMs: 500 });
    render(<ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('12');

    // Advance into the second items window.
    setCurrentTimeMs(2_500);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBe('13');

    // Past the end of all items → null.
    setCurrentTimeMs(10_000);
    expect(useBooksStore.getState().currentSpokenSegmentId).toBeNull();
  });

  it('smoothly scrolls the active segment node into view (centered) when the playhead advances', () => {
    // Install a DOM node carrying the data-testid the observer queries for,
    // with a spy on scrollIntoView. The component looks up by attribute and
    // calls scrollIntoView on every currentTimeMs change once an active id
    // has been resolved.
    const scrollSpy = vi.fn();
    const el = document.createElement('div');
    el.setAttribute('data-testid', 'seg-12');
    (el as unknown as HTMLElement & { scrollIntoView: typeof scrollSpy }).scrollIntoView = scrollSpy;
    document.body.appendChild(el);

    try {
      useStoryStore.setState({ currentTimeMs: 500 });
      render(<ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />);
      // The first render resolves the active id (seg-12) and queues both
      // effects. The scroll effect runs on the same mount and, because the
      // ref is populated synchronously during the first effect, the scroll
      // effect fires for the second currentTimeMs change below.
      // Advance the playhead — this re-runs the scroll effect.
      setCurrentTimeMs(800);
      expect(scrollSpy).toHaveBeenCalled();
      const args = scrollSpy.mock.calls.at(-1)?.[0];
      expect(args).toMatchObject({ behavior: 'smooth', block: 'center' });
    } finally {
      el.remove();
    }
  });

  it('skips scrollIntoView when no DOM node matches the active seg-{id} (graceful no-op)', () => {
    // The active id resolves to seg-12 at currentTimeMs=500, but the DOM only
    // contains a node for a DIFFERENT segment (seg-99). The observer must
    // querySelector by the active id, find no match, and skip the scroll —
    // it must NOT fall back to scrolling some other node, and it must still
    // publish the active id so the segment-highlight UI keeps working.
    const decoyScrollSpy = vi.fn();
    const decoy = document.createElement('div');
    decoy.setAttribute('data-testid', 'seg-99');
    (decoy as unknown as HTMLElement & { scrollIntoView: typeof decoyScrollSpy }).scrollIntoView = decoyScrollSpy;
    document.body.appendChild(decoy);

    try {
      useStoryStore.setState({ currentTimeMs: 500 });
      render(<ChapterReadAlong story={makeStory()} segments={[seg12, seg13]} />);
      // Advance the playhead so the scroll effect re-runs at least once with
      // an active id populated in the ref.
      setCurrentTimeMs(800);

      // Concrete post-condition #1: the unrelated DOM node was never touched.
      expect(decoyScrollSpy).not.toHaveBeenCalled();
      // Concrete post-condition #2: no seg-12 element ever materialised in
      // the DOM (so we know the no-op branch was actually exercised — there
      // was genuinely nothing for querySelector to find).
      expect(document.querySelector('[data-testid="seg-12"]')).toBeNull();
      // Concrete post-condition #3: the active id was still published so the
      // visual highlight pipeline (which reads booksStore directly) still
      // works even when no DOM node is mounted yet.
      expect(useBooksStore.getState().currentSpokenSegmentId).toBe('12');
    } finally {
      decoy.remove();
    }
  });
});
