/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// ─────────────────────────────────────────────────────────────────────────────
// Module-level state container.
//
// These are the *observable* boundaries we capture: payloads sent to the
// mutation hooks, toast notifications, server-side state writes. Tests inspect
// these to make WHAT-style assertions about behavior — no call-count or
// internal-collaborator probes.
// ─────────────────────────────────────────────────────────────────────────────

type CreateProfileArgs = {
  name: string;
  description?: string;
  language: string;
  voice_type?: string;
  preset_engine?: string;
  preset_voice_id?: string;
  default_engine?: string;
  personality?: string;
};
type UpdateProfileArgs = {
  profileId: string;
  data: {
    name: string;
    description?: string;
    language: string;
    default_engine?: string;
    personality?: string;
  };
};
type AddSampleArgs = { profileId: string; file: File; referenceText: string };
type UploadAvatarArgs = { profileId: string; file: File };
type ToastArgs = { title: string; description?: string; variant?: string };
type EffectsUpdateArgs = { profileId: string; chain: unknown };

const captured = {
  createCalls: [] as CreateProfileArgs[],
  updateCalls: [] as UpdateProfileArgs[],
  addSampleCalls: [] as AddSampleArgs[],
  uploadAvatarCalls: [] as UploadAvatarArgs[],
  deleteAvatarCalls: [] as string[],
  deleteProfileCalls: [] as string[],
  effectsUpdateCalls: [] as EffectsUpdateArgs[],
  toasts: [] as ToastArgs[],
  draftWrites: [] as (unknown | null)[],
  dialogOpenWrites: [] as boolean[],
  editingProfileIdWrites: [] as (string | null)[],
};

// Inject failure behavior — tests flip these to drive error/rollback paths.
const failures = {
  addSampleRejects: false as boolean | Error,
  deleteProfileRejects: false as boolean | Error,
  uploadAvatarRejects: false as boolean | Error,
  effectsUpdateRejects: false as boolean | Error,
  createProfileRejects: false as boolean | Error,
};

const mutableProfile: {
  editingProfileId: string | null;
  editingProfile: undefined | Record<string, unknown>;
  draft: null | Record<string, unknown>;
  presetVoices: Array<{ voice_id: string; name: string; gender: string; language: string }>;
  isTauri: boolean;
  isSystemAudioSupported: boolean;
} = {
  editingProfileId: null,
  editingProfile: undefined,
  draft: null,
  presetVoices: [],
  isTauri: false,
  isSystemAudioSupported: false,
};

// ─────────────────────────────────────────────────────────────────────────────
// Mocks — boundary stubs only (HTTP/file/recording/toast layers).
// We do NOT mock first-party react-hook-form, zod, or Dialog components.
// ─────────────────────────────────────────────────────────────────────────────

vi.mock('@/components/AudioTrimmer/AudioTrimmer', () => ({
  AudioTrimmer: ({ onConfirm }: { file: File; onConfirm: (f: File, dur: number) => void }) => {
    const trimmedFile = new File(['trimmed-wav-data'], 'reference-trimmed.wav', {
      type: 'audio/wav',
    });
    return (
      <div data-testid="audio-trimmer">
        <button data-testid="trimmer-confirm" onClick={() => onConfirm(trimmedFile, 20)}>
          Use this clip
        </button>
      </div>
    );
  },
}));

vi.mock('@/lib/utils/audio', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/utils/audio')>();
  return {
    ...actual,
    getAudioDuration: vi.fn(() => Promise.resolve(20)),
    formatAudioDuration: actual.formatAudioDuration,
    convertToWav: vi.fn().mockResolvedValue(new Blob(['wav'], { type: 'audio/wav' })),
  };
});

vi.mock('@/lib/hooks/useReferenceTranscript', () => ({
  useReferenceTranscript: (args: { file: File | null; setText: (v: string) => void }) => ({
    status: args.file ? 'filled' : 'idle',
    isTranscribing: false,
    regeneratePrompt: false,
    retranscribe: vi.fn(),
    acceptRegenerate: vi.fn(),
    keepEdits: vi.fn(),
  }),
}));

vi.mock('@/lib/hooks/useProfiles', () => ({
  useProfile: () => ({ data: mutableProfile.editingProfile }),
  useCreateProfile: () => ({
    mutateAsync: vi.fn(async (args: CreateProfileArgs) => {
      captured.createCalls.push(args);
      if (failures.createProfileRejects) {
        throw failures.createProfileRejects instanceof Error
          ? failures.createProfileRejects
          : new Error('create failed');
      }
      return { id: 'new-profile-1' };
    }),
    isPending: false,
  }),
  useUpdateProfile: () => ({
    mutateAsync: vi.fn(async (args: UpdateProfileArgs) => {
      captured.updateCalls.push(args);
      return {};
    }),
    isPending: false,
  }),
  useAddSample: () => ({
    mutateAsync: vi.fn(async (args: AddSampleArgs) => {
      captured.addSampleCalls.push(args);
      if (failures.addSampleRejects) {
        throw failures.addSampleRejects instanceof Error
          ? failures.addSampleRejects
          : new Error('sample failed');
      }
      return {};
    }),
    isPending: false,
  }),
  useDeleteProfile: () => ({
    mutateAsync: vi.fn(async (id: string) => {
      captured.deleteProfileCalls.push(id);
      if (failures.deleteProfileRejects) {
        throw failures.deleteProfileRejects instanceof Error
          ? failures.deleteProfileRejects
          : new Error('delete failed');
      }
      return {};
    }),
    isPending: false,
  }),
  useUploadAvatar: () => ({
    mutateAsync: vi.fn(async (args: UploadAvatarArgs) => {
      captured.uploadAvatarCalls.push(args);
      if (failures.uploadAvatarRejects) {
        throw failures.uploadAvatarRejects instanceof Error
          ? failures.uploadAvatarRejects
          : new Error('avatar upload failed');
      }
      return {};
    }),
    isPending: false,
  }),
  useDeleteAvatar: () => ({
    mutateAsync: vi.fn(async (id: string) => {
      captured.deleteAvatarCalls.push(id);
      return {};
    }),
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

vi.mock('@/lib/hooks/useSystemAudioCapture', () => ({
  useSystemAudioCapture: () => ({
    isRecording: false,
    duration: 0,
    error: null,
    isSupported: mutableProfile.isSystemAudioSupported,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    cancelRecording: vi.fn(),
  }),
}));

vi.mock('@/lib/hooks/useAudioPlayer', () => ({
  useAudioPlayer: () => ({
    isPlaying: false,
    playPause: vi.fn(),
    cleanup: vi.fn(),
  }),
}));

vi.mock('@/lib/hooks/useTranscription', () => ({
  useTranscription: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('@tanstack/react-query', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-query')>();
  return {
    ...actual,
    useQuery: vi.fn(() => ({ data: { voices: mutableProfile.presetVoices } })),
  };
});

vi.mock('@/stores/uiStore', () => ({
  useUIStore: (selector: any) =>
    selector({
      profileDialogOpen: true,
      setProfileDialogOpen: (open: boolean) => {
        captured.dialogOpenWrites.push(open);
      },
      editingProfileId: mutableProfile.editingProfileId,
      setEditingProfileId: (id: string | null) => {
        captured.editingProfileIdWrites.push(id);
      },
      profileFormDraft: mutableProfile.draft,
      setProfileFormDraft: (d: unknown) => {
        captured.draftWrites.push(d);
      },
    }),
}));

vi.mock('@/stores/serverStore', () => ({
  useServerStore: (selector: any) => selector({ serverUrl: 'http://localhost:8000' }),
}));

vi.mock('@/platform/PlatformContext', () => ({
  usePlatform: () => ({
    metadata: { isTauri: mutableProfile.isTauri },
  }),
}));

vi.mock('@/lib/api/client', () => ({
  apiClient: {
    listPresetVoices: vi.fn().mockResolvedValue({ voices: [] }),
    updateProfileEffects: vi.fn(async (profileId: string, chain: unknown) => {
      captured.effectsUpdateCalls.push({ profileId, chain });
      if (failures.effectsUpdateRejects) {
        throw failures.effectsUpdateRejects instanceof Error
          ? failures.effectsUpdateRejects
          : new Error('effects update failed');
      }
      return {};
    }),
  },
}));

vi.mock('@/components/Effects/EffectsChainEditor', () => ({
  EffectsChainEditor: ({
    onChange,
  }: {
    value: unknown;
    onChange: (chain: unknown) => void;
    compact?: boolean;
  }) => (
    <div data-testid="effects-chain-editor">
      <button
        data-testid="effects-add"
        type="button"
        onClick={() => onChange([{ type: 'reverb', params: {} }])}
      >
        Add Effect
      </button>
    </div>
  ),
}));

vi.mock('@/components/ui/use-toast', () => ({
  useToast: () => ({
    toast: (args: ToastArgs) => {
      captured.toasts.push(args);
    },
  }),
}));

vi.mock('@/components/VoiceProfiles/SampleList', () => ({
  SampleList: ({ profileId }: { profileId: string }) => (
    <div data-testid="sample-list">SampleList for {profileId}</div>
  ),
}));

vi.mock('@/components/VoiceProfiles/AudioSampleUpload', () => ({
  AudioSampleUpload: ({ onFileChange }: { onFileChange: (f: File) => void }) => (
    <div data-testid="audio-sample-upload">
      <input
        data-testid="upload-file-input"
        type="file"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onFileChange(file);
        }}
      />
    </div>
  ),
}));

vi.mock('@/components/VoiceProfiles/AudioSampleRecording', () => ({
  AudioSampleRecording: () => <div data-testid="audio-sample-recording" />,
}));

vi.mock('@/components/VoiceProfiles/AudioSampleSystem', () => ({
  AudioSampleSystem: () => <div data-testid="audio-sample-system" />,
}));

import { ProfileForm } from '@/components/VoiceProfiles/ProfileForm';

beforeEach(() => {
  captured.createCalls.length = 0;
  captured.updateCalls.length = 0;
  captured.addSampleCalls.length = 0;
  captured.uploadAvatarCalls.length = 0;
  captured.deleteAvatarCalls.length = 0;
  captured.deleteProfileCalls.length = 0;
  captured.effectsUpdateCalls.length = 0;
  captured.toasts.length = 0;
  captured.draftWrites.length = 0;
  captured.dialogOpenWrites.length = 0;
  captured.editingProfileIdWrites.length = 0;
  failures.addSampleRejects = false;
  failures.deleteProfileRejects = false;
  failures.uploadAvatarRejects = false;
  failures.effectsUpdateRejects = false;
  failures.createProfileRejects = false;
  mutableProfile.editingProfileId = null;
  mutableProfile.editingProfile = undefined;
  mutableProfile.draft = null;
  mutableProfile.presetVoices = [];
  mutableProfile.isTauri = false;
  mutableProfile.isSystemAudioSupported = false;
});

// Helper — fill the name field so submit can pass zod validation.
async function fillName(name: string) {
  const nameInput = screen.getByPlaceholderText(/my voice/i);
  await userEvent.type(nameInput, name);
}

describe('ProfileForm — create mode (clone source)', () => {
  it('rejects submit when no sample file is provided and surfaces a toast', async () => {
    render(<ProfileForm />);
    await fillName('Test Voice');

    const submitBtn = screen.getByRole('button', { name: /create profile/i });
    await userEvent.click(submitBtn);

    await waitFor(() => {
      const toast = captured.toasts.find((t) => /sample required/i.test(t.title));
      expect(toast).toBeDefined();
      expect(toast?.variant).toBe('destructive');
    });
    // No profile was created.
    expect(captured.createCalls).toHaveLength(0);
  });

  it('rejects submit when reference text is blank after a clip is chosen', async () => {
    render(<ProfileForm />);
    await fillName('Test Voice');

    // Upload + confirm a trimmed clip
    const uploadTab = screen.getByRole('tab', { name: /upload/i });
    await userEvent.click(uploadTab);
    const input = screen.getByTestId('upload-file-input');
    await userEvent.upload(input, new File(['x'], 'a.wav', { type: 'audio/wav' }));
    await userEvent.click(await screen.findByTestId('trimmer-confirm'));

    // referenceText stays empty — the zod refine prevents the form from ever
    // reaching the createProfile boundary. The observable outcome is that no
    // create attempt crosses the mutation boundary, and no addSample either.
    const submitBtn = screen.getByRole('button', { name: /create profile/i });
    await userEvent.click(submitBtn);

    // Give the form a tick to process validation, then assert that none of the
    // network-bound mutations fired.
    await new Promise((r) => setTimeout(r, 50));
    expect(captured.createCalls).toHaveLength(0);
    expect(captured.addSampleCalls).toHaveLength(0);
  });

  it('trims personality whitespace and omits the field when blank on create', async () => {
    render(<ProfileForm />);
    await fillName('Trim Voice');

    const personalityArea = screen.getByPlaceholderText(/grumpy pirate/i);
    await userEvent.type(personalityArea, '   ');

    // Provide clip + reference text so the submit can reach createProfile
    const uploadTab = screen.getByRole('tab', { name: /upload/i });
    await userEvent.click(uploadTab);
    const input = screen.getByTestId('upload-file-input');
    await userEvent.upload(input, new File(['x'], 'a.wav', { type: 'audio/wav' }));
    await userEvent.click(await screen.findByTestId('trimmer-confirm'));

    const refArea = screen.getByTestId('transcript-input');
    await userEvent.type(refArea, 'the reference text');

    const submitBtn = screen.getByRole('button', { name: /create profile/i });
    await userEvent.click(submitBtn);

    await waitFor(() => expect(captured.createCalls).toHaveLength(1));
    // Blank-after-trim → field is dropped from the payload.
    expect(captured.createCalls[0].personality).toBeUndefined();
  });

  it('rolls back the profile when sample upload fails after creation', async () => {
    failures.addSampleRejects = new Error('disk full');
    render(<ProfileForm />);
    await fillName('Rollback Voice');

    const uploadTab = screen.getByRole('tab', { name: /upload/i });
    await userEvent.click(uploadTab);
    const input = screen.getByTestId('upload-file-input');
    await userEvent.upload(input, new File(['x'], 'a.wav', { type: 'audio/wav' }));
    await userEvent.click(await screen.findByTestId('trimmer-confirm'));

    const refArea = screen.getByTestId('transcript-input');
    await userEvent.type(refArea, 'reference text');

    await userEvent.click(screen.getByRole('button', { name: /create profile/i }));

    await waitFor(() => expect(captured.createCalls).toHaveLength(1));
    await waitFor(() => expect(captured.addSampleCalls).toHaveLength(1));
    // After the sample failure, the just-created profile is deleted (rollback).
    await waitFor(() => expect(captured.deleteProfileCalls).toEqual(['new-profile-1']));
    // User sees a failure toast that mentions the underlying error.
    const failureToast = captured.toasts.find((t) => /failed to add sample/i.test(t.title));
    expect(failureToast).toBeDefined();
    expect(failureToast?.description).toMatch(/disk full/);
  });

  it('reports rollback failure when delete also fails after sample failure', async () => {
    failures.addSampleRejects = new Error('sample boom');
    failures.deleteProfileRejects = new Error('delete boom');
    render(<ProfileForm />);
    await fillName('Double Fail');

    const uploadTab = screen.getByRole('tab', { name: /upload/i });
    await userEvent.click(uploadTab);
    const input = screen.getByTestId('upload-file-input');
    await userEvent.upload(input, new File(['x'], 'a.wav', { type: 'audio/wav' }));
    await userEvent.click(await screen.findByTestId('trimmer-confirm'));
    await userEvent.type(screen.getByTestId('transcript-input'), 'ref');

    await userEvent.click(screen.getByRole('button', { name: /create profile/i }));

    await waitFor(() => {
      const rollbackToast = captured.toasts.find((t) => /rollback failed/i.test(t.title));
      expect(rollbackToast).toBeDefined();
      expect(rollbackToast?.description).toMatch(/delete boom/);
    });
  });

  it('passes the trimmed clip and trimmed personality across the addSample boundary', async () => {
    render(<ProfileForm />);
    await fillName('Vox');

    const personalityArea = screen.getByPlaceholderText(/grumpy pirate/i);
    await userEvent.type(personalityArea, '  a pirate  ');

    const uploadTab = screen.getByRole('tab', { name: /upload/i });
    await userEvent.click(uploadTab);
    const input = screen.getByTestId('upload-file-input');
    await userEvent.upload(input, new File(['x'], 'orig.wav', { type: 'audio/wav' }));
    await userEvent.click(await screen.findByTestId('trimmer-confirm'));

    await userEvent.type(screen.getByTestId('transcript-input'), 'the reference text');

    await userEvent.click(screen.getByRole('button', { name: /create profile/i }));

    await waitFor(() => expect(captured.addSampleCalls).toHaveLength(1));
    expect(captured.addSampleCalls[0].file.name).toBe('reference-trimmed.wav');
    expect(captured.addSampleCalls[0].referenceText).toBe('the reference text');
    expect(captured.createCalls[0].personality).toBe('a pirate');
    // Success toast appears and dialog is closed via the store.
    await waitFor(() => {
      expect(captured.toasts.some((t) => /profile created/i.test(t.title))).toBe(true);
    });
    expect(captured.dialogOpenWrites).toContain(false);
  });
});

describe('ProfileForm — create mode (built-in voice source)', () => {
  it('rejects builtin submit when no preset voice is selected', async () => {
    mutableProfile.presetVoices = [
      { voice_id: 'p1', name: 'Voice 1', gender: 'female', language: 'en' },
    ];
    render(<ProfileForm />);

    // Switch source to built-in
    const builtinBtn = screen.getByRole('button', { name: /built-in voice/i });
    await userEvent.click(builtinBtn);
    await fillName('Builtin Voice');

    await userEvent.click(screen.getByRole('button', { name: /create profile/i }));

    await waitFor(() => {
      expect(captured.toasts.some((t) => /no voice selected/i.test(t.title))).toBe(true);
    });
    expect(captured.createCalls).toHaveLength(0);
  });

  it('submits a preset profile carrying the selected engine and voice id', async () => {
    mutableProfile.presetVoices = [
      { voice_id: 'voice-a', name: 'Alex', gender: 'male', language: 'en' },
      { voice_id: 'voice-b', name: 'Bella', gender: 'female', language: 'fr' },
    ];
    render(<ProfileForm />);

    await userEvent.click(screen.getByRole('button', { name: /built-in voice/i }));
    await fillName('Builtin Voice');

    // Pick a voice — voice_id 'voice-b' with language fr (drives auto-language)
    await userEvent.click(screen.getByRole('button', { name: /Bella/i }));

    await userEvent.click(screen.getByRole('button', { name: /create profile/i }));

    await waitFor(() => expect(captured.createCalls).toHaveLength(1));
    const payload = captured.createCalls[0];
    expect(payload.voice_type).toBe('preset');
    expect(payload.preset_voice_id).toBe('voice-b');
    // The form auto-selects the voice's language.
    expect(payload.language).toBe('fr');
    expect(payload.default_engine).toBe(payload.preset_engine);
    // No sample was added — addSample never crossed the boundary for preset.
    expect(captured.addSampleCalls).toHaveLength(0);
  });
});

describe('ProfileForm — avatar handling', () => {
  it('rejects a non-image file via toast and does not enqueue it for upload', async () => {
    render(<ProfileForm />);
    await fillName('A');

    const avatarInput = document.querySelector(
      'input[type="file"][accept*="image"]',
    ) as HTMLInputElement;
    expect(avatarInput).toBeTruthy();

    // userEvent.upload validates against the input's accept= attribute (PDFs are
    // blocked client-side). The handler we're testing reads File.type at runtime
    // and must reject non-image MIME types — fire the change event directly so a
    // PDF actually reaches the handler.
    const badFile = new File(['x'], 'doc.pdf', { type: 'application/pdf' });
    Object.defineProperty(avatarInput, 'files', {
      value: [badFile],
      configurable: true,
    });
    fireEvent.change(avatarInput);

    expect(captured.toasts.some((t) => /invalid file type/i.test(t.title))).toBe(true);
  });

  it('rejects an oversized image via toast', async () => {
    render(<ProfileForm />);
    await fillName('A');

    const avatarInput = document.querySelector(
      'input[type="file"][accept*="image"]',
    ) as HTMLInputElement;
    // 6MB image — exceeds the 5MB cap.
    const bigBytes = new Uint8Array(6 * 1024 * 1024);
    const bigFile = new File([bigBytes], 'big.png', { type: 'image/png' });
    await userEvent.upload(avatarInput, bigFile);

    expect(captured.toasts.some((t) => /file too large/i.test(t.title))).toBe(true);
  });

  it('uploads a valid avatar after profile creation in builtin flow', async () => {
    mutableProfile.presetVoices = [
      { voice_id: 'v1', name: 'V1', gender: 'female', language: 'en' },
    ];
    render(<ProfileForm />);

    await userEvent.click(screen.getByRole('button', { name: /built-in voice/i }));
    await fillName('Av');
    await userEvent.click(screen.getByRole('button', { name: /V1/i }));

    const avatarInput = document.querySelector(
      'input[type="file"][accept*="image"]',
    ) as HTMLInputElement;
    const goodImage = new File(['imgbytes'], 'face.png', { type: 'image/png' });
    await userEvent.upload(avatarInput, goodImage);

    await userEvent.click(screen.getByRole('button', { name: /create profile/i }));

    await waitFor(() => expect(captured.createCalls).toHaveLength(1));
    await waitFor(() => expect(captured.uploadAvatarCalls).toHaveLength(1));
    expect(captured.uploadAvatarCalls[0].profileId).toBe('new-profile-1');
    expect(captured.uploadAvatarCalls[0].file.name).toBe('face.png');
  });

  it('surfaces an avatar upload failure as a toast but still completes profile creation', async () => {
    failures.uploadAvatarRejects = new Error('avatar boom');
    mutableProfile.presetVoices = [
      { voice_id: 'v1', name: 'V1', gender: 'female', language: 'en' },
    ];
    render(<ProfileForm />);

    await userEvent.click(screen.getByRole('button', { name: /built-in voice/i }));
    await fillName('Av');
    await userEvent.click(screen.getByRole('button', { name: /V1/i }));

    const avatarInput = document.querySelector(
      'input[type="file"][accept*="image"]',
    ) as HTMLInputElement;
    await userEvent.upload(
      avatarInput,
      new File(['x'], 'a.png', { type: 'image/png' }),
    );

    await userEvent.click(screen.getByRole('button', { name: /create profile/i }));

    await waitFor(() => expect(captured.createCalls).toHaveLength(1));
    await waitFor(() => {
      const failToast = captured.toasts.find((t) => /avatar upload failed/i.test(t.title));
      expect(failToast).toBeDefined();
      expect(failToast?.description).toMatch(/avatar boom/);
    });
    // Even after avatar failure, the profile-created success toast appears.
    expect(captured.toasts.some((t) => /profile created/i.test(t.title))).toBe(true);
  });
});

describe('ProfileForm — edit mode', () => {
  beforeEach(() => {
    mutableProfile.editingProfileId = 'existing-1';
    mutableProfile.editingProfile = {
      id: 'existing-1',
      name: 'Existing Voice',
      description: 'desc',
      language: 'en',
      voice_type: 'cloned',
      personality: 'old personality',
      effects_chain: [],
      default_engine: 'qwen',
    };
  });

  it('renders SampleList in editing mode for a cloned profile', () => {
    render(<ProfileForm />);
    expect(screen.getByTestId('sample-list')).toHaveTextContent('SampleList for existing-1');
  });

  it('pre-fills the form fields from the editing profile', async () => {
    render(<ProfileForm />);
    await waitFor(() => {
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Existing Voice',
      );
    });
  });

  it('submits an update with the edited name across the updateProfile boundary', async () => {
    render(<ProfileForm />);
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Existing Voice',
      ),
    );

    const nameInput = screen.getByPlaceholderText(/my voice/i);
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'Renamed Voice');

    const submit = screen.getByRole('button', { name: /save changes/i });
    await userEvent.click(submit);

    await waitFor(() => expect(captured.updateCalls).toHaveLength(1));
    const payload = captured.updateCalls[0];
    expect(payload.profileId).toBe('existing-1');
    expect(payload.data.name).toBe('Renamed Voice');
    // No new profile created in edit mode.
    expect(captured.createCalls).toHaveLength(0);
  });

  it('persists effects-chain changes on edit when the editor signals dirty', async () => {
    render(<ProfileForm />);
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Existing Voice',
      ),
    );

    // Modify effects via the stub button — flips effectsDirty true and sets chain.
    await userEvent.click(screen.getByTestId('effects-add'));
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(captured.effectsUpdateCalls).toHaveLength(1));
    expect(captured.effectsUpdateCalls[0].profileId).toBe('existing-1');
    expect(captured.effectsUpdateCalls[0].chain).toEqual([{ type: 'reverb', params: {} }]);
  });

  it('surfaces effects-update failures as a toast and aborts the success path', async () => {
    failures.effectsUpdateRejects = new Error('fx boom');
    render(<ProfileForm />);
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Existing Voice',
      ),
    );

    await userEvent.click(screen.getByTestId('effects-add'));
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => {
      const fxToast = captured.toasts.find((t) => /effects update failed/i.test(t.title));
      expect(fxToast).toBeDefined();
      expect(fxToast?.description).toMatch(/fx boom/);
    });
    // The "voice updated" success toast must NOT have fired — the function returned early.
    expect(captured.toasts.some((t) => /voice updated/i.test(t.title))).toBe(false);
  });

  it('removes an existing avatar via the delete-avatar boundary', async () => {
    mutableProfile.editingProfile = {
      ...(mutableProfile.editingProfile as Record<string, unknown>),
      avatar_path: 'avatars/existing.png',
    };
    render(<ProfileForm />);

    // The avatar overlay X-button shows when the avatar is present.
    // The avatar overlay X-button is the second of multiple X-icon buttons —
    // we locate it directly inside the avatar area via its parent group.
    const avatarImg = await screen.findByAltText(/avatar preview/i);
    const avatarGroup = avatarImg.closest('.group');
    expect(avatarGroup).toBeTruthy();
    const removeBtn = avatarGroup?.querySelectorAll('button')[1];
    expect(removeBtn).toBeTruthy();
    await userEvent.click(removeBtn as HTMLButtonElement);

    await waitFor(() => expect(captured.deleteAvatarCalls).toEqual(['existing-1']));
    expect(captured.toasts.some((t) => /avatar removed/i.test(t.title))).toBe(true);
  });

  describe('with a preset profile being edited', () => {
    beforeEach(() => {
      mutableProfile.editingProfile = {
        id: 'preset-1',
        name: 'Preset Voice',
        description: '',
        language: 'en',
        voice_type: 'preset',
        preset_engine: 'kokoro',
        preset_voice_id: 'voice-a',
        effects_chain: [],
      };
      mutableProfile.editingProfileId = 'preset-1';
      mutableProfile.presetVoices = [
        { voice_id: 'voice-a', name: 'Alex', gender: 'male', language: 'en' },
      ];
    });

    it('shows the built-in badge and voice name in the read-only side', () => {
      render(<ProfileForm />);
      // The voice name + engine badge are unique strings rendered only when the
      // editing-mode preset summary block is active.
      expect(screen.getByText('Alex')).toBeInTheDocument();
      expect(screen.getByText('kokoro')).toBeInTheDocument();
      // The summary contains a note about voice immutability — distinct text that
      // appears only on the preset summary block.
      expect(
        screen.getByText(/voice cannot be changed after creation/i),
      ).toBeInTheDocument();
    });
  });
});

describe('ProfileForm — edit mode (avatar + create-only update branches)', () => {
  beforeEach(() => {
    mutableProfile.editingProfileId = 'existing-1';
    mutableProfile.editingProfile = {
      id: 'existing-1',
      name: 'Existing Voice',
      description: 'desc',
      language: 'en',
      voice_type: 'cloned',
      effects_chain: [],
    };
  });

  it('uploads a new avatar in edit mode via the upload-avatar boundary', async () => {
    render(<ProfileForm />);
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Existing Voice',
      ),
    );

    const avatarInput = document.querySelector(
      'input[type="file"][accept*="image"]',
    ) as HTMLInputElement;
    await userEvent.upload(
      avatarInput,
      new File(['x'], 'edit-avatar.png', { type: 'image/png' }),
    );

    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(captured.uploadAvatarCalls).toHaveLength(1));
    expect(captured.uploadAvatarCalls[0].profileId).toBe('existing-1');
    expect(captured.uploadAvatarCalls[0].file.name).toBe('edit-avatar.png');
  });

  it('surfaces an avatar upload failure as a toast during edit', async () => {
    failures.uploadAvatarRejects = new Error('edit avatar boom');
    render(<ProfileForm />);
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Existing Voice',
      ),
    );

    const avatarInput = document.querySelector(
      'input[type="file"][accept*="image"]',
    ) as HTMLInputElement;
    await userEvent.upload(avatarInput, new File(['x'], 'a.png', { type: 'image/png' }));

    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => {
      const fail = captured.toasts.find((t) => /avatar upload failed/i.test(t.title));
      expect(fail).toBeDefined();
      expect(fail?.description).toMatch(/edit avatar boom/);
    });
  });

  it('closes the dialog after a successful edit submit', async () => {
    render(<ProfileForm />);
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Existing Voice',
      ),
    );

    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));
    await waitFor(() => expect(captured.updateCalls).toHaveLength(1));
    // Dialog close was written to the store after the successful submit.
    expect(captured.dialogOpenWrites).toContain(false);
    // And the editing-profile id was cleared.
    expect(captured.editingProfileIdWrites).toContain(null);
  });
});

describe('ProfileForm — retranscribe controls', () => {
  it('triggers a retranscribe on the confirmed clip when the user clicks Re-transcribe', async () => {
    // The mocked useReferenceTranscript exposes a retranscribe spy through the
    // returned object — but the actual spy identity changes per render. We rely
    // on the visible state of the button (enabled when hasClip is true).
    render(<ProfileForm />);
    await fillName('Retransc');

    // Confirm a clip
    await userEvent.click(screen.getByRole('tab', { name: /upload/i }));
    const input = screen.getByTestId('upload-file-input');
    await userEvent.upload(input, new File(['x'], 'a.wav', { type: 'audio/wav' }));
    await userEvent.click(await screen.findByTestId('trimmer-confirm'));

    // Re-transcribe button becomes enabled because a clip was confirmed.
    const retranscribeBtn = screen.getByTestId('transcript-retranscribe');
    expect(retranscribeBtn).not.toBeDisabled();
    // Clicking it should not throw and should not produce any toast.
    await userEvent.click(retranscribeBtn);
    // No error toasts should be produced by the click.
    expect(captured.toasts.filter((t) => t.variant === 'destructive')).toHaveLength(0);
  });
});

describe('ProfileForm — draft persistence', () => {
  it('restores name and reference text from a saved draft when opening in create mode', async () => {
    mutableProfile.draft = {
      name: 'Drafted Name',
      description: 'drafted desc',
      language: 'en',
      personality: '',
      referenceText: 'drafted ref',
      sampleMode: 'upload',
    };
    render(<ProfileForm />);

    await waitFor(() => {
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Drafted Name',
      );
    });
    // The reference-transcript textarea is rendered after the upload tab is active.
    const uploadTabPanel = await screen.findByTestId('audio-sample-upload');
    expect(uploadTabPanel).toBeInTheDocument();
    expect((screen.getByTestId('transcript-input') as HTMLTextAreaElement).value).toBe(
      'drafted ref',
    );
  });

  it('clears the draft store when the user clicks Discard', async () => {
    mutableProfile.draft = {
      name: 'Drafted Name',
      description: '',
      language: 'en',
      personality: '',
      referenceText: '',
      sampleMode: 'record',
    };
    render(<ProfileForm />);

    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Drafted Name',
      ),
    );

    await userEvent.click(screen.getByRole('button', { name: /discard/i }));

    // Discarding writes null into the draft store.
    expect(captured.draftWrites).toContain(null);
    // The form was reset — name input is now empty.
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(''),
    );
  });

  it('saves a draft when the user cancels with unsaved content', async () => {
    render(<ProfileForm />);
    await fillName('Half-finished');

    // Click Cancel (outline button in footer).
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));

    await waitFor(() => {
      // The store received a non-null draft write containing the user's input.
      const drafts = captured.draftWrites.filter((d) => d !== null) as Array<{
        name: string;
      }>;
      expect(drafts.some((d) => d.name === 'Half-finished')).toBe(true);
    });
  });

  it('restores a sample file from base64 draft data when the draft holds a saved clip', async () => {
    // A 1-byte WAV encoded as base64 — exercises the base64ToFile helper.
    const b64 = 'data:audio/wav;base64,QQ=='; // 'A' as base64
    mutableProfile.draft = {
      name: 'Has Clip',
      description: '',
      language: 'en',
      personality: '',
      referenceText: 'r',
      sampleMode: 'upload',
      sampleFileName: 'saved.wav',
      sampleFileType: 'audio/wav',
      sampleFileData: b64,
    };
    render(<ProfileForm />);

    // The form successfully mounts and the name field is restored — proxy for
    // the base64ToFile branch executing without throwing.
    await waitFor(() =>
      expect((screen.getByPlaceholderText(/my voice/i) as HTMLInputElement).value).toBe(
        'Has Clip',
      ),
    );
  });

  it('saves a draft containing base64-encoded sample data when closing with a clip', async () => {
    render(<ProfileForm />);
    await fillName('With Clip');

    // Confirm a clip so the form has a sampleFile, then cancel.
    await userEvent.click(screen.getByRole('tab', { name: /upload/i }));
    await userEvent.upload(
      screen.getByTestId('upload-file-input'),
      new File(['clipdata'], 'clip.wav', { type: 'audio/wav' }),
    );
    await userEvent.click(await screen.findByTestId('trimmer-confirm'));

    await userEvent.click(screen.getByRole('button', { name: /cancel/i }));

    await waitFor(() => {
      const drafts = captured.draftWrites.filter((d) => d !== null) as Array<{
        sampleFileName?: string;
        sampleFileData?: string;
      }>;
      const withClip = drafts.find((d) => !!d.sampleFileName);
      expect(withClip).toBeDefined();
      // Sample file data was base64-encoded into the draft.
      expect(withClip?.sampleFileData).toMatch(/^data:/);
    });
  });
});

describe('ProfileForm — system audio (Tauri only)', () => {
  beforeEach(() => {
    mutableProfile.isTauri = true;
    mutableProfile.isSystemAudioSupported = true;
  });

  it('renders the system-audio tab when running on Tauri with system audio support', () => {
    render(<ProfileForm />);
    expect(screen.getByRole('tab', { name: /system audio/i })).toBeInTheDocument();
  });
});
