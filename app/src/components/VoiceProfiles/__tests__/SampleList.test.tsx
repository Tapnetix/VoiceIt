/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// ── apiClient HTTP boundary mock ──────────────────────────────────────────────
// SampleList consumes the real useProfileSamples / useDeleteSample /
// useUpdateSample hooks. By stubbing the api client (the HTTP boundary) and
// letting react-query run for real, we can observe component behaviour
// (rendered samples, edit / delete flows, toast calls) through the same path
// the production code would take.

type Sample = {
  id: string;
  profile_id: string;
  audio_path: string;
  reference_text: string;
};

let samplesFixture: Sample[] = [];
let listSamplesRejection: Error | null = null;
let listSamplesNeverResolves = false;

const listProfileSamplesFn = vi.fn(async (_profileId: string) => {
  if (listSamplesNeverResolves) {
    return new Promise<Sample[]>(() => {});
  }
  if (listSamplesRejection) {
    throw listSamplesRejection;
  }
  return samplesFixture;
});

const deleteProfileSampleFn = vi.fn(async (sampleId: string) => {
  samplesFixture = samplesFixture.filter((s) => s.id !== sampleId);
});

const updateProfileSampleFn = vi.fn(async (sampleId: string, referenceText: string) => {
  const idx = samplesFixture.findIndex((s) => s.id === sampleId);
  if (idx === -1) throw new Error('Sample not found');
  samplesFixture[idx] = { ...samplesFixture[idx], reference_text: referenceText };
  return samplesFixture[idx];
});

const getSampleUrlFn = vi.fn((sampleId: string) => `http://api.test/samples/${sampleId}`);

vi.mock('@/lib/api/client', () => ({
  apiClient: {
    listProfileSamples: (profileId: string) => listProfileSamplesFn(profileId),
    deleteProfileSample: (sampleId: string) => deleteProfileSampleFn(sampleId),
    updateProfileSample: (sampleId: string, text: string) =>
      updateProfileSampleFn(sampleId, text),
    getSampleUrl: (sampleId: string) => getSampleUrlFn(sampleId),
  },
}));

// ── Toast capture ─────────────────────────────────────────────────────────────
// Observe toast notifications through the public hook surface. This lets the
// tests assert on visible feedback for invalid edits and update failures
// without inspecting internal collaborators. The vi.mock factory is hoisted
// above the test file's top-level code, so the spy is created via vi.hoisted
// to keep it valid at the moment the mock is evaluated.
const { toastFn } = vi.hoisted(() => ({ toastFn: vi.fn() }));
vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({ toast: toastFn }),
  toast: toastFn,
}));

// ── SampleUpload stub ─────────────────────────────────────────────────────────
// SampleUpload is a heavy peer that wires platform/audio capture, react-hook-
// form, the Whisper transcription hook and the audio trimmer. None of that is
// in the contract of SampleList, which only forwards `profileId` and the
// open/close handlers. Stub it with a minimal probe that surfaces the props
// we care about — matching the project's existing pattern (BooksTab.test.tsx,
// ProfileForm.trim.test.tsx) for stubbing peer dialogs.
vi.mock('@/components/VoiceProfiles/SampleUpload', () => ({
  SampleUpload: ({
    profileId,
    open,
    onOpenChange,
  }: {
    profileId: string;
    open: boolean;
    onOpenChange: (open: boolean) => void;
  }) => (
    <div
      data-testid="sample-upload-stub"
      data-profile-id={profileId}
      data-open={open ? 'true' : 'false'}
    >
      <button type="button" data-testid="sample-upload-close" onClick={() => onOpenChange(false)}>
        close-upload
      </button>
    </div>
  ),
}));

import { SampleList } from '@/components/VoiceProfiles/SampleList';

function buildSample(overrides: Partial<Sample> = {}): Sample {
  return {
    id: 'sample-1',
    profile_id: 'profile-1',
    audio_path: '/audio/sample-1.wav',
    reference_text: 'The quick brown fox jumps over the lazy dog.',
    ...overrides,
  };
}

function renderList(profileId = 'profile-1') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SampleList profileId={profileId} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  samplesFixture = [];
  listSamplesRejection = null;
  listSamplesNeverResolves = false;
  listProfileSamplesFn.mockClear();
  deleteProfileSampleFn.mockClear();
  updateProfileSampleFn.mockClear();
  getSampleUrlFn.mockClear();
  toastFn.mockClear();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('SampleList loading + empty state', () => {
  it('renders the localized loading message while samples are being fetched', () => {
    listSamplesNeverResolves = true;

    renderList();

    // Locale key sampleList.loading -> "Loading samples…"
    expect(screen.getByText('Loading samples…')).toBeInTheDocument();
  });

  it('renders the empty-state placeholder when the profile has no samples', async () => {
    samplesFixture = [];

    renderList();

    expect(await screen.findByText('No samples yet')).toBeInTheDocument();
    expect(
      screen.getByText('Add your first audio sample to get started'),
    ).toBeInTheDocument();
    // Empty state means no edit/delete buttons should appear.
    expect(screen.queryByRole('button', { name: /edit transcription/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete sample/i })).not.toBeInTheDocument();
  });

  it('queries samples for the profileId it was given', async () => {
    samplesFixture = [buildSample({ id: 'sample-1' })];

    renderList('profile-xyz');

    await screen.findByText(samplesFixture[0].reference_text);
    expect(listProfileSamplesFn).toHaveBeenCalledWith('profile-xyz');
  });
});

describe('SampleList view mode', () => {
  it('renders one row per sample with its reference text and an indexed badge', async () => {
    samplesFixture = [
      buildSample({ id: 's1', reference_text: 'First sample reads this.' }),
      buildSample({ id: 's2', reference_text: 'Second sample reads that.' }),
    ];

    renderList();

    expect(await screen.findByText('First sample reads this.')).toBeInTheDocument();
    expect(screen.getByText('Second sample reads that.')).toBeInTheDocument();

    // Index badges (#1, #2) are rendered for each sample.
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('#2')).toBeInTheDocument();
  });

  it('exposes per-sample edit and delete action buttons with localized labels', async () => {
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);

    expect(screen.getByRole('button', { name: /edit transcription/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /delete sample/i })).toBeInTheDocument();
  });

  it('renders the mini-player play/pause and stop controls for each sample', async () => {
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);

    // Mini-player surface: play affordance + stop affordance per sample row.
    expect(screen.getByRole('button', { name: /play sample/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /stop playback/i })).toBeInTheDocument();
  });

  it('builds the mini-player audio URL by asking the api client for the sample id', async () => {
    samplesFixture = [buildSample({ id: 'audio-id-42' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);

    expect(getSampleUrlFn).toHaveBeenCalledWith('audio-id-42');
  });

  it('renders the "Add Sample" CTA and the helper note below the list', async () => {
    samplesFixture = [];

    renderList();

    await screen.findByText('No samples yet');

    expect(screen.getByRole('button', { name: /add sample/i })).toBeInTheDocument();
    // Note copy comes from sampleList.note.
    expect(screen.getByText(/single 30-second sample is the sweet spot/i)).toBeInTheDocument();
  });
});

describe('SampleList edit flow', () => {
  it('clicking edit swaps the row into an editable textarea pre-filled with the current text', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1', reference_text: 'Original text.' })];

    renderList();

    await screen.findByText('Original text.');
    await user.click(screen.getByRole('button', { name: /edit transcription/i }));

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    expect(textarea.value).toBe('Original text.');
    // Editing banner is visible.
    expect(screen.getByText('Editing transcription')).toBeInTheDocument();
    // Save + cancel buttons appear.
    expect(screen.getByRole('button', { name: /save/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument();
  });

  it('clicking cancel discards edits and returns to view mode without calling the update API', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1', reference_text: 'Original text.' })];

    renderList();

    await screen.findByText('Original text.');
    await user.click(screen.getByRole('button', { name: /edit transcription/i }));

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    await user.clear(textarea);
    await user.type(textarea, 'unsaved edits');

    await user.click(screen.getByRole('button', { name: /cancel/i }));

    // Back to view mode, original text intact.
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getByText('Original text.')).toBeInTheDocument();
    expect(updateProfileSampleFn).not.toHaveBeenCalled();
  });

  it('clicking save with non-empty text calls the update API with the trimmed text', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1', reference_text: 'Original text.' })];

    renderList();

    await screen.findByText('Original text.');
    await user.click(screen.getByRole('button', { name: /edit transcription/i }));

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    await user.clear(textarea);
    await user.type(textarea, '   The new transcription   ');

    await user.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(updateProfileSampleFn).toHaveBeenCalledWith('s1', 'The new transcription');
    });

    // Success toast surfaced.
    await waitFor(() => {
      expect(toastFn).toHaveBeenCalledWith(
        expect.objectContaining({ title: 'Sample updated' }),
      );
    });
  });

  it('clicking save with whitespace-only text emits a destructive toast and skips the API call', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1', reference_text: 'Original text.' })];

    renderList();

    await screen.findByText('Original text.');
    await user.click(screen.getByRole('button', { name: /edit transcription/i }));

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    await user.clear(textarea);
    await user.type(textarea, '     ');

    await user.click(screen.getByRole('button', { name: /save/i }));

    expect(toastFn).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Invalid text',
        variant: 'destructive',
      }),
    );
    expect(updateProfileSampleFn).not.toHaveBeenCalled();
    // Still in edit mode.
    expect(screen.getByRole('textbox')).toBeInTheDocument();
  });

  it('shows a destructive toast with the backend error message when update fails', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1', reference_text: 'Original text.' })];
    updateProfileSampleFn.mockImplementationOnce(async () => {
      throw new Error('HTTP 500: server exploded');
    });

    renderList();

    await screen.findByText('Original text.');
    await user.click(screen.getByRole('button', { name: /edit transcription/i }));

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    await user.clear(textarea);
    await user.type(textarea, 'will fail');

    await user.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(toastFn).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Update failed',
          description: 'HTTP 500: server exploded',
          variant: 'destructive',
        }),
      );
    });
  });
});

describe('SampleList delete flow', () => {
  it('clicking the row delete icon opens a confirmation dialog without calling the delete API', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    await user.click(screen.getByRole('button', { name: /delete sample/i }));

    // Dialog title (sampleList.deleteDialog.title) shows.
    expect(await screen.findByText('Delete Sample')).toBeInTheDocument();
    expect(
      screen.getByText(/Are you sure you want to delete this audio sample/i),
    ).toBeInTheDocument();
    expect(deleteProfileSampleFn).not.toHaveBeenCalled();
  });

  it('confirming the dialog calls the delete API with the sample id and closes the dialog', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 'sample-to-go' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    await user.click(screen.getByRole('button', { name: /delete sample/i }));

    const dialog = await screen.findByRole('dialog');
    // Confirm = the destructive "Delete" button inside the dialog.
    const confirmBtn = within(dialog).getByRole('button', { name: /^delete$/i });
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(deleteProfileSampleFn).toHaveBeenCalledWith('sample-to-go');
    });

    await waitFor(() => {
      expect(
        screen.queryByText(/Are you sure you want to delete this audio sample/i),
      ).not.toBeInTheDocument();
    });
  });

  it('cancelling the dialog does not call the delete API and closes the dialog', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 'sample-to-keep' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    await user.click(screen.getByRole('button', { name: /delete sample/i }));

    const dialog = await screen.findByRole('dialog');
    const cancelBtn = within(dialog).getByRole('button', { name: /cancel/i });
    await user.click(cancelBtn);

    await waitFor(() => {
      expect(
        screen.queryByText(/Are you sure you want to delete this audio sample/i),
      ).not.toBeInTheDocument();
    });
    expect(deleteProfileSampleFn).not.toHaveBeenCalled();
  });
});

describe('SampleList mini-player', () => {
  // The MiniSamplePlayer constructs a `new Audio(url)` instance per sample and
  // wires its DOM events back into local state. jsdom does not actually load
  // media, so we capture the constructed audio elements and drive their
  // lifecycle events directly to exercise the player's behaviour.
  let originalAudio: typeof Audio;
  let createdAudios: HTMLAudioElement[];

  beforeEach(() => {
    createdAudios = [];
    originalAudio = window.Audio;
    // @ts-expect-error — patching the global to record instances.
    window.Audio = function AudioCtor(src?: string) {
      const el = new originalAudio(src);
      createdAudios.push(el);
      return el;
    } as unknown as typeof Audio;
  });

  afterEach(() => {
    window.Audio = originalAudio;
  });

  it('asks the audio element to play when the play button is clicked while paused', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    expect(createdAudios.length).toBeGreaterThan(0);
    const audio = createdAudios[0];
    const playSpy = vi.spyOn(audio, 'play');

    // The play button stays disabled until loadedmetadata fires — emit that
    // so the click is dispatched against the enabled control.
    Object.defineProperty(audio, 'duration', { configurable: true, value: 10 });
    act(() => {
      audio.dispatchEvent(new Event('loadedmetadata'));
    });

    await user.click(screen.getByRole('button', { name: /play sample/i }));

    expect(playSpy).toHaveBeenCalled();
  });

  it('swaps the play button into a pause button once the audio reports it started playing', async () => {
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    const audio = createdAudios[0];

    // Simulate the browser emitting `play` (e.g. after audio.play() resolved).
    act(() => {
      audio.dispatchEvent(new Event('play'));
    });

    expect(await screen.findByRole('button', { name: /pause sample/i })).toBeInTheDocument();
  });

  it('asks the audio element to pause when the pause button is clicked while playing', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    const audio = createdAudios[0];
    const pauseSpy = vi.spyOn(audio, 'pause');

    Object.defineProperty(audio, 'duration', { configurable: true, value: 10 });
    act(() => {
      audio.dispatchEvent(new Event('loadedmetadata'));
      audio.dispatchEvent(new Event('play'));
    });

    await user.click(screen.getByRole('button', { name: /pause sample/i }));

    expect(pauseSpy).toHaveBeenCalled();
  });

  it('enables the play button and reflects total duration once loadedmetadata fires', async () => {
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    const audio = createdAudios[0];

    // While loading, the play button is disabled.
    expect(screen.getByRole('button', { name: /play sample/i })).toBeDisabled();

    // Pretend the metadata loaded with a 12.5s clip.
    Object.defineProperty(audio, 'duration', { configurable: true, value: 12.5 });
    act(() => {
      audio.dispatchEvent(new Event('loadedmetadata'));
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /play sample/i })).not.toBeDisabled();
    });
    // formatAudioDuration(12.5) renders as 0:12 (or similar mm:ss). Assert
    // that the readout no longer shows the placeholder 0:00 / 0:00 pair.
    expect(screen.queryAllByText('0:00').length).toBeLessThan(2);
  });

  it('resets to the paused state when the audio reports it ended', async () => {
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    const audio = createdAudios[0];

    // Move into the playing state first.
    act(() => {
      audio.dispatchEvent(new Event('play'));
    });
    expect(await screen.findByRole('button', { name: /pause sample/i })).toBeInTheDocument();

    // Now simulate playback finishing.
    act(() => {
      audio.dispatchEvent(new Event('ended'));
    });

    expect(await screen.findByRole('button', { name: /play sample/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /pause sample/i })).not.toBeInTheDocument();
  });

  it('updates the current-time readout when the audio reports a timeupdate event', async () => {
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    const audio = createdAudios[0];

    Object.defineProperty(audio, 'duration', { configurable: true, value: 60 });
    act(() => {
      audio.dispatchEvent(new Event('loadedmetadata'));
    });

    Object.defineProperty(audio, 'currentTime', { configurable: true, value: 30, writable: true });
    act(() => {
      audio.dispatchEvent(new Event('timeupdate'));
    });

    // formatAudioDuration(30) renders as 0:30.
    await waitFor(() => {
      expect(screen.getByText('0:30')).toBeInTheDocument();
    });
  });

  it('pauses and rewinds the audio to zero when the stop button is clicked', async () => {
    const user = userEvent.setup();
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    const audio = createdAudios[0];
    Object.defineProperty(audio, 'duration', { configurable: true, value: 10 });
    Object.defineProperty(audio, 'currentTime', { configurable: true, value: 5, writable: true });
    const pauseSpy = vi.spyOn(audio, 'pause');

    // Put the player into the loaded + playing state so we can observe the
    // toggle back to the play button.
    act(() => {
      audio.dispatchEvent(new Event('loadedmetadata'));
      audio.dispatchEvent(new Event('play'));
    });

    // The stop button is never gated on loading state; click it.
    await user.click(screen.getByRole('button', { name: /stop playback/i }));

    expect(pauseSpy).toHaveBeenCalled();
    expect(audio.currentTime).toBe(0);
    // After stop, the play button should be back (no pause button).
    expect(screen.queryByRole('button', { name: /pause sample/i })).not.toBeInTheDocument();
  });

  it('seeks the audio currentTime in proportion to the slider value', async () => {
    samplesFixture = [buildSample({ id: 's1' })];

    renderList();

    await screen.findByText(samplesFixture[0].reference_text);
    const audio = createdAudios[0];
    Object.defineProperty(audio, 'duration', { configurable: true, value: 100 });
    Object.defineProperty(audio, 'currentTime', {
      configurable: true,
      value: 0,
      writable: true,
    });
    act(() => {
      audio.dispatchEvent(new Event('loadedmetadata'));
    });

    // Radix Slider exposes a thumb with role="slider"; nudge it right and
    // observe the audio element's currentTime advance proportionally.
    const slider = await screen.findByRole('slider');
    const before = audio.currentTime;
    fireEvent.keyDown(slider, { key: 'ArrowRight', code: 'ArrowRight' });

    await waitFor(() => {
      expect(audio.currentTime).toBeGreaterThan(before);
    });
  });
});

describe('SampleList add-sample flow', () => {
  it('renders the SampleUpload child closed by default and wires it with the same profileId', async () => {
    samplesFixture = [];

    renderList('profile-7');

    await screen.findByText('No samples yet');

    const stub = screen.getByTestId('sample-upload-stub');
    expect(stub).toHaveAttribute('data-open', 'false');
    expect(stub).toHaveAttribute('data-profile-id', 'profile-7');
  });

  it('clicking "Add Sample" opens the SampleUpload child via onOpenChange(true)', async () => {
    const user = userEvent.setup();
    samplesFixture = [];

    renderList();

    await screen.findByText('No samples yet');

    expect(screen.getByTestId('sample-upload-stub')).toHaveAttribute('data-open', 'false');

    await user.click(screen.getByRole('button', { name: /add sample/i }));

    expect(screen.getByTestId('sample-upload-stub')).toHaveAttribute('data-open', 'true');
  });
});
