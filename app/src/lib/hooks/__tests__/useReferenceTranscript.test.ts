/// <reference types="@testing-library/jest-dom/vitest" />
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

import { useReferenceTranscript } from '@/lib/hooks/useReferenceTranscript';
import { apiClient } from '@/lib/api/client';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
//
// Behaviour bar:
//   - The boundary is `apiClient.transcribeAudio` (the HTTP edge). We spy on
//     that, never on the first-party `useTranscription` hook — the real
//     `useMutation` machinery participates in every test.
//   - Tests assert observable outcomes: the public `status` / `error` /
//     `regeneratePrompt` / `isTranscribing` fields, and the *value* the hook
//     hands to `setText` (its only side-channel back to the consumer).
//   - No `toHaveBeenCalledTimes` on internal collaborators, no checks that
//     blindly assert "the function ran" — every assertion describes WHAT the
//     hook should do, not HOW it does it.

function makeWrapper() {
  // Disable retries so a rejected mutation fails fast and bubbles to the hook.
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
  // Mirror the consumer contract: `text` is owned by the parent component, and
  // the hook writes back through `setText`. We model that with a closure-held
  // string so the hook's "isEdited" detection sees what we set.
  const state = { text: initialText, lastWritten: null as string | null };
  const setText = (value: string) => {
    state.text = value;
    state.lastWritten = value;
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
    rerenderWith: (next: File | null) => rerender({ file: next }),
    /** Simulate a manual edit from outside the hook. */
    userTypes: (value: string) => {
      state.text = value;
    },
    unmount,
  };
}

const fileA = new File(['a'], 'window-a.wav', { type: 'audio/wav' });
const fileB = new File(['b'], 'window-b.wav', { type: 'audio/wav' });

function makeTranscriptionResponse(text: string) {
  // The hook only reads `.text`; other fields are fine to omit.
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useReferenceTranscript — first confirmed clip', () => {
  it('fills the transcript with the backend result and reaches the filled state', async () => {
    vi.spyOn(apiClient, 'transcribeAudio').mockResolvedValue(
      makeTranscriptionResponse('hello reference'),
    );

    const h = setup({ file: fileA });

    await waitFor(() => expect(h.result.current.status).toBe('filled'));
    expect(h.state.lastWritten).toBe('hello reference');
    expect(h.state.text).toBe('hello reference');
    expect(h.result.current.error).toBeNull();
    expect(h.result.current.regeneratePrompt).toBe(false);
    expect(h.result.current.isTranscribing).toBe(false);
  });

  it('exposes isTranscribing while the backend is still working', async () => {
    let resolveFn: (value: { text: string; duration: number }) => void = () => {};
    vi.spyOn(apiClient, 'transcribeAudio').mockImplementation(
      () =>
        new Promise<{ text: string; duration: number }>((resolve) => {
          resolveFn = resolve;
        }),
    );

    const h = setup({ file: fileA });

    await waitFor(() => expect(h.result.current.status).toBe('transcribing'));
    expect(h.result.current.isTranscribing).toBe(true);
    expect(h.result.current.error).toBeNull();

    await act(async () => {
      resolveFn({ text: 'eventually', duration: 0 });
    });
    await waitFor(() => expect(h.result.current.status).toBe('filled'));
    expect(h.result.current.isTranscribing).toBe(false);
  });
});

describe('useReferenceTranscript — failures', () => {
  it('surfaces the error message and stays in the failed state when the backend rejects', async () => {
    vi.spyOn(apiClient, 'transcribeAudio').mockRejectedValue(
      new Error('stt service unavailable'),
    );

    const h = setup({ file: fileA });

    await waitFor(() => expect(h.result.current.status).toBe('failed'));
    expect(h.result.current.error).toBe('stt service unavailable');
    expect(h.result.current.isTranscribing).toBe(false);
    // The transcript must NOT be clobbered with an error string.
    expect(h.state.lastWritten).toBeNull();
  });

  it('reports a friendly failure when the backend returns an empty transcript', async () => {
    vi.spyOn(apiClient, 'transcribeAudio').mockResolvedValue(
      makeTranscriptionResponse('   '),
    );

    const h = setup({ file: fileA });

    await waitFor(() => expect(h.result.current.status).toBe('failed'));
    expect(h.result.current.error).toBe('the transcription came back empty');
    expect(h.state.lastWritten).toBeNull();
  });
});

describe('useReferenceTranscript — repeated file identity', () => {
  it('is idempotent across re-renders that pass the same File reference', async () => {
    vi.spyOn(apiClient, 'transcribeAudio').mockResolvedValue(
      makeTranscriptionResponse('once'),
    );

    const h = setup({ file: fileA });
    await waitFor(() => expect(h.result.current.status).toBe('filled'));

    // Re-render with the same File reference. Outcome contract: we stay in
    // 'filled' with the same text, no regenerate-prompt, no error.
    h.rerenderWith(fileA);
    await act(async () => {});
    expect(h.result.current.status).toBe('filled');
    expect(h.state.text).toBe('once');
    expect(h.result.current.regeneratePrompt).toBe(false);
  });
});

describe('useReferenceTranscript — new confirmed clip', () => {
  it('silently re-transcribes when the transcript has not been edited', async () => {
    const spy = vi
      .spyOn(apiClient, 'transcribeAudio')
      .mockResolvedValueOnce(makeTranscriptionResponse('first take'))
      .mockResolvedValueOnce(makeTranscriptionResponse('second take'));

    const h = setup({ file: fileA });
    await waitFor(() => expect(h.state.text).toBe('first take'));

    // The transcript is exactly what the hook auto-filled → not edited.
    h.rerenderWith(fileB);

    await waitFor(() => expect(h.state.text).toBe('second take'));
    expect(h.result.current.status).toBe('filled');
    expect(h.result.current.regeneratePrompt).toBe(false);
    // Both calls had to target the user-confirmed clips, not stale data.
    const firstArg = spy.mock.calls[0]?.[0];
    const secondArg = spy.mock.calls[1]?.[0];
    expect(firstArg).toBe(fileA);
    expect(secondArg).toBe(fileB);
  });

  it('asks before overwriting an edited transcript on a new clip', async () => {
    vi.spyOn(apiClient, 'transcribeAudio').mockResolvedValue(
      makeTranscriptionResponse('auto-filled'),
    );

    const h = setup({ file: fileA });
    await waitFor(() => expect(h.state.text).toBe('auto-filled'));

    // User edits the transcript, then a new clip arrives.
    h.userTypes('hand-typed words');
    h.rerenderWith(fileB);

    await waitFor(() => expect(h.result.current.regeneratePrompt).toBe(true));
    // We did NOT clobber the user's text.
    expect(h.state.text).toBe('hand-typed words');
    expect(h.result.current.status).toBe('filled'); // unchanged from the previous fill
  });
});

describe('useReferenceTranscript — regenerate prompt resolution', () => {
  it('keepEdits dismisses the prompt and leaves the user-edited transcript intact', async () => {
    vi.spyOn(apiClient, 'transcribeAudio').mockResolvedValue(
      makeTranscriptionResponse('auto-filled'),
    );

    const h = setup({ file: fileA });
    await waitFor(() => expect(h.state.text).toBe('auto-filled'));
    h.userTypes('I will keep this');
    h.rerenderWith(fileB);
    await waitFor(() => expect(h.result.current.regeneratePrompt).toBe(true));

    act(() => h.result.current.keepEdits());

    expect(h.result.current.regeneratePrompt).toBe(false);
    expect(h.state.text).toBe('I will keep this');
    // No new transcript was written.
    expect(h.state.lastWritten).toBe('auto-filled');
  });

  it('acceptRegenerate replaces the edited transcript with a fresh backend result', async () => {
    vi.spyOn(apiClient, 'transcribeAudio')
      .mockResolvedValueOnce(makeTranscriptionResponse('first auto'))
      .mockResolvedValueOnce(makeTranscriptionResponse('second auto'));

    const h = setup({ file: fileA });
    await waitFor(() => expect(h.state.text).toBe('first auto'));
    h.userTypes('my edited version');
    h.rerenderWith(fileB);
    await waitFor(() => expect(h.result.current.regeneratePrompt).toBe(true));

    act(() => h.result.current.acceptRegenerate());

    await waitFor(() => expect(h.state.text).toBe('second auto'));
    expect(h.result.current.regeneratePrompt).toBe(false);
    expect(h.result.current.status).toBe('filled');
  });
});

describe('useReferenceTranscript — retranscribe action', () => {
  it('replaces the current transcript with a fresh run on the same clip', async () => {
    vi.spyOn(apiClient, 'transcribeAudio')
      .mockResolvedValueOnce(makeTranscriptionResponse('original'))
      .mockResolvedValueOnce(makeTranscriptionResponse('retake'));

    const h = setup({ file: fileA });
    await waitFor(() => expect(h.state.text).toBe('original'));

    act(() => h.result.current.retranscribe());

    await waitFor(() => expect(h.state.text).toBe('retake'));
    expect(h.result.current.status).toBe('filled');
    expect(h.result.current.error).toBeNull();
  });

  it('does nothing when there is no confirmed clip yet', async () => {
    // If the hook were to (incorrectly) hit the boundary, the resolved value
    // would be written to setText. We use a sentinel string and assert that
    // string never reaches the consumer — an observable outcome stronger than
    // a call-count: it covers both "not called" and "called but ignored".
    vi.spyOn(apiClient, 'transcribeAudio').mockResolvedValue(
      makeTranscriptionResponse('SENTINEL_SHOULD_NEVER_APPEAR'),
    );

    const h = setup({ file: null });
    // Status should be idle with no file present.
    expect(h.result.current.status).toBe('idle');
    expect(h.result.current.isTranscribing).toBe(false);

    act(() => h.result.current.retranscribe());

    // Give the hook ample opportunity to (mis)transition out of idle.
    await act(async () => {});
    await act(async () => {});

    // The hook never left idle: no transcribing flicker, no error, no prompt.
    expect(h.result.current.status).toBe('idle');
    expect(h.result.current.isTranscribing).toBe(false);
    expect(h.result.current.error).toBeNull();
    expect(h.result.current.regeneratePrompt).toBe(false);
    // And — critically — the sentinel text never reached the consumer.
    expect(h.state.lastWritten).toBeNull();
    expect(h.state.text).toBe('');
  });
});

describe('useReferenceTranscript — clip cleared', () => {
  it('resets to idle without further transcription when the clip is removed', async () => {
    vi.spyOn(apiClient, 'transcribeAudio').mockResolvedValue(
      makeTranscriptionResponse('done'),
    );

    const h = setup({ file: fileA });
    await waitFor(() => expect(h.result.current.status).toBe('filled'));

    h.rerenderWith(null);

    await waitFor(() => expect(h.result.current.status).toBe('idle'));
    expect(h.result.current.regeneratePrompt).toBe(false);
    expect(h.result.current.isTranscribing).toBe(false);
  });
});

describe('useReferenceTranscript — Whisper model download', () => {
  it('shows the downloading state and fills the transcript once the model is ready', async () => {
    vi.useFakeTimers();
    try {
      vi.spyOn(apiClient, 'transcribeAudio')
        .mockRejectedValueOnce(makeDownloadingError())
        .mockResolvedValueOnce(makeTranscriptionResponse('finally transcribed'));

      const h = setup({ file: fileA });

      // Flush the first attempt (microtasks): the typed downloading error
      // surfaces as 'downloading' rather than 'failed', and isTranscribing
      // stays true so the UI can show a spinner.
      await act(async () => {});
      expect(h.result.current.status).toBe('downloading');
      expect(h.result.current.isTranscribing).toBe(true);
      expect(h.result.current.error).toBeNull();

      // Advance the retry timer; the second attempt resolves and the hook
      // fills the transcript.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000);
      });
      expect(h.result.current.status).toBe('filled');
      expect(h.state.text).toBe('finally transcribed');
      expect(h.result.current.isTranscribing).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not schedule a retry after the hook unmounts', async () => {
    vi.useFakeTimers();
    try {
      // First attempt: downloading (forces the hook to schedule a retry).
      // Second attempt: if the retry ever fires post-unmount, it would
      // successfully resolve and the resulting text would be written to
      // setText — a directly observable leak. The test passes only when
      // that text never reaches the consumer.
      vi.spyOn(apiClient, 'transcribeAudio')
        .mockRejectedValueOnce(makeDownloadingError())
        .mockResolvedValueOnce(
          makeTranscriptionResponse('LEAKED_AFTER_UNMOUNT'),
        );

      const h = setup({ file: fileA });
      await act(async () => {});
      // Precondition: the hook is in the downloading state with a pending retry.
      expect(h.result.current.status).toBe('downloading');
      expect(h.state.lastWritten).toBeNull();

      h.unmount();
      await act(async () => {
        // Advance well past the retry interval. If the timer leaked, the
        // second mock would resolve and write 'LEAKED_AFTER_UNMOUNT'.
        await vi.advanceTimersByTimeAsync(4000);
      });
      await act(async () => {
        // Flush any post-timer microtasks (mutateAsync resolution).
        await Promise.resolve();
      });

      // Observable outcome: the unmounted hook never wrote the leaked text.
      // This covers both "no retry call fired" AND "a call fired but was
      // ignored" — both are valid behaviours of a correctly torn-down hook.
      expect(h.state.lastWritten).toBeNull();
      expect(h.state.text).toBe('');
    } finally {
      vi.useRealTimers();
    }
  });
});
