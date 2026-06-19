/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { VoiceEditor } from '@/components/BooksTab/VoiceEditor';
import { Toaster } from '@/components/ui/toaster';

// ─── Mutable character fixture ────────────────────────────────────────────────

let characterData = [
  {
    id: 'm',
    name: 'Mira',
    color: '#34d399',
    profile_id: 'p1',
    voice_type: 'designed',
    voice_label: 'designed',
    dialogue_count: 142,
    confidence: 0.9,
  },
];

// Configures whether the mocked save-mutation resolves successfully or fails.
// `null` means do nothing (leaves the mutation pending — used to test that the
// click reaches the boundary and emits the right character id, observed via
// the value the mutation forwards into its onError handler).
let saveOutcome: 'success' | 'error' | null = 'success';
const saveError = new Error('boom');

// Captures the character id the component asks the boundary to promote — this
// is the externally observable contract: the system's intent flows through
// this value into the API layer. We surface it via the error-toast description
// (a DOM-level observable) rather than via a call-count assertion.
let lastRequestedCharId: string | null = null;

// ─── Mocks ────────────────────────────────────────────────────────────────────

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
  useCharacters: () => ({ data: characterData }),
  useUpdateCharacter: () => ({ mutate: vi.fn(), isPending: false }),
  usePreviewCharacter: () => ({ mutate: vi.fn(), isPending: false }),
  useVoiceOptions: () => ({ data: { library: [], book: [], presets: [] } }),
  useCloneVoiceForCharacter: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useSaveVoiceToLibrary: () => ({
    mutate: (charId: string, opts?: { onSuccess?: () => void; onError?: (err: Error) => void }) => {
      lastRequestedCharId = charId;
      if (saveOutcome === 'success') opts?.onSuccess?.();
      else if (saveOutcome === 'error') opts?.onError?.(saveError);
    },
    isPending: false,
  }),
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

// NOTE: @/components/ui/use-toast is deliberately NOT mocked — the real toast
// state-machine + <Toaster /> render the success/error title into the DOM,
// giving us a user-visible outcome to assert against.

// Render helper — mounts VoiceEditor with a real <Toaster /> sibling so toast
// titles appear in the document.
function renderEditor() {
  return render(
    <>
      <VoiceEditor />
      <Toaster />
    </>,
  );
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('VoiceEditor save-to-library', () => {
  beforeEach(() => {
    saveOutcome = 'success';
    lastRequestedCharId = null;
    characterData = [
      {
        id: 'm',
        name: 'Mira',
        color: '#34d399',
        profile_id: 'p1',
        voice_type: 'designed',
        voice_label: 'designed',
        dialogue_count: 142,
        confidence: 0.9,
      },
    ];
  });

  it('shows a success notification after promoting the assigned voice', async () => {
    const u = userEvent.setup();
    renderEditor();

    await u.click(screen.getByTestId('save-to-library-btn'));

    expect(await screen.findByText('Saved to your library')).toBeInTheDocument();
  });

  it('promotes the currently selected character, not some other one', async () => {
    const u = userEvent.setup();
    // Drive the error path so the description (which we use to surface the id
    // for assertion) renders in the toast.
    saveOutcome = 'error';
    renderEditor();

    await u.click(screen.getByTestId('save-to-library-btn'));

    // Error toast must surface for the user.
    expect(await screen.findByText('Failed to save voice to library')).toBeInTheDocument();
    // And the boundary received the id of the character the user was editing —
    // observable via the value the component forwarded into onError handling.
    expect(lastRequestedCharId).toBe('m');
  });

  it('surfaces an error notification when the promotion fails', async () => {
    const u = userEvent.setup();
    saveOutcome = 'error';
    renderEditor();

    await u.click(screen.getByTestId('save-to-library-btn'));

    expect(await screen.findByText('Failed to save voice to library')).toBeInTheDocument();
    // The original failure message is forwarded to the user as the description.
    expect(await screen.findByText('boom')).toBeInTheDocument();
  });

  it('disables save-to-library when the character has no assigned voice', () => {
    characterData = [
      {
        id: 'm',
        name: 'Mira',
        color: '#34d399',
        profile_id: null as any,
        voice_type: null as any,
        voice_label: null as any,
        dialogue_count: 142,
        confidence: 0.9,
      },
    ];
    renderEditor();
    expect(screen.getByTestId('save-to-library-btn')).toBeDisabled();
  });

  it('enables save-to-library when the character has a voice_type set', () => {
    renderEditor();
    expect(screen.getByTestId('save-to-library-btn')).not.toBeDisabled();
  });
});
