/// <reference types="@testing-library/jest-dom/vitest" />
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

import { useReferenceTranscript } from '@/lib/hooks/useReferenceTranscript';
import { apiClient } from '@/lib/api/client';

// ---------------------------------------------------------------------------
// Acceptance scenario S2
// ---------------------------------------------------------------------------
//
// Scenario (from the audit-coverage plan):
//   Changing the in-flight target mid-flight cancels the in-flight poll;
//   no stale callback fires for the cancelled target.
//
// Mapping note:
//   The plan brief is written in generic terms ("profileId"). In
//   `useReferenceTranscript` the mid-flight identifier is the confirmed
//   `file` reference, and the hook's only "poll" is the Whisper
//   model-download retry loop scheduled by `setTimeout(DOWNLOAD_RETRY_MS)`.
//   Swapping `file` from A -> B while A's poll is pending must:
//     (1) cancel the pending retry for A (no further A request happens
//         after the swap),
//     (2) never write A's eventual text into the consumer's transcript,
//     (3) follow through with B as the new source of truth.
//
// Boundary:
//   We spy on `apiClient.transcribeAudio` (the HTTP edge). Everything
//   above it — `useMutation`, the hook's state machine, its retry timer —
//   runs for real. No first-party hook is mocked.

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const Wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
  return Wrapper;
}

interface SetupOptions {
  file: File | null;
}

function setup({ file }: SetupOptions) {
  // Mirror the consumer contract: parent owns `text`, hook writes back via
  // `setText`. Track every value the hook hands us so we can assert that a
  // cancelled clip's result never lands.
  const writes: string[] = [];
  const state = { text: '' };
  const setText = (value: string) => {
    state.text = value;
    writes.push(value);
  };
  const wrapper = makeWrapper();
  const { result, rerender, unmount } = renderHook(
    (props: { file: File | null }) =>
      useReferenceTranscript({
        file: props.file,
        text: state.text,
        setText,
      }),
    { initialProps: { file }, wrapper },
  );
  return {
    result,
    state,
    writes,
    rerenderWith: (next: File | null) => rerender({ file: next }),
    unmount,
  };
}

const fileA = new File(['a'], 'window-a.wav', { type: 'audio/wav' });
const fileB = new File(['b'], 'window-b.wav', { type: 'audio/wav' });

function makeTranscriptionResponse(text: string) {
  return { text } as Awaited<ReturnType<typeof apiClient.transcribeAudio>>;
}

function makeDownloadingError(): Error & { code: string } {
  // Mirrors what apiClient.transcribeAudio throws on HTTP 202.
  return Object.assign(new Error('Whisper model is downloading'), {
    code: 'MODEL_DOWNLOADING',
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('S2: changing the in-flight target cancels the in-flight poll', () => {
  it('S2: swapping the confirmed clip mid-poll cancels the pending retry for the old clip', async () => {
    vi.useFakeTimers();
    try {
      // Per-call dispatch keyed on the actual File argument. A is "stuck"
      // on the model-download response; B succeeds immediately.
      const spy = vi
        .spyOn(apiClient, 'transcribeAudio')
        .mockImplementation((arg: File) => {
          if (arg === fileA) return Promise.reject(makeDownloadingError());
          if (arg === fileB) return Promise.resolve(makeTranscriptionResponse('B transcript'));
          throw new Error(`unexpected file: ${(arg as File).name}`);
        });

      const h = setup({ file: fileA });

      // Drain microtasks → the first A attempt has rejected with the
      // downloading marker, hook is in 'downloading' with a retry queued.
      await act(async () => {});
      expect(h.result.current.status).toBe('downloading');
      expect(h.result.current.isTranscribing).toBe(true);

      const aCallsBeforeSwap = spy.mock.calls.filter((c) => c[0] === fileA).length;
      expect(aCallsBeforeSwap).toBeGreaterThan(0);

      // ── Mid-flight swap ────────────────────────────────────────────────
      h.rerenderWith(fileB);

      // Resolve B's microtasks. Status flips to 'filled' with B's text.
      await act(async () => {});
      expect(h.result.current.status).toBe('filled');
      expect(h.state.text).toBe('B transcript');

      const aCallsRightAfterSwap = spy.mock.calls.filter((c) => c[0] === fileA).length;

      // Now advance past A's would-be retry window. If the cancellation is
      // correct, no further A request happens.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });

      // (1) The cancelled A poll never fired again after the swap.
      const aCallsAfterFlush = spy.mock.calls.filter((c) => c[0] === fileA).length;
      expect(aCallsAfterFlush).toBe(aCallsRightAfterSwap);

      // (2) The consumer's transcript was only ever written with B's value.
      expect(h.writes).toContain('B transcript');
      for (const value of h.writes) {
        expect(value).toBe('B transcript');
      }

      // (3) Final outcome is B as the source of truth, no error surfaced.
      expect(h.result.current.status).toBe('filled');
      expect(h.result.current.error).toBeNull();
      expect(h.state.text).toBe('B transcript');
    } finally {
      vi.useRealTimers();
    }
  });

  it('S2: clearing the clip mid-poll cancels the pending retry entirely', async () => {
    // Companion case for S2's "cancel the in-flight poll" guarantee: instead
    // of swapping A -> B, we swap A -> null (the clip is taken away). The
    // pending retry timer for A must be cancelled and the hook must settle
    // back to 'idle' with no further A request firing.
    vi.useFakeTimers();
    try {
      const spy = vi
        .spyOn(apiClient, 'transcribeAudio')
        .mockRejectedValue(makeDownloadingError());

      const h = setup({ file: fileA });

      // First A attempt rejects with downloading marker → 'downloading'
      // state with a retry queued.
      await act(async () => {});
      expect(h.result.current.status).toBe('downloading');

      const callsBeforeClear = spy.mock.calls.length;

      // ── Clip cleared mid-poll ─────────────────────────────────────────
      h.rerenderWith(null);
      await act(async () => {});
      expect(h.result.current.status).toBe('idle');
      expect(h.result.current.isTranscribing).toBe(false);

      // Flush well past the retry window. No further A request should fire.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(spy.mock.calls.length).toBe(callsBeforeClear);

      // The transcript was never written.
      expect(h.writes).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });
});
