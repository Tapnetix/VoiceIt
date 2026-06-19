/// <reference types="@testing-library/jest-dom/vitest" />
//
// VoiceEditor — extra coverage for sub-component branches.
//
// This file targets behaviours not exercised elsewhere: the shared
// PreviewPlayer's play/pause cycle, LibraryTabBody's search and filter
// inputs, VoiceCard's preview button and keyboard-activation paths, the
// CloneTabBody dropzone (file-select + drag/drop), and the profile-kind
// branches of handlePreviewCandidate / handleAssignCandidate.
//
// Quality bar (SC8): no spy call-count assertions. Boundary requests that
// cross the hook layer are captured into module-level variables and
// asserted as data; UI state changes are observed against the rendered DOM.
//
import '@/i18n';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { VoiceEditor } from '@/components/BooksTab/VoiceEditor';
import { useBooksStore } from '@/stores/booksStore';

// ─── Boundary capture ─────────────────────────────────────────────────────────

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

// Allow tests to vary the voice-options dataset that drives library/book/preset
// filtering. Mutated in beforeEach and per test.
let voiceOptionsData: {
  library: Array<Record<string, unknown>>;
  book: Array<Record<string, unknown>>;
  presets: Array<Record<string, unknown>>;
} = { library: [], book: [], presets: [] };

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
        voice_type: 'designed',
      },
    ],
  }),
  useUpdateCharacter: () => ({
    mutate: (
      payload: UpdatePayload,
      opts?: { onSuccess?: () => void; onError?: (err: Error) => void },
    ) => {
      lastUpdatePayload = payload;
      opts?.onSuccess?.();
    },
    isPending: false,
  }),
  usePreviewCharacter: () => ({
    mutate: (payload: PreviewPayload) => {
      lastPreviewPayload = payload;
    },
    data: { generation_id: 'gen-1', audio_path: '/audio/gen-1' },
    isPending: false,
  }),
  useVoiceOptions: () => ({ data: voiceOptionsData }),
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

// Stub AudioTrimmer so the dropzone-file-load tests don't depend on it.
vi.mock('@/components/AudioTrimmer/AudioTrimmer', () => ({
  AudioTrimmer: ({ file }: { file: File }) => (
    <div data-testid="audio-trimmer">{file.name}</div>
  ),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('VoiceEditor — PreviewPlayer play/pause', () => {
  beforeEach(() => {
    lastUpdatePayload = null;
    lastPreviewPayload = null;
    voiceOptionsData = { library: [], book: [], presets: [] };
    useBooksStore.getState().reset();
    useBooksStore.getState().setSelectedBookId('b1');
    useBooksStore.getState().setSelectedCharacterId('m');
    useBooksStore.getState().setView('voice-editor');
  });

  it('PreviewPlayer toggles from play (▶) to pause (⏸) after a click when audio is available', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor />);
    const player = screen.getByTestId('preview-player');
    const playBtn = player.querySelector('button') as HTMLButtonElement;
    expect(playBtn).not.toBeDisabled();
    expect(playBtn.textContent).toContain('▶');
    await u.click(playBtn);
    // After clicking, the player should show the pause glyph in the same button.
    expect(player.querySelector('button')?.textContent).toContain('⏸');
  });

  it('PreviewPlayer toggles back from pause (⏸) to play (▶) on a second click', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor />);
    const player = screen.getByTestId('preview-player');
    const playBtn = player.querySelector('button') as HTMLButtonElement;
    await u.click(playBtn);
    expect(player.querySelector('button')?.textContent).toContain('⏸');
    await u.click(player.querySelector('button') as HTMLButtonElement);
    expect(player.querySelector('button')?.textContent).toContain('▶');
  });
});

describe('VoiceEditor — Library filters & VoiceCard interactions', () => {
  beforeEach(() => {
    lastUpdatePayload = null;
    lastPreviewPayload = null;
    voiceOptionsData = {
      library: [
        { id: 'lib-female', name: 'Aria', voice_type: 'designed', gender: 'female', age_range: '30s' },
        { id: 'lib-male', name: 'Borin', voice_type: 'designed', gender: 'male', age_range: '50s' },
      ],
      book: [
        { id: 'bk-1', name: 'Captain', voice_type: 'preset', gender: 'male' },
      ],
      presets: [
        { voice_id: 'preset-en', name: 'Heart', engine: 'kokoro', gender: 'female', language: 'en' },
        { voice_id: 'preset-fr', name: 'Lune', engine: 'kokoro', gender: 'female', language: 'fr' },
      ],
    };
    useBooksStore.getState().reset();
    useBooksStore.getState().setSelectedBookId('b1');
    useBooksStore.getState().setSelectedCharacterId('m');
    useBooksStore.getState().setView('voice-editor');
  });

  it('search input narrows the library list to names matching the typed query', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    const search = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    await u.type(search, 'Aria');
    // Aria is in the library section; Borin should be filtered out.
    expect(within(screen.getByTestId('library-voices')).getByText('Aria')).toBeInTheDocument();
    expect(within(screen.getByTestId('library-voices')).queryByText('Borin')).not.toBeInTheDocument();
  });

  it('gender filter set to male hides female library entries', async () => {
    render(<VoiceEditor initialTab="library" />);
    const genderSelect = screen.getByLabelText(/gender/i) as HTMLSelectElement;
    fireEvent.change(genderSelect, { target: { value: 'male' } });
    expect(within(screen.getByTestId('library-voices')).queryByText('Aria')).not.toBeInTheDocument();
    expect(within(screen.getByTestId('library-voices')).getByText('Borin')).toBeInTheDocument();
  });

  it('accent filter only shows presets whose language matches the selected accent', async () => {
    render(<VoiceEditor initialTab="library" />);
    const accent = screen.getByTestId('accent-filter') as HTMLSelectElement;
    fireEvent.change(accent, { target: { value: 'fr' } });
    const presetGrid = screen.getByTestId('preset-voices');
    expect(within(presetGrid).queryByText('Heart')).not.toBeInTheDocument();
    expect(within(presetGrid).getByText('Lune')).toBeInTheDocument();
  });

  it('accent dropdown lists each unique preset language as an option', () => {
    render(<VoiceEditor initialTab="library" />);
    const accent = screen.getByTestId('accent-filter') as HTMLSelectElement;
    const optionValues = Array.from(accent.options).map((o) => o.value);
    expect(optionValues).toContain('en');
    expect(optionValues).toContain('fr');
  });

  it('clicking a VoiceCard preview button (▶) requests a preview for that profile id', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    const libraryGrid = screen.getByTestId('library-voices');
    // The preview button on the card carries an aria-label "Preview Aria".
    const ariaBtn = within(libraryGrid).getByLabelText(/Preview Aria/i);
    await u.click(ariaBtn);
    expect(lastPreviewPayload).not.toBeNull();
    expect(lastPreviewPayload?.charId).toBe('m');
    expect(lastPreviewPayload?.data.profile_id).toBe('lib-female');
  });

  it('a VoiceCard becomes selected via the Enter key (keyboard activation)', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    const libraryGrid = screen.getByTestId('library-voices');
    const ariaCard = within(libraryGrid).getByText('Aria').closest('[role="button"]') as HTMLElement;
    ariaCard.focus();
    await u.keyboard('{Enter}');
    // Once selected, the assign button enables and the status line reflects the name.
    expect(screen.getByTestId('assign-selected-btn')).not.toBeDisabled();
    expect(screen.getByTestId('voice-panel-library')).toHaveTextContent('Aria');
  });

  it('the status line shows the engine suffix for a selected preset (e.g. "Heart (kokoro)")', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    await u.click(within(screen.getByTestId('preset-voices')).getByText('Heart'));
    expect(screen.getByTestId('voice-panel-library')).toHaveTextContent(/Heart \(kokoro\)/);
  });

  it('assigning a book-section voice sends its id as profile_id', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    await u.click(within(screen.getByTestId('book-voices')).getByText('Captain'));
    await u.click(screen.getByTestId('assign-selected-btn'));
    expect(lastUpdatePayload?.data.profile_id).toBe('bk-1');
    expect(lastUpdatePayload?.data.preset_voice_id).toBeUndefined();
  });

  it('previewing a library profile through the action row carries the profile id', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="library" />);
    await u.click(within(screen.getByTestId('library-voices')).getByText('Aria'));
    await u.click(screen.getByTestId('preview-voice-btn'));
    expect(lastPreviewPayload?.data.profile_id).toBe('lib-female');
    expect(lastPreviewPayload?.data.preset_voice_id).toBeUndefined();
  });
});

describe('VoiceEditor — Clone dropzone file loading', () => {
  beforeEach(() => {
    lastUpdatePayload = null;
    lastPreviewPayload = null;
    voiceOptionsData = { library: [], book: [], presets: [] };
    useBooksStore.getState().reset();
    useBooksStore.getState().setSelectedBookId('b1');
    useBooksStore.getState().setSelectedCharacterId('m');
    useBooksStore.getState().setView('voice-editor');
  });

  it('selecting a file via the input updates the dropzone to show the file name', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="clone" />);
    const dropzone = screen.getByTestId('clone-dropzone');
    const input = dropzone.querySelector('input[type=file]') as HTMLInputElement;
    const file = new File([new Uint8Array(8)], 'sample.wav', { type: 'audio/wav' });
    await u.upload(input, file);
    expect(dropzone).toHaveTextContent('sample.wav');
  });

  it('dragging a file onto the dropzone loads it and renders the file name', () => {
    render(<VoiceEditor initialTab="clone" />);
    const dropzone = screen.getByTestId('clone-dropzone');
    const file = new File([new Uint8Array(8)], 'dropped.wav', { type: 'audio/wav' });
    fireEvent.dragOver(dropzone);
    fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });
    expect(dropzone).toHaveTextContent('dropped.wav');
  });

  it('dragLeave clears the drag-over visual highlight state', () => {
    render(<VoiceEditor initialTab="clone" />);
    const dropzone = screen.getByTestId('clone-dropzone');
    fireEvent.dragOver(dropzone);
    expect(dropzone.className).toMatch(/border-primary/);
    fireEvent.dragLeave(dropzone);
    expect(dropzone.className).not.toMatch(/border-primary/);
  });

  it('dropping with no files in the transfer leaves the dropzone empty', () => {
    render(<VoiceEditor initialTab="clone" />);
    const dropzone = screen.getByTestId('clone-dropzone');
    fireEvent.drop(dropzone, { dataTransfer: { files: [] } });
    // No file → still showing the drop-here placeholder text from i18n.
    expect(dropzone.querySelector('input[type=file]')).toBeInTheDocument();
    // The AudioTrimmer is only mounted once a sampleFile exists.
    expect(screen.queryByTestId('audio-trimmer')).not.toBeInTheDocument();
  });

  it('once a file is loaded, the AudioTrimmer is mounted underneath the dropzone', async () => {
    const u = userEvent.setup();
    render(<VoiceEditor initialTab="clone" />);
    const input = screen.getByTestId('clone-dropzone').querySelector('input[type=file]') as HTMLInputElement;
    const file = new File([new Uint8Array(8)], 'mounted.wav', { type: 'audio/wav' });
    await u.upload(input, file);
    expect(screen.getByTestId('audio-trimmer')).toHaveTextContent('mounted.wav');
  });
});
