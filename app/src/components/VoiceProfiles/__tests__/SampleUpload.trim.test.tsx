/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { SampleUpload } from '@/components/VoiceProfiles/SampleUpload';

// ---- mocks ----

// Mock AudioTrimmer as a stub exposing a confirm button that fires onConfirm
vi.mock('@/components/AudioTrimmer/AudioTrimmer', () => ({
  AudioTrimmer: ({ file, onConfirm }: { file: File; onConfirm: (f: File, d: number) => void }) => {
    return (
      <div data-testid="audio-trimmer">
        <span data-testid="trimmer-file-name">{file.name}</span>
        <button
          data-testid="trimmer-confirm"
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
}));

// Capture submissions to addSample at the hook boundary so we can assert on the
// payload that left the component as an observable outcome of the form lifecycle.
const submittedPayloads: Array<{ profileId: string; file: File; referenceText: string }> = [];
const addSampleMutateAsync = vi.fn(async (payload: { profileId: string; file: File; referenceText: string }) => {
  submittedPayloads.push(payload);
  return {};
});

vi.mock('@/lib/hooks/useProfiles', () => ({
  useAddSample: () => ({ mutateAsync: addSampleMutateAsync, isPending: false }),
  useProfile: () => ({ data: { id: 'p1', name: 'Test Profile', language: 'en' } }),
}));

vi.mock('@/lib/hooks/useAudioPlayer', () => ({
  useAudioPlayer: () => ({
    isPlaying: false,
    playPause: vi.fn(),
    cleanup: vi.fn(),
  }),
}));

// Record the options each recorder hook was constructed with so the test can
// observe the duration cap the component actually enforces on its recorders.
const audioRecordingOpts: Array<{ maxDurationSeconds?: number }> = [];
vi.mock('@/lib/hooks/useAudioRecording', () => ({
  useAudioRecording: (opts: { onRecordingComplete?: (blob: Blob, duration?: number) => void; maxDurationSeconds?: number }) => {
    audioRecordingOpts.push({ maxDurationSeconds: opts.maxDurationSeconds });
    return {
      isRecording: false,
      duration: 0,
      error: null,
      startRecording: vi.fn(),
      stopRecording: vi.fn(),
      cancelRecording: vi.fn(),
    };
  },
}));

const systemAudioOpts: Array<{ maxDurationSeconds?: number }> = [];
vi.mock('@/lib/hooks/useSystemAudioCapture', () => ({
  useSystemAudioCapture: (opts: { maxDurationSeconds?: number }) => {
    systemAudioOpts.push({ maxDurationSeconds: opts.maxDurationSeconds });
    return {
      isRecording: false,
      duration: 0,
      error: null,
      isSupported: false,
      startRecording: vi.fn(),
      stopRecording: vi.fn(),
      cancelRecording: vi.fn(),
    };
  },
}));

vi.mock('@/platform/PlatformContext', () => ({
  usePlatform: () => ({
    metadata: { isTauri: false },
  }),
}));

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// Single module-scope mock for useReferenceTranscript for the whole file.
const hookArgs: Array<{ file: File | null }> = [];
vi.mock('@/lib/hooks/useReferenceTranscript', () => ({
  useReferenceTranscript: (args: { file: File | null; setText: (v: string) => void }) => {
    hookArgs.push({ file: args.file });
    // NOTE: deliberately does NOT call args.setText — the field stays user-controlled,
    // so typed reference text is never overwritten by the mock.
    return {
      status: args.file ? 'filled' : 'idle',
      isTranscribing: false,
      regeneratePrompt: false,
      retranscribe: vi.fn(),
      acceptRegenerate: vi.fn(),
      keepEdits: vi.fn(),
    };
  },
}));

// ---- helpers ----

function renderSampleUpload() {
  const openStates: boolean[] = [];
  const onOpenChange = (next: boolean) => {
    openStates.push(next);
  };
  return {
    openStates,
    ...render(<SampleUpload profileId="p1" open={true} onOpenChange={onOpenChange} />),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  submittedPayloads.length = 0;
  audioRecordingOpts.length = 0;
  systemAudioOpts.length = 0;
  hookArgs.length = 0;
});

// ---- tests ----

describe('SampleUpload — AudioTrimmer integration', () => {
  it('shows the trimmer (and no duration rejection) when a long file is selected', async () => {
    const u = userEvent.setup();
    renderSampleUpload();

    // Find the file input inside upload tab
    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    expect(fileInput).toBeTruthy();

    // Upload a long file (200s) — should NOT be rejected, should go to trimmer
    const longFile = new File(['audio-data'.repeat(1000)], 'interview.wav', {
      type: 'audio/wav',
    });
    await u.upload(fileInput, longFile);

    // AudioTrimmer should be shown, not a duration error
    expect(await screen.findByTestId('audio-trimmer')).toBeInTheDocument();
    expect(screen.queryByText(/too long/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/duration/i)).not.toBeInTheDocument();
  });

  it('submits the trimmed clip (not the originally selected raw file)', async () => {
    const u = userEvent.setup();
    const { openStates } = renderSampleUpload();

    // Select a file
    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    const rawFile = new File(['raw-audio-data'], 'long-recording.wav', { type: 'audio/wav' });
    await u.upload(fileInput, rawFile);

    // Trimmer appears
    await screen.findByTestId('audio-trimmer');

    // Fill in reference text (required by the form schema)
    const referenceTextarea = screen.getByTestId('transcript-input');
    await u.type(referenceTextarea, 'Hello world this is my reference text');

    // Confirm the trim
    await u.click(screen.getByTestId('trimmer-confirm'));

    // Submit the form
    await u.click(screen.getByRole('button', { name: /add sample/i }));

    // Observable outcomes:
    //  (a) the dialog closes (the component requests open=false on success)
    //  (b) the payload that left the component carries the trimmed file, NOT the raw one
    await waitFor(() => {
      expect(openStates.at(-1)).toBe(false);
    });
    const submitted = submittedPayloads.at(-1);
    expect(submitted?.profileId).toBe('p1');
    expect(submitted?.file.name).toBe('reference-trimmed.wav');
    expect(submitted?.file.name).not.toBe('long-recording.wav');
    expect(submitted?.referenceText).toBe('Hello world this is my reference text');
  });

  it('replaces the trimmer source when a different file is selected', async () => {
    const u = userEvent.setup();
    renderSampleUpload();

    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;

    // First file
    const file1 = new File(['data1'], 'first.wav', { type: 'audio/wav' });
    await u.upload(fileInput, file1);
    expect(await screen.findByTestId('trimmer-file-name')).toHaveTextContent('first.wav');

    // Second file (re-select)
    const file2 = new File(['data2'], 'second.wav', { type: 'audio/wav' });
    await u.upload(fileInput, file2);
    await waitFor(() => {
      expect(screen.getByTestId('trimmer-file-name')).toHaveTextContent('second.wav');
    });
  });

  it('caps recordable duration at 120 seconds for both mic and system recorders', () => {
    renderSampleUpload();

    // The component must wire a 120s cap into both recorder hooks so users
    // cannot capture clips longer than the model accepts.
    expect(audioRecordingOpts[audioRecordingOpts.length - 1]?.maxDurationSeconds).toBe(120);
    expect(systemAudioOpts[systemAudioOpts.length - 1]?.maxDurationSeconds).toBe(120);
  });

  it('drives transcription off the trimmed clip once the trimmer confirms', async () => {
    const u = userEvent.setup();
    renderSampleUpload();

    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    const rawFile = new File(['raw-audio-data'], 'long-recording.wav', { type: 'audio/wav' });
    await u.upload(fileInput, rawFile);

    // Trimmer appears
    await screen.findByTestId('audio-trimmer');

    // Before confirmation, useReferenceTranscript should NOT have seen the trimmed
    // file — it should still be receiving null (or the raw file is not promoted).
    expect(hookArgs.some((a) => a.file?.name === 'reference-trimmed.wav')).toBe(false);

    // Confirm the trim
    await u.click(screen.getByTestId('trimmer-confirm'));

    // After confirmation, the trimmed clip must flow into the transcription pipeline.
    await waitFor(() =>
      expect(hookArgs.some((a) => a.file?.name === 'reference-trimmed.wav')).toBe(true),
    );
  });

  it('refuses to submit, and keeps the dialog open, when the reference text is empty', async () => {
    const u = userEvent.setup();
    const { openStates } = renderSampleUpload();

    // Pick a file and confirm the trim, but leave the reference text untouched.
    const fileInput = document.querySelector('input[type=file]') as HTMLInputElement;
    const rawFile = new File(['raw-audio-data'], 'long-recording.wav', { type: 'audio/wav' });
    await u.upload(fileInput, rawFile);
    await screen.findByTestId('audio-trimmer');
    await u.click(screen.getByTestId('trimmer-confirm'));

    // Submit the form without filling the required reference-text field.
    await u.click(screen.getByRole('button', { name: /add sample/i }));

    // Give the form's async submit handler a tick to run (or, in this case,
    // to be short-circuited by the resolver). We then assert the user-visible
    // outcomes: no payload crossed the addSample boundary, and the dialog
    // never requested closing so the user stays on the form to fix it.
    await new Promise((r) => setTimeout(r, 50));
    expect(submittedPayloads).toHaveLength(0);
    expect(openStates).not.toContain(false);
  });
});
