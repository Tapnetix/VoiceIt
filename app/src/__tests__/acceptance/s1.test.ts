/// <reference types="@testing-library/jest-dom/vitest" />
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

import { useReferenceTranscript } from '@/lib/hooks/useReferenceTranscript';
import { apiClient } from '@/lib/api/client';

// ---------------------------------------------------------------------------
// Scenario S1
// ---------------------------------------------------------------------------
//
// Acceptance scenario S1: when the Whisper model download never completes,
// `useReferenceTranscript` must keep polling up to the documented ceiling
// (MAX_DOWNLOAD_RETRIES = 75; see useReferenceTranscript.ts). After the
// transition from the 74th retry to the 75th, the hook MUST transition into a
// terminal `failed` state, surface the backend error to the caller, and stop
// scheduling further polls — otherwise an offline / wedged model service would
// cause the hook to spin forever.
//
// Behaviour boundary: we spy on `apiClient.transcribeAudio` (the HTTP edge),
// never on first-party modules. The hook's `useMutation` machinery and its
// retry-timer logic both participate in the test for real, with vitest fake
// timers driving wall-clock advancement deterministically.
//
// Total backend-call accounting (matches the implementation in
// useReferenceTranscript.ts):
//   attempt 0 (initial)                                   -> 1 call
//   attempts 1..74 scheduled because attempt < 75 holds   -> 74 calls
//   attempt 75 scheduled when previous check held         -> 1 call
//   on that final call the guard `attempt < MAX_DOWNLOAD_RETRIES`
//   is FALSE, so the hook goes to 'failed' instead of scheduling.
// Total: 76 calls, then no further calls regardless of timer advancement.

const DOWNLOAD_RETRY_MS = 4000;
const MAX_DOWNLOAD_RETRIES = 75; // mirrors useReferenceTranscript.ts
const TOTAL_BACKEND_CALLS = MAX_DOWNLOAD_RETRIES + 1; // initial + 75 retries

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
  initialText?: string;
}

function setup({ file, initialText = '' }: SetupOptions) {
  const state = { text: initialText, lastWritten: null as string | null };
  const setText = (value: string) => {
    state.text = value;
    state.lastWritten = value;
  };
  const wrapper = makeWrapper();
  const { result, unmount } = renderHook(
    (props: { file: File | null }) =>
      useReferenceTranscript({
        file: props.file,
        text: state.text,
        setText,
      }),
    { initialProps: { file }, wrapper },
  );
  return { result, state, unmount };
}

function makeDownloadingError(): Error & { code: string } {
  return Object.assign(new Error('Whisper model is downloading'), {
    code: 'MODEL_DOWNLOADING',
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('S1: useReferenceTranscript download retry ceiling', () => {
  it(
    'S1: at MAX_DOWNLOAD_RETRIES (attempt 74 -> 75) transitions to terminal failed and stops polling',
    async () => {
      vi.useFakeTimers();
      try {
        // Every backend attempt reports the model is still downloading. This
        // forces the hook to walk all the way through its retry budget.
        const spy = vi
          .spyOn(apiClient, 'transcribeAudio')
          .mockRejectedValue(makeDownloadingError());

        const fileA = new File(['a'], 'window-a.wav', { type: 'audio/wav' });
        const h = setup({ file: fileA });

        // Flush attempt 0 (the initial run). The hook treats the typed
        // MODEL_DOWNLOADING error as "still downloading" and stays in the
        // downloading state rather than going to failed.
        await act(async () => {});
        expect(h.result.current.status).toBe('downloading');
        expect(h.result.current.isTranscribing).toBe(true);
        expect(h.result.current.error).toBeNull();
        expect(spy.mock.calls.length).toBe(1); // attempt 0 only so far

        // Walk attempts 1..74 by advancing the retry timer once per attempt.
        // Each tick fires the next poll, which again rejects with
        // MODEL_DOWNLOADING; the hook must stay in 'downloading' the whole way
        // and never reach 'failed' yet. Stepping one timer at a time keeps the
        // chained setState + new setTimeout from each rejection deterministic.
        for (let i = 1; i <= MAX_DOWNLOAD_RETRIES - 1; i++) {
          await act(async () => {
            await vi.advanceTimersByTimeAsync(DOWNLOAD_RETRY_MS);
          });
        }
        expect(h.result.current.status).toBe('downloading');
        expect(h.result.current.isTranscribing).toBe(true);
        expect(h.result.current.error).toBeNull();
        // We have now made MAX_DOWNLOAD_RETRIES total calls (attempts 0..74).
        expect(spy.mock.calls.length).toBe(MAX_DOWNLOAD_RETRIES);

        // The final transition: advance one more retry timer. The 76th call
        // (attempt index 75) fails with MODEL_DOWNLOADING but the guard
        // `attempt < MAX_DOWNLOAD_RETRIES` is now false, so the hook must
        // give up: status flips to 'failed', the backend error surfaces
        // through `error`, and isTranscribing drops to false.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(DOWNLOAD_RETRY_MS);
        });

        expect(h.result.current.status).toBe('failed');
        expect(spy.mock.calls.length).toBe(TOTAL_BACKEND_CALLS);
        expect(h.result.current.error).toBe('Whisper model is downloading');
        expect(h.result.current.isTranscribing).toBe(false);
        // The transcript must NOT have been clobbered with an error string.
        expect(h.state.lastWritten).toBeNull();

        // After terminal failure, no further polls fire even if the wall clock
        // keeps advancing — the hook has stopped scheduling retries.
        const callsAfterTerminal = spy.mock.calls.length;
        await act(async () => {
          await vi.advanceTimersByTimeAsync(DOWNLOAD_RETRY_MS * 10);
        });
        expect(spy.mock.calls.length).toBe(callsAfterTerminal);
        expect(h.result.current.status).toBe('failed');
      } finally {
        vi.useRealTimers();
      }
    },
    30_000,
  );
});
