/// <reference types="@testing-library/jest-dom/vitest" />
/**
 * VoiceEditorTrim.test.tsx
 *
 * Verifies VoiceEditor's Clone tab via observable outcomes:
 *   - shows the AudioTrimmer once a sample is loaded
 *   - keeps Create disabled (and the cloneTooLong alert absent) for >30s samples
 *     until the user confirms a trim
 *   - reveals the post-clone assign/preview action row only after a trim is
 *     confirmed and the create flow succeeds
 *   - surfaces a cloneTooShort alert (and never reveals the action row) for
 *     samples under 3s
 *   - configures the recorder for the 120s maximum sample length
 */
import '@/i18n';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { VoiceEditor } from '@/components/BooksTab/VoiceEditor';

// ── Trimmer confirm stub ────────────────────────────────────────────────────
// Captures onConfirm so tests can call it with a trimmed file
let capturedTrimmerOnConfirm: ((trimmed: File, durationSec: number) => void) | null = null;

vi.mock('@/components/AudioTrimmer/AudioTrimmer', () => ({
  AudioTrimmer: ({ file: _file, onConfirm }: { file: File; onConfirm: (f: File, d: number) => void }) => {
    capturedTrimmerOnConfirm = onConfirm;
    return <div data-testid="audio-trimmer">AudioTrimmer stub</div>;
  },
}));

// ── Recording callback ──────────────────────────────────────────────────────
let capturedOnRecordingComplete: ((blob: Blob, duration?: number) => void) | null = null;
let capturedMaxDurationSeconds: number | undefined;

// ── Hook / store mocks ──────────────────────────────────────────────────────
const createClone = vi.fn().mockResolvedValue({ id: 'cloned-1', name: 'Mira (cloned)' });
const updateMutate = vi.fn();
const previewMutate = vi.fn();

vi.mock('@/stores/booksStore', () => ({
  useBooksStore: (s: any) =>
    s({
      selectedBookId: 'b1',
      selectedCharacterId: 'm',
      setView: vi.fn(),
      setSelectedCharacterId: vi.fn(),
    }),
}));

vi.mock('@/lib/hooks/useBooks', () => ({
  useCharacters: () => ({
    data: [{ id: 'm', name: 'Mira', color: '#34d399', dialogue_count: 142, confidence: 0.9 }],
  }),
  useUpdateCharacter: () => ({ mutate: updateMutate, isPending: false }),
  usePreviewCharacter: () => ({ mutate: previewMutate, isPending: false }),
  useCloneVoiceForCharacter: () => ({ mutateAsync: createClone, isPending: false }),
  useVoiceOptions: () => ({ data: { library: [], book: [], presets: [] } }),
  useSaveVoiceToLibrary: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock('@/lib/api/client', () => ({
  apiClient: { getBookAudioUrl: (id: string) => `http://localhost/audio/${id}` },
}));

vi.mock('@/lib/hooks/useReferenceTranscript', () => ({
  useReferenceTranscript: () => ({
    status: 'idle',
    isTranscribing: false,
    regeneratePrompt: false,
    retranscribe: vi.fn(),
    acceptRegenerate: vi.fn(),
    keepEdits: vi.fn(),
  }),
}));

vi.mock('@/lib/hooks/useAudioRecording', () => ({
  useAudioRecording: (opts: { maxDurationSeconds?: number; onRecordingComplete?: (blob: Blob, duration?: number) => void }) => {
    capturedOnRecordingComplete = opts.onRecordingComplete ?? null;
    capturedMaxDurationSeconds = opts.maxDurationSeconds;
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

beforeEach(() => {
  vi.clearAllMocks();
  capturedTrimmerOnConfirm = null;
  capturedOnRecordingComplete = null;
  capturedMaxDurationSeconds = undefined;
  createClone.mockResolvedValue({ id: 'cloned-1', name: 'Mira (cloned)' });
});

describe('VoiceEditor Clone tab — AudioTrimmer integration', () => {
  it('mounts AudioTrimmer after a file is uploaded', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="clone" />);

    const input = screen.getByTestId('clone-dropzone').querySelector('input[type=file]')!;
    await u.upload(
      input as HTMLElement,
      new File([new Uint8Array(16)], 'mira.wav', { type: 'audio/wav' }),
    );

    expect(screen.getByTestId('audio-trimmer')).toBeInTheDocument();
  });

  it('does NOT show cloneTooLong for a >30s sample, and gates Create until the trim is confirmed', () => {
    render(<VoiceEditor initialTab="clone" />);

    // Simulate a recording that completes with duration = 35s (previously too long)
    act(() => {
      capturedOnRecordingComplete?.(new Blob(['audio'], { type: 'audio/wav' }), 35);
    });

    // The trimmer is shown (no cloneTooLong error) and Create stays disabled
    // until the user confirms a trimmed window — so the raw, untrimmed file is
    // never uploaded (consistent with ProfileForm/SampleUpload).
    expect(screen.getByTestId('audio-trimmer')).toBeInTheDocument();
    expect(screen.getByTestId('create-clone-btn')).toBeDisabled();
  });

  it('reveals the assign/preview controls only after the user confirms a trim and creates the clone', async () => {
    const u = userEvent.setup();
    // The clone-create path only resolves for the trimmed file; if the raw file
    // were sent instead, the success branch (which renders assign/preview) would
    // never run and the test would fail on the assertion below.
    const trimmedFile = new File([new Uint8Array(4)], 'reference-123.wav', { type: 'audio/wav' });
    createClone.mockImplementation(async (args: { file: File }) => {
      if (args.file !== trimmedFile) {
        throw new Error('expected the trimmed file, got the raw upload');
      }
      return { id: 'cloned-1', name: 'Mira (cloned)' };
    });

    render(<VoiceEditor initialTab="clone" />);

    const input = screen.getByTestId('clone-dropzone').querySelector('input[type=file]')!;
    await u.upload(
      input as HTMLElement,
      new File([new Uint8Array(16)], 'raw.wav', { type: 'audio/wav' }),
    );

    // Before confirming a trim, Create is disabled and the post-clone action row
    // (assign-clone-btn) is not in the DOM.
    expect(screen.getByTestId('create-clone-btn')).toBeDisabled();
    expect(screen.queryByTestId('assign-clone-btn')).not.toBeInTheDocument();

    // User confirms a trimmed selection from the AudioTrimmer
    act(() => {
      capturedTrimmerOnConfirm?.(trimmedFile, 20);
    });

    await u.click(screen.getByTestId('create-clone-btn'));

    // Observable outcome: the post-clone action row appears (assign + preview),
    // and no error alert is shown — proving the trimmed file (not the raw one)
    // flowed through to a successful clone.
    expect(await screen.findByTestId('assign-clone-btn')).toBeInTheDocument();
    expect(screen.getByTestId('preview-voice-btn')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows cloneTooShort and does not reveal assign controls for a recorded sample under 3 seconds', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="clone" />);

    // Simulate a very short recording (1s — still too short)
    act(() => {
      capturedOnRecordingComplete?.(new Blob(['audio'], { type: 'audio/wav' }), 1);
    });

    // AudioTrimmer is now shown for the recorded file; confirm with the same short duration
    // so that validateDuration (which runs after the confirmedFile guard) fires.
    act(() => {
      capturedTrimmerOnConfirm?.(new File(['audio'], 'trimmed.wav', { type: 'audio/wav' }), 1);
    });

    await u.click(screen.getByTestId('create-clone-btn'));

    // Observable outcome: a "too short" error alert appears and the post-clone
    // action row (assign-clone-btn) never renders, so the clone did not complete.
    expect(await screen.findByRole('alert')).toHaveTextContent(/too short/i);
    expect(screen.queryByTestId('assign-clone-btn')).not.toBeInTheDocument();
  });

  it('recorder maxDurationSeconds is 120 (not 29)', () => {
    render(<VoiceEditor initialTab="clone" />);
    expect(capturedMaxDurationSeconds).toBe(120);
  });
});
