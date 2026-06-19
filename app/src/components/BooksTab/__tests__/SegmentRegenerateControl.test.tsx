/**
 * SegmentRegenerateControl — focused unit tests for the per-line regenerate
 * menu item rendered by ChapterEditor's ⋯ SelectionDialog.
 *
 * The component's public contract:
 *  - Renders a button labeled "↻ Regenerate" when idle.
 *  - Shows a spinner + "Re-rendering…" label when the segment is being
 *    re-rendered (either the mutation is in-flight OR the segment's
 *    audio_status is 'pending' / 'generating').
 *  - Disables the button while pending/generating so the user can't queue
 *    duplicate regenerations.
 *  - Dispatches the regenerate mutation with the supplied segment/book/chapter
 *    identifiers.
 *  - Invokes onDone after a successful mutation (and is safe when no onDone
 *    callback is provided).
 *
 * Observable outcomes are asserted on the DOM (button label / disabled state /
 * spinner presence / title attr) and on the value crossing the mutation
 * boundary (mock.calls[0][0]). No call-count or internal-call assertions.
 */
/// <reference types="@testing-library/jest-dom/vitest" />
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { SegmentRegenerateControl } from '@/components/BooksTab/SegmentRegenerateControl';

// ─── Mocks ────────────────────────────────────────────────────────────────────
//
// The hook is wired so onSuccess fires synchronously — tests can assert on the
// post-mutation observable behaviour (onDone invocation) without timers.
const regenerateMutate = vi.fn(
  (_args: unknown, opts?: { onSuccess?: () => void }) => {
    opts?.onSuccess?.();
  },
);

let hookIsPending = false;

vi.mock('@/lib/hooks/useBooks', () => ({
  useRegenerateSegment: () => ({
    mutate: regenerateMutate,
    isPending: hookIsPending,
  }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('SegmentRegenerateControl', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hookIsPending = false;
  });

  it('renders an enabled "Regenerate" button when idle', () => {
    render(
      <SegmentRegenerateControl
        segmentId="s1"
        bookId="b1"
        chapterId="c1"
        audioStatus="completed"
      />,
    );

    const btn = screen.getByTestId('regenerate-btn-s1');
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveTextContent('↻ Regenerate');
    expect(btn).not.toBeDisabled();
    expect(btn).toHaveAttribute('title', 'Regenerate this line');
    // No spinner in the idle state.
    expect(screen.queryByTestId('regenerate-spinner-s1')).not.toBeInTheDocument();
  });

  it('renders an enabled idle button when audioStatus is omitted', () => {
    render(
      <SegmentRegenerateControl segmentId="s2" bookId="b1" chapterId="c1" />,
    );

    const btn = screen.getByTestId('regenerate-btn-s2');
    expect(btn).toHaveTextContent('↻ Regenerate');
    expect(btn).not.toBeDisabled();
  });

  it('renders a spinner and disables the button while the hook reports pending', () => {
    hookIsPending = true;
    render(
      <SegmentRegenerateControl
        segmentId="s3"
        bookId="b1"
        chapterId="c1"
        audioStatus="completed"
      />,
    );

    const btn = screen.getByTestId('regenerate-btn-s3');
    expect(btn).toBeDisabled();
    expect(btn).toHaveTextContent('Re-rendering');
    expect(btn).toHaveAttribute('title', 'Re-rendering this line…');
    const spinner = screen.getByTestId('regenerate-spinner-s3');
    expect(spinner).toBeInTheDocument();
    expect(spinner).toHaveAttribute('aria-label', 'Re-rendering');
  });

  it('renders a spinner when audioStatus is "pending"', () => {
    render(
      <SegmentRegenerateControl
        segmentId="s4"
        bookId="b1"
        chapterId="c1"
        audioStatus="pending"
      />,
    );

    const btn = screen.getByTestId('regenerate-btn-s4');
    expect(btn).toBeDisabled();
    expect(screen.getByTestId('regenerate-spinner-s4')).toBeInTheDocument();
  });

  it('renders a spinner when audioStatus is "generating"', () => {
    render(
      <SegmentRegenerateControl
        segmentId="s5"
        bookId="b1"
        chapterId="c1"
        audioStatus="generating"
      />,
    );

    const btn = screen.getByTestId('regenerate-btn-s5');
    expect(btn).toBeDisabled();
    expect(screen.getByTestId('regenerate-spinner-s5')).toBeInTheDocument();
  });

  it('stays idle (no spinner) for non-blocking audio statuses such as "error" or "stale"', () => {
    const { rerender } = render(
      <SegmentRegenerateControl
        segmentId="s6"
        bookId="b1"
        chapterId="c1"
        audioStatus="error"
      />,
    );
    expect(screen.getByTestId('regenerate-btn-s6')).not.toBeDisabled();
    expect(
      screen.queryByTestId('regenerate-spinner-s6'),
    ).not.toBeInTheDocument();

    rerender(
      <SegmentRegenerateControl
        segmentId="s6"
        bookId="b1"
        chapterId="c1"
        audioStatus="stale"
      />,
    );
    expect(screen.getByTestId('regenerate-btn-s6')).not.toBeDisabled();
    expect(
      screen.queryByTestId('regenerate-spinner-s6'),
    ).not.toBeInTheDocument();
  });

  it('dispatches the regenerate mutation with the segment / book / chapter ids when clicked', async () => {
    const u = userEvent.setup();
    render(
      <SegmentRegenerateControl
        segmentId="s7"
        bookId="b7"
        chapterId="c7"
        audioStatus="completed"
      />,
    );

    await u.click(screen.getByTestId('regenerate-btn-s7'));

    // Assert on the value that crossed the hook boundary, not the call count.
    const payload = regenerateMutate.mock.calls[0]?.[0];
    expect(payload).toEqual({
      segmentId: 's7',
      bookId: 'b7',
      chapterId: 'c7',
    });
  });

  it('invokes onDone after a successful regenerate', async () => {
    const u = userEvent.setup();
    const onDone = vi.fn();
    render(
      <SegmentRegenerateControl
        segmentId="s8"
        bookId="b1"
        chapterId="c1"
        audioStatus="completed"
        onDone={onDone}
      />,
    );

    await u.click(screen.getByTestId('regenerate-btn-s8'));

    // The mock wiring calls onSuccess synchronously; onDone must run.
    expect(onDone).toHaveBeenCalled();
  });

  it('does not throw when the mutation succeeds and no onDone callback is supplied', async () => {
    const u = userEvent.setup();
    render(
      <SegmentRegenerateControl
        segmentId="s9"
        bookId="b1"
        chapterId="c1"
        audioStatus="completed"
      />,
    );

    // Should not throw — onDone is optional.
    await expect(
      u.click(screen.getByTestId('regenerate-btn-s9')),
    ).resolves.not.toThrow();
  });
});
