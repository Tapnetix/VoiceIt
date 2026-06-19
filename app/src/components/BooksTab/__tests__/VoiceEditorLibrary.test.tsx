/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { VoiceEditor } from '@/components/BooksTab/VoiceEditor';
import { useBooksStore } from '@/stores/booksStore';

// ─── Hook-boundary capture ────────────────────────────────────────────────────
//
// The hook layer is the system's boundary to the server. We capture the
// payloads the component sends through it into module-level variables so the
// tests can inspect them as outcomes (the request shape that crossed the
// boundary) rather than asserting on call counts of internal spies. The store
// is the real one, so view/selection changes show up as observable state.

interface UpdatePayload {
  bookId: string;
  charId: string;
  data: {
    design_prompt?: string;
    profile_id?: string;
    preset_voice_id?: string;
  };
}

interface PreviewPayload {
  charId: string;
  data: {
    design_prompt?: string;
    profile_id?: string;
    preset_voice_id?: string;
  };
}

let lastUpdatePayload: UpdatePayload | null = null;
let lastPreviewPayload: PreviewPayload | null = null;
let updateOutcome: 'success' | 'pending' = 'success';

vi.mock('@/lib/hooks/useBooks', () => ({
  useCharacters: () => ({
    data: [
      {
        id: 'm',
        name: 'Mira',
        color: '#34d399',
        dialogue_count: 142,
        confidence: 0.9,
        vocal_description: 'warm, resolute',
      },
    ],
  }),
  useUpdateCharacter: () => ({
    mutate: (
      payload: UpdatePayload,
      opts?: { onSuccess?: () => void; onError?: (err: Error) => void },
    ) => {
      lastUpdatePayload = payload;
      if (updateOutcome === 'success') opts?.onSuccess?.();
    },
    isPending: false,
  }),
  usePreviewCharacter: () => ({
    mutate: (payload: PreviewPayload) => {
      lastPreviewPayload = payload;
    },
    isPending: false,
  }),
  useVoiceOptions: () => ({
    data: {
      library: [{ id: 'lib1', name: 'Gravelly Narrator', voice_type: 'designed' }],
      book: [{ id: 'bk1', name: 'Holt (designed)', voice_type: 'designed' }],
      presets: [{ voice_id: 'af_heart', name: 'Heart', engine: 'kokoro', gender: 'female' }],
    },
  }),
  useSaveVoiceToLibrary: () => ({ mutate: vi.fn(), isPending: false }),
  useCloneVoiceForCharacter: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@/lib/hooks/useAudioRecording', () => ({
  useAudioRecording: () => ({
    isRecording: false,
    duration: 0,
    error: null,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    cancelRecording: vi.fn(),
  }),
}));

vi.mock('@/lib/api/client', () => ({
  apiClient: {
    getBookAudioUrl: (id: string) => `http://localhost/audio/${id}`,
  },
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

describe('VoiceEditor (Library)', () => {
  beforeEach(() => {
    lastUpdatePayload = null;
    lastPreviewPayload = null;
    updateOutcome = 'success';
    useBooksStore.getState().reset();
    useBooksStore.getState().setSelectedBookId('b1');
    useBooksStore.getState().setSelectedCharacterId('m');
    useBooksStore.getState().setView('voice-editor');
  });

  it('lists library, book, and preset voices in their respective sections', () => {
    render(<VoiceEditor initialTab="library" />);
    expect(
      within(screen.getByTestId('library-voices')).getByText('Gravelly Narrator'),
    ).toBeInTheDocument();
    expect(within(screen.getByTestId('book-voices')).getByText(/Holt/)).toBeInTheDocument();
    expect(within(screen.getByTestId('preset-voices')).getByText('Heart')).toBeInTheDocument();
  });

  it('assigning a selected preset sends the preset id for the current book and character', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    await u.click(within(screen.getByTestId('preset-voices')).getByText('Heart'));
    await u.click(screen.getByTestId('assign-selected-btn'));
    expect(lastUpdatePayload).not.toBeNull();
    expect(lastUpdatePayload?.bookId).toBe('b1');
    expect(lastUpdatePayload?.charId).toBe('m');
    expect(lastUpdatePayload?.data.preset_voice_id).toBe('af_heart');
    // No profile_id should leak into a preset assignment.
    expect(lastUpdatePayload?.data.profile_id).toBeUndefined();
  });

  it('a successful preset assignment returns the books view to overview', async () => {
    const u = userEvent.setup();
    updateOutcome = 'success';
    render(<VoiceEditor initialTab="library" />);
    await u.click(within(screen.getByTestId('preset-voices')).getByText('Heart'));
    await u.click(screen.getByTestId('assign-selected-btn'));
    expect(useBooksStore.getState().view).toBe('overview');
  });

  it('assigning a library voice sends its profile id, not a preset id', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    await u.click(within(screen.getByTestId('library-voices')).getByText('Gravelly Narrator'));
    await u.click(screen.getByTestId('assign-selected-btn'));
    expect(lastUpdatePayload?.data.profile_id).toBe('lib1');
    expect(lastUpdatePayload?.data.preset_voice_id).toBeUndefined();
  });

  it('assign-selected-btn is disabled until the user picks a voice', () => {
    render(<VoiceEditor initialTab="library" />);
    expect(screen.getByTestId('assign-selected-btn')).toBeDisabled();
  });

  it('shows the selected voice name in the status line once chosen', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    await u.click(within(screen.getByTestId('preset-voices')).getByText('Heart'));
    expect(screen.getByTestId('voice-panel-library')).toHaveTextContent('Heart');
  });

  it('previewing a preset from the action row carries the preset voice id', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    await u.click(within(screen.getByTestId('preset-voices')).getByText('Heart'));
    await u.click(screen.getByTestId('preview-voice-btn'));
    expect(lastPreviewPayload?.charId).toBe('m');
    expect(lastPreviewPayload?.data.preset_voice_id).toBe('af_heart');
  });
});
