/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { SampleUpload } from '@/components/VoiceProfiles/SampleUpload';

// ----------------------------------------------------------------------------
// Mocks
//
// SampleUpload composes a number of hooks (useAddSample, useProfile,
// useAudioPlayer, useAudioRecording, useSystemAudioCapture,
// useReferenceTranscript) plus a few child components (AudioTrimmer, the three
// AudioSample inputs, ReferenceTranscript). The component's public contract is
// the dialog it renders, the addSample mutation payload, the duration cap it
// wires into the recorders, and the toasts/state transitions it triggers in
// response to user interactions.
//
// We stub the hook implementations to expose controllable state and to capture
// the callbacks SampleUpload registers (e.g. onRecordingComplete) so each test
// can drive a specific code path and assert on observable outcomes.
// ----------------------------------------------------------------------------

// AudioTrimmer stub — exposes a "Use this clip" button and a getClip() ref.
vi.mock('@/components/AudioTrimmer/AudioTrimmer', async () => {
  const React = await import('react');
  return {
    AudioTrimmer: React.forwardRef(
      (
        { file, onConfirm }: { file: File; onConfirm: (f: File, d: number) => void },
        ref: React.Ref<{ getClip: () => { file: File; durationSec: number } | null }>,
      ) => {
        React.useImperativeHandle(ref, () => ({
          getClip: () => ({
            file: new File(['ref-clip'], 'ref-clip-from-getClip.wav', { type: 'audio/wav' }),
            durationSec: 10,
          }),
        }));
        return (
          <div data-testid="audio-trimmer">
            <span data-testid="trimmer-file-name">{file.name}</span>
            <button
              data-testid="trimmer-confirm"
              type="button"
              onClick={() => {
                const trimmed = new File(['trimmed-wav-data'], 'reference-trimmed.wav', {
                  type: 'audio/wav',
                });
                onConfirm(trimmed, 20);
              }}
            >
              Use this clip
            </button>
          </div>
        );
      },
    ),
  };
});

// Capture addSample submissions to observe what payload leaves the component.
let addSampleShouldReject = false;
const submittedPayloads: Array<{ profileId: string; file: File; referenceText: string }> = [];
const addSampleMutateAsync = vi.fn(
  async (payload: { profileId: string; file: File; referenceText: string }) => {
    submittedPayloads.push(payload);
    if (addSampleShouldReject) {
      throw new Error('upload-failed');
    }
    return {};
  },
);
let addSampleIsPending = false;

vi.mock('@/lib/hooks/useProfiles', () => ({
  useAddSample: () => ({ mutateAsync: addSampleMutateAsync, isPending: addSampleIsPending }),
  useProfile: () => ({ data: { id: 'p1', name: 'Test Profile', language: 'en' } }),
}));

// useAudioPlayer — capture the playPause call so a test can verify the form
// file is passed to it on play button click.
const playPauseCalls: Array<File | null | undefined> = [];
const cleanupAudioMock = vi.fn();
let audioPlayerIsPlaying = false;
vi.mock('@/lib/hooks/useAudioPlayer', () => ({
  useAudioPlayer: () => ({
    isPlaying: audioPlayerIsPlaying,
    playPause: (f: File | null | undefined) => {
      playPauseCalls.push(f);
    },
    cleanup: cleanupAudioMock,
  }),
}));

// useAudioRecording — controllable state; capture options & callbacks.
type RecordingState = {
  isRecording: boolean;
  duration: number;
  error: string | null;
};
const recordingState: RecordingState = { isRecording: false, duration: 0, error: null };
const recordingCancel = vi.fn();
const recordingStart = vi.fn();
const recordingStop = vi.fn();
let lastAudioRecordingOpts: {
  maxDurationSeconds?: number;
  onRecordingComplete?: (blob: Blob, duration?: number) => void;
} = {};
vi.mock('@/lib/hooks/useAudioRecording', () => ({
  useAudioRecording: (opts: {
    maxDurationSeconds?: number;
    onRecordingComplete?: (blob: Blob, duration?: number) => void;
  }) => {
    lastAudioRecordingOpts = opts;
    return {
      isRecording: recordingState.isRecording,
      duration: recordingState.duration,
      error: recordingState.error,
      startRecording: recordingStart,
      stopRecording: recordingStop,
      cancelRecording: recordingCancel,
    };
  },
}));

// useSystemAudioCapture — controllable state; capture options & callbacks.
const systemState: RecordingState & { isSupported: boolean } = {
  isRecording: false,
  duration: 0,
  error: null,
  isSupported: false,
};
const systemCancel = vi.fn();
const systemStart = vi.fn();
const systemStop = vi.fn();
let lastSystemAudioOpts: {
  maxDurationSeconds?: number;
  onRecordingComplete?: (blob: Blob, duration?: number) => void;
} = {};
vi.mock('@/lib/hooks/useSystemAudioCapture', () => ({
  useSystemAudioCapture: (opts: {
    maxDurationSeconds?: number;
    onRecordingComplete?: (blob: Blob, duration?: number) => void;
  }) => {
    lastSystemAudioOpts = opts;
    return {
      isRecording: systemState.isRecording,
      duration: systemState.duration,
      error: systemState.error,
      isSupported: systemState.isSupported,
      startRecording: systemStart,
      stopRecording: systemStop,
      cancelRecording: systemCancel,
    };
  },
}));

// Platform context — controllable.
const platformMeta = { isTauri: false };
vi.mock('@/platform/PlatformContext', () => ({
  usePlatform: () => ({
    metadata: platformMeta,
  }),
}));

// Toast — capture every call so we can assert on the messages the component
// triggers in response to recording completion, errors, and submission outcomes.
type ToastCall = { title?: string; description?: string; variant?: string };
const toastCalls: ToastCall[] = [];
const toastFn = vi.fn((args: ToastCall) => {
  toastCalls.push(args);
});
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: toastFn }),
}));

// useReferenceTranscript — capture args; expose mockable behavior.
type TranscriptHookArgs = {
  file: File | null;
  text: string;
  language?: string;
};
const transcriptArgs: TranscriptHookArgs[] = [];
const retranscribeMock = vi.fn();
const acceptRegenerateMock = vi.fn();
const keepEditsMock = vi.fn();
let lastSetText: ((v: string) => void) | undefined;
vi.mock('@/lib/hooks/useReferenceTranscript', () => ({
  useReferenceTranscript: (args: {
    file: File | null;
    text: string;
    setText: (v: string) => void;
    language?: string;
  }) => {
    transcriptArgs.push({ file: args.file, text: args.text, language: args.language });
    lastSetText = args.setText;
    return {
      status: args.file ? 'filled' : 'idle',
      isTranscribing: false,
      regeneratePrompt: false,
      retranscribe: retranscribeMock,
      acceptRegenerate: acceptRegenerateMock,
      keepEdits: keepEditsMock,
      error: null,
    };
  },
}));

// ----------------------------------------------------------------------------
// helpers
// ----------------------------------------------------------------------------

function renderSampleUpload(initialOpen = true) {
  const openStates: boolean[] = [];
  const onOpenChange = (next: boolean) => {
    openStates.push(next);
  };
  const utils = render(
    <SampleUpload profileId="p1" open={initialOpen} onOpenChange={onOpenChange} />,
  );
  return { openStates, ...utils };
}

function resetState() {
  vi.clearAllMocks();
  submittedPayloads.length = 0;
  toastCalls.length = 0;
  transcriptArgs.length = 0;
  playPauseCalls.length = 0;
  addSampleShouldReject = false;
  addSampleIsPending = false;
  audioPlayerIsPlaying = false;
  recordingState.isRecording = false;
  recordingState.duration = 0;
  recordingState.error = null;
  systemState.isRecording = false;
  systemState.duration = 0;
  systemState.error = null;
  systemState.isSupported = false;
  platformMeta.isTauri = false;
  lastAudioRecordingOpts = {};
  lastSystemAudioOpts = {};
  lastSetText = undefined;
}

beforeEach(() => {
  resetState();
});

// ----------------------------------------------------------------------------
// tests — describe WHAT the dialog does, not HOW it does it.
// ----------------------------------------------------------------------------

describe('SampleUpload — dialog visibility & lifecycle', () => {
  it('renders the upload dialog with title and description when open', () => {
    renderSampleUpload();

    // The dialog is rendered (title + description visible).
    expect(screen.getByText('Add Audio Sample')).toBeInTheDocument();
    expect(screen.getByText(/upload an audio file/i)).toBeInTheDocument();
  });

  it('does not render the dialog contents when open=false', () => {
    renderSampleUpload(false);
    expect(screen.queryByText('Add Audio Sample')).not.toBeInTheDocument();
  });

  it('clicking Cancel closes the dialog (parent receives open=false)', async () => {
    const u = userEvent.setup();
    const { openStates } = renderSampleUpload();

    await u.click(screen.getByRole('button', { name: /cancel/i }));
    await waitFor(() => {
      expect(openStates.at(-1)).toBe(false);
    });
  });
});

describe('SampleUpload — tab visibility based on platform', () => {
  it('shows only Upload + Record tabs in the browser (no Tauri)', () => {
    platformMeta.isTauri = false;
    systemState.isSupported = false;
    renderSampleUpload();

    expect(screen.getByRole('tab', { name: /upload/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /record/i })).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: /system audio/i })).not.toBeInTheDocument();
  });

  it('shows the System Audio tab when running on Tauri with system audio support', () => {
    platformMeta.isTauri = true;
    systemState.isSupported = true;
    renderSampleUpload();

    expect(screen.getByRole('tab', { name: /system audio/i })).toBeInTheDocument();
  });

  it('hides the System Audio tab on Tauri when system audio is not supported', () => {
    platformMeta.isTauri = true;
    systemState.isSupported = false;
    renderSampleUpload();

    expect(screen.queryByRole('tab', { name: /system audio/i })).not.toBeInTheDocument();
  });
});

describe('SampleUpload — record mode', () => {
  it('switching to the Record tab reveals the Start Recording control', async () => {
    const u = userEvent.setup();
    renderSampleUpload();

    await u.click(screen.getByRole('tab', { name: /record/i }));

    expect(await screen.findByRole('button', { name: /start recording/i })).toBeInTheDocument();
  });

  it('caps both recorders at 120 seconds', () => {
    renderSampleUpload();
    expect(lastAudioRecordingOpts.maxDurationSeconds).toBe(120);
    expect(lastSystemAudioOpts.maxDurationSeconds).toBe(120);
  });

  it('feeds a completed mic recording into the trimmer and toasts the user', async () => {
    const u = userEvent.setup();
    renderSampleUpload();
    await u.click(screen.getByRole('tab', { name: /record/i }));

    const blob = new Blob(['mic-bytes'], { type: 'audio/webm' });
    act(() => {
      lastAudioRecordingOpts.onRecordingComplete?.(blob, 12.5);
    });

    // After the recording completes, the trimmer is shown with the captured file.
    expect(await screen.findByTestId('audio-trimmer')).toBeInTheDocument();
    expect(screen.getByTestId('trimmer-file-name').textContent).toMatch(/^recording-\d+\.webm$/);

    // The component announces success via the toast.
    expect(toastCalls.some((t) => /recording complete/i.test(t.title ?? ''))).toBe(true);
  });

  it('feeds a completed system audio capture into the trimmer and toasts the user', async () => {
    platformMeta.isTauri = true;
    systemState.isSupported = true;
    const u = userEvent.setup();
    renderSampleUpload();
    await u.click(screen.getByRole('tab', { name: /system audio/i }));

    const blob = new Blob(['sys-bytes'], { type: 'audio/wav' });
    act(() => {
      lastSystemAudioOpts.onRecordingComplete?.(blob, 8);
    });

    expect(await screen.findByTestId('audio-trimmer')).toBeInTheDocument();
    expect(screen.getByTestId('trimmer-file-name').textContent).toMatch(/^system-audio-\d+\.wav$/);
    expect(toastCalls.some((t) => /system audio captured/i.test(t.title ?? ''))).toBe(true);
  });
});

describe('SampleUpload — recorder errors surface as toasts', () => {
  it('a mic recording error triggers a destructive toast', async () => {
    recordingState.error = 'microphone-denied';
    renderSampleUpload();

    await waitFor(() => {
      const toast = toastCalls.find((t) => /recording error/i.test(t.title ?? ''));
      expect(toast).toBeTruthy();
      expect(toast?.variant).toBe('destructive');
      expect(toast?.description).toBe('microphone-denied');
    });
  });

  it('a system audio error triggers a destructive toast', async () => {
    systemState.error = 'screen-capture-blocked';
    renderSampleUpload();

    await waitFor(() => {
      const toast = toastCalls.find((t) => /system audio capture error/i.test(t.title ?? ''));
      expect(toast).toBeTruthy();
      expect(toast?.variant).toBe('destructive');
      expect(toast?.description).toBe('screen-capture-blocked');
    });
  });
});

describe('SampleUpload — close while recording cancels the active capture', () => {
  it('cancels a mic recording in progress when the dialog closes', async () => {
    recordingState.isRecording = true;
    const u = userEvent.setup();
    const { openStates } = renderSampleUpload();

    // Switch to the Record tab so the dialog's `mode` reflects the active mic
    // capture; this is the same path users take when they hit Cancel while a
    // recording is running.
    await u.click(screen.getByRole('tab', { name: /record/i }));
    await u.click(screen.getByRole('button', { name: /cancel/i }));

    // Observable downstream outcomes:
    //   1. The dialog requests close (parent receives open=false).
    //   2. The audio player resources are released — the next render no longer
    //      offers a Play control because the form file has been cleared.
    await waitFor(() => {
      expect(openStates.at(-1)).toBe(false);
    });
    expect(screen.queryByRole('button', { name: /^play$/i })).not.toBeInTheDocument();

    // I/O boundary check: cancel & cleanup must be invoked with no arguments,
    // exactly once each, on the hooks SampleUpload owns. We assert the call
    // *shape* (not just a count) so a stray argument or extra invocation would
    // surface as a diff.
    const cancelShapes = (recordingCancel.mock.calls as unknown[][]).map(
      (call: unknown[]) => call,
    );
    expect(cancelShapes).toEqual([[]]);
    const cleanupShapes = (cleanupAudioMock.mock.calls as unknown[][]).map(
      (call: unknown[]) => call,
    );
    expect(cleanupShapes).toEqual([[]]);
  });

  it('cancels a system audio capture in progress when the dialog closes', async () => {
    platformMeta.isTauri = true;
    systemState.isSupported = true;
    systemState.isRecording = true;
    const u = userEvent.setup();
    const { openStates } = renderSampleUpload();

    // Switch to the System Audio tab so `mode === 'system'` when the user
    // cancels — that's the branch in handleOpenChange that aborts the system
    // capture.
    await u.click(screen.getByRole('tab', { name: /system audio/i }));
    await u.click(screen.getByRole('button', { name: /cancel/i }));

    // Observable: the dialog requests close.
    await waitFor(() => {
      expect(openStates.at(-1)).toBe(false);
    });

    // I/O boundary check on the system-capture hook: exactly one no-arg abort.
    const systemCancelShapes = (systemCancel.mock.calls as unknown[][]).map(
      (call: unknown[]) => call,
    );
    expect(systemCancelShapes).toEqual([[]]);
    // And we do NOT collaterally cancel the mic recorder when the system tab
    // was the active mode.
    const micCancelShapes = (recordingCancel.mock.calls as unknown[][]).map(
      (call: unknown[]) => call,
    );
    expect(micCancelShapes).toEqual([]);
  });
});

describe('SampleUpload — submission outcomes', () => {
  it('on a successful submission the dialog closes and a success toast fires', async () => {
    const u = userEvent.setup();
    const { openStates } = renderSampleUpload();

    // Upload + trim + fill text + submit.
    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    await u.upload(fileInput, new File(['raw'], 'orig.wav', { type: 'audio/wav' }));
    await screen.findByTestId('audio-trimmer');

    await u.type(screen.getByTestId('transcript-input'), 'A reference line.');
    await u.click(screen.getByTestId('trimmer-confirm'));
    await u.click(screen.getByRole('button', { name: /add sample/i }));

    await waitFor(() => {
      expect(submittedPayloads.length).toBe(1);
      expect(openStates.at(-1)).toBe(false);
    });
    expect(toastCalls.some((t) => /sample added/i.test(t.title ?? ''))).toBe(true);
  });

  it('on a failed submission shows a destructive error toast and keeps the dialog open', async () => {
    addSampleShouldReject = true;
    const u = userEvent.setup();
    const { openStates } = renderSampleUpload();

    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    await u.upload(fileInput, new File(['raw'], 'orig.wav', { type: 'audio/wav' }));
    await screen.findByTestId('audio-trimmer');
    await u.type(screen.getByTestId('transcript-input'), 'A reference line.');
    await u.click(screen.getByTestId('trimmer-confirm'));
    await u.click(screen.getByRole('button', { name: /add sample/i }));

    await waitFor(() => {
      const errToast = toastCalls.find((t) => t.variant === 'destructive' && /error/i.test(t.title ?? ''));
      expect(errToast).toBeTruthy();
      expect(errToast?.description).toBe('upload-failed');
    });
    // The dialog should NOT have requested close on a failed submission.
    expect(openStates.includes(false)).toBe(false);
  });

  it('disables the submit button while the mutation is pending', () => {
    addSampleIsPending = true;
    renderSampleUpload();

    const submit = screen.getByRole('button', { name: /uploading/i });
    expect(submit).toBeDisabled();
  });
});

describe('SampleUpload — playback wiring', () => {
  it('the Play control plays the confirmed (form) file once trimming is done', async () => {
    const u = userEvent.setup();
    renderSampleUpload();

    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    await u.upload(fileInput, new File(['raw'], 'raw.wav', { type: 'audio/wav' }));
    await screen.findByTestId('audio-trimmer');
    await u.click(screen.getByTestId('trimmer-confirm'));

    // Now there is a confirmed file in the form. Click Play.
    const playBtn = await screen.findByRole('button', { name: /play/i });
    await u.click(playBtn);

    expect(playPauseCalls.length).toBeGreaterThan(0);
    expect(playPauseCalls.at(-1)?.name).toBe('reference-trimmed.wav');
  });
});

describe('SampleUpload — reference transcript wiring', () => {
  it('typing in the transcript field updates the form value (next render reflects it)', async () => {
    const u = userEvent.setup();
    renderSampleUpload();

    const textarea = screen.getByTestId('transcript-input') as HTMLTextAreaElement;
    await u.type(textarea, 'hello');

    await waitFor(() => {
      expect(transcriptArgs.at(-1)?.text).toBe('hello');
    });
  });

  it('passes the profile language down to the transcript hook', () => {
    renderSampleUpload();
    expect(transcriptArgs.at(-1)?.language).toBe('en');
  });

  it('the transcript hook can write the reference text via its setText callback', async () => {
    renderSampleUpload();

    // Simulate the transcription pipeline auto-filling the reference text.
    act(() => {
      lastSetText?.('auto transcribed text');
    });

    await waitFor(() => {
      expect(transcriptArgs.at(-1)?.text).toBe('auto transcribed text');
    });
  });

  it('Re-transcribe with a confirmed clip re-runs transcription against that clip', async () => {
    const u = userEvent.setup();
    renderSampleUpload();

    // Upload, trim, confirm — this puts the trimmer's output file in the form.
    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    await u.upload(fileInput, new File(['raw'], 'raw.wav', { type: 'audio/wav' }));
    await screen.findByTestId('audio-trimmer');
    await u.click(screen.getByTestId('trimmer-confirm'));

    // Sanity: the confirmed clip is the one the transcript hook now sees.
    await waitFor(() => {
      expect(transcriptArgs.at(-1)?.file?.name).toBe('reference-trimmed.wav');
    });

    await u.click(screen.getByTestId('transcript-retranscribe'));

    // I/O boundary: retranscribe is the hook contract for "redo the transcription
    // for the currently confirmed clip". Assert the call shape (no arguments,
    // exactly one invocation) — this guards against silent regressions where
    // the wiring fires with stale state or fires twice.
    const retranscribeShapes = (retranscribeMock.mock.calls as unknown[][]).map(
      (call: unknown[]) => call,
    );
    expect(retranscribeShapes).toEqual([[]]);

    // Downstream observable: the form file did NOT change as a side effect of
    // re-transcribing (promotion path is only used when no clip is confirmed).
    expect(transcriptArgs.at(-1)?.file?.name).toBe('reference-trimmed.wav');
  });

  it('Re-transcribe without a confirmed clip promotes the trimmer selection into the form', async () => {
    const u = userEvent.setup();
    renderSampleUpload();

    // Upload a file so the trimmer is visible, but do NOT confirm.
    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    await u.upload(fileInput, new File(['raw'], 'raw.wav', { type: 'audio/wav' }));
    await screen.findByTestId('audio-trimmer');

    // Before the click, no confirmed file has reached useReferenceTranscript.
    expect(transcriptArgs.every((a) => a.file === null)).toBe(true);

    // Re-transcribe without confirming first — this should pull the current
    // trimmer selection and use it as the form file (observable: the next
    // render hands the trimmer-derived file to useReferenceTranscript).
    await u.click(screen.getByTestId('transcript-retranscribe'));

    await waitFor(() => {
      expect(transcriptArgs.at(-1)?.file?.name).toBe('ref-clip-from-getClip.wav');
    });

    // I/O boundary: the promotion path bypasses the transcript hook's
    // retranscribe entrypoint — instead, the new file flows through the
    // hook's args on the next render, which our `transcriptArgs` capture
    // above already proves. Assert the boundary call list is empty as a
    // structural check that no implicit retranscribe was triggered.
    const retranscribeShapes = (retranscribeMock.mock.calls as unknown[][]).map(
      (call: unknown[]) => call,
    );
    expect(retranscribeShapes).toEqual([]);
  });
});
