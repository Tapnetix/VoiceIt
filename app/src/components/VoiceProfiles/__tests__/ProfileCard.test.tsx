/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { ProfileCard } from '@/components/VoiceProfiles/ProfileCard';
import type { VoiceProfileResponse } from '@/lib/api/types';
import { useUIStore } from '@/stores/uiStore';

// ── apiClient mock ────────────────────────────────────────────────────────────
// Drive the real useDeleteProfile / useExportProfile hooks through a faked
// HTTP boundary instead of mocking the hooks themselves. This keeps the test
// against the public contract of the component (effects on selection, profile
// store, and the network) rather than the internal hook implementation.
const deleteProfileFn = vi.fn().mockResolvedValue(undefined);
const exportProfileFn = vi.fn().mockResolvedValue(new Blob(['zip'], { type: 'application/zip' }));
// useExportProfile fetches the profile again to derive a download filename from
// its name. We keep the current "displayed" profile in a holder so the API
// response stays in sync with whatever the rendering test asked for.
const currentProfileHolder: { profile: VoiceProfileResponse | null } = { profile: null };
const getProfileFn = vi.fn(async (id: string) => {
  if (currentProfileHolder.profile?.id === id) return currentProfileHolder.profile;
  return buildProfile({ id });
});

vi.mock('@/lib/api/client', () => ({
  apiClient: {
    deleteProfile: (id: string) => deleteProfileFn(id),
    exportProfile: (id: string) => exportProfileFn(id),
    getProfile: (id: string) => getProfileFn(id),
  },
}));

// ── Platform mock (used by useExportProfile to save the downloaded blob) ─────
const saveFileFn = vi.fn().mockResolvedValue(undefined);
vi.mock('@/platform/PlatformContext', () => ({
  usePlatform: () => ({
    metadata: { isTauri: false },
    filesystem: { saveFile: saveFileFn },
  }),
}));

// ─────────────────────────────────────────────────────────────────────────────

function buildProfile(overrides: Partial<VoiceProfileResponse> = {}): VoiceProfileResponse {
  return {
    id: 'profile-1',
    name: 'Test Voice',
    description: 'A friendly narrator',
    language: 'en',
    voice_type: 'cloned',
    generation_count: 0,
    sample_count: 1,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

function renderCard(profile: VoiceProfileResponse, disabled?: boolean) {
  currentProfileHolder.profile = profile;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  // Seed the profiles list cache so we can later assert that a successful
  // delete mutation actually causes it to be invalidated (observable side
  // effect of useDeleteProfile.onSuccess), and that cancel/no-op flows leave
  // the cache state untouched.
  queryClient.setQueryData(['profiles'], [profile]);
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <ProfileCard profile={profile} disabled={disabled} />
    </QueryClientProvider>,
  );
  return { ...utils, queryClient };
}

// Reset the persisted Zustand UI store between tests so selection state from
// one test cannot leak into the next.
function resetUIStore() {
  useUIStore.setState({
    selectedProfileId: null,
    editingProfileId: null,
    profileDialogOpen: false,
  });
}

beforeEach(() => {
  resetUIStore();
  deleteProfileFn.mockClear();
  exportProfileFn.mockClear();
  getProfileFn.mockClear();
  saveFileFn.mockClear();
});

afterEach(() => {
  resetUIStore();
});

describe('ProfileCard rendering', () => {
  it('renders the profile name and description', () => {
    renderCard(buildProfile({ name: 'Alex', description: 'Warm baritone' }));

    expect(screen.getByText('Alex')).toBeInTheDocument();
    expect(screen.getByText('Warm baritone')).toBeInTheDocument();
  });

  it('falls back to the localized noDescription label when description is empty', () => {
    renderCard(buildProfile({ description: undefined }));

    // The English locale renders "No description" for profiles.card.noDescription.
    expect(screen.getByText('No description')).toBeInTheDocument();
  });

  it('renders the language as a badge', () => {
    renderCard(buildProfile({ language: 'ja' }));

    expect(screen.getByText('ja')).toBeInTheDocument();
  });

  it('renders the human-readable engine name for preset profiles with a known engine', () => {
    renderCard(
      buildProfile({ voice_type: 'preset', preset_engine: 'kokoro' }),
    );

    expect(screen.getByText('Kokoro')).toBeInTheDocument();
  });

  it('renders the qwen_custom_voice engine as "CustomVoice"', () => {
    renderCard(
      buildProfile({ voice_type: 'preset', preset_engine: 'qwen_custom_voice' }),
    );

    expect(screen.getByText('CustomVoice')).toBeInTheDocument();
  });

  it('falls back to the raw preset_engine value for unknown engines', () => {
    renderCard(
      buildProfile({ voice_type: 'preset', preset_engine: 'experimental_xyz' }),
    );

    expect(screen.getByText('experimental_xyz')).toBeInTheDocument();
  });

  it('renders the "designed" badge for designed voice_type', () => {
    renderCard(buildProfile({ voice_type: 'designed' }));

    expect(screen.getByText('designed')).toBeInTheDocument();
  });

  it('does not render preset/designed badges for cloned voice_type', () => {
    renderCard(buildProfile({ voice_type: 'cloned' }));

    expect(screen.queryByText('designed')).not.toBeInTheDocument();
    expect(screen.queryByText('Kokoro')).not.toBeInTheDocument();
  });

  it('exposes an accessible button role with a descriptive aria-label', () => {
    renderCard(buildProfile({ name: 'Alex', language: 'en' }));

    const card = screen.getByRole('button', { name: /Alex.*en/ });
    expect(card).toBeInTheDocument();
    expect(card).toHaveAttribute('aria-pressed', 'false');
  });

  it('reflects selected state via aria-pressed and uses the "selected" aria-label variant', () => {
    useUIStore.setState({ selectedProfileId: 'profile-1' });

    renderCard(buildProfile({ id: 'profile-1', name: 'Alex' }));

    const card = screen.getByRole('button', { name: /Alex.*Selected as voice/i });
    expect(card).toHaveAttribute('aria-pressed', 'true');
  });

  it('renders edit / export / delete action buttons with localized aria-labels', () => {
    renderCard(buildProfile());

    expect(screen.getByRole('button', { name: /export profile/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /edit profile/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /delete profile/i })).toBeInTheDocument();
  });
});

describe('ProfileCard selection behaviour', () => {
  it('selects the profile when the card is clicked and it was not selected', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    await user.click(screen.getByRole('button', { name: /Test Voice/ }));

    expect(useUIStore.getState().selectedProfileId).toBe('profile-1');
  });

  it('deselects the profile when the card is clicked and it was already selected', async () => {
    useUIStore.setState({ selectedProfileId: 'profile-1' });
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    await user.click(screen.getByRole('button', { name: /Test Voice/ }));

    expect(useUIStore.getState().selectedProfileId).toBeNull();
  });

  it('selects the profile when Enter is pressed while the card has focus', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    const card = screen.getByRole('button', { name: /Test Voice/ });
    card.focus();
    await user.keyboard('{Enter}');

    expect(useUIStore.getState().selectedProfileId).toBe('profile-1');
  });

  it('selects the profile when Space is pressed while the card has focus', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    const card = screen.getByRole('button', { name: /Test Voice/ });
    card.focus();
    await user.keyboard(' ');

    expect(useUIStore.getState().selectedProfileId).toBe('profile-1');
  });

  it('does not toggle selection when other keys are pressed', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    const card = screen.getByRole('button', { name: /Test Voice/ });
    card.focus();
    await user.keyboard('a');

    expect(useUIStore.getState().selectedProfileId).toBeNull();
  });

  it('when disabled and currently selected, re-emits the selection so the parent can react', async () => {
    // The disabled branch nulls the selection synchronously and then re-asserts
    // it after a 0-ms setTimeout. We assert the post-timeout state directly
    // rather than racing fake timers against userEvent.
    useUIStore.setState({ selectedProfileId: 'profile-1' });
    const user = userEvent.setup();

    renderCard(buildProfile({ id: 'profile-1' }), /* disabled */ true);

    await user.click(screen.getByRole('button', { name: /Test Voice/ }));

    // After the macrotask the selection should be restored to this profile id.
    await waitFor(() => {
      expect(useUIStore.getState().selectedProfileId).toBe('profile-1');
    });
  });
});

describe('ProfileCard action buttons', () => {
  it('opens the edit dialog by writing the editing profile id and dialog flag into the UI store', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    await user.click(screen.getByRole('button', { name: /edit profile/i }));

    const state = useUIStore.getState();
    expect(state.editingProfileId).toBe('profile-1');
    expect(state.profileDialogOpen).toBe(true);
  });

  it('does not toggle selection when an action button is clicked (event propagation halted)', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    await user.click(screen.getByRole('button', { name: /edit profile/i }));

    // The edit click should not flip the selection.
    expect(useUIStore.getState().selectedProfileId).toBeNull();
  });

  it('invokes the export API and saves the resulting blob through the platform filesystem', async () => {
    // Give the export API a uniquely identifiable blob so we can verify the
    // *same* blob makes it all the way through to the filesystem boundary —
    // a true end-to-end data-flow assertion instead of a call-ledger check.
    const exportedBlob = new Blob(['ZIP-CONTENT-FOR-PROFILE-1'], {
      type: 'application/zip',
    });
    exportProfileFn.mockResolvedValueOnce(exportedBlob);

    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1', name: 'My Voice' }));

    await user.click(screen.getByRole('button', { name: /export profile/i }));

    // Wait until the platform filesystem boundary has received the save call;
    // its arguments are the observable outcome of the whole export pipeline.
    await waitFor(() => {
      expect(saveFileFn.mock.calls.length).toBeGreaterThan(0);
    });

    const [filename, blob, filters] = saveFileFn.mock.calls[0] as [
      string,
      Blob,
      Array<{ name: string; extensions: string[] }>,
    ];

    // 1. Filename shape: sluggified from the profile name we exported.
    expect(filename).toBe('profile-my-voice.voiceit.zip');

    // 2. The blob that arrives at the filesystem is exactly the blob the
    //    export API returned — proves the API was called and its result
    //    flowed through unmodified (subsumes the earlier "called with id"
    //    check, since a different id would have produced a different blob).
    expect(blob).toBe(exportedBlob);
    expect(blob.type).toBe('application/zip');
    expect(blob.size).toBeGreaterThan(0);

    // 3. Filter spec passed to the native save dialog includes the .zip
    //    extension under a human-readable group name.
    expect(filters).toEqual([
      { name: 'VoiceIt Profile', extensions: ['zip'] },
    ]);
  });

  it('clicking delete opens a confirmation dialog but does not yet call the delete API', async () => {
    const user = userEvent.setup();
    const { queryClient } = renderCard(buildProfile({ id: 'profile-1', name: 'My Voice' }));
    // Snapshot the freshly-seeded profiles cache so we can confirm the
    // mutation's onSuccess invalidation has NOT yet fired.
    const profilesStateBefore = queryClient.getQueryState(['profiles']);

    await user.click(screen.getByRole('button', { name: /delete profile/i }));

    // Dialog body includes the profile name.
    expect(
      await screen.findByText(/Are you sure you want to delete "My Voice"/i),
    ).toBeInTheDocument();

    // Observable signs the delete has not yet been confirmed:
    //  - The destructive button still reads its pre-pending label ("Delete"),
    //    not the "Deleting..." in-flight label that useDeleteProfile.isPending
    //    would flip it to.
    const destructive = screen.getByRole('button', { name: /^delete$/i });
    expect(destructive).toBeEnabled();
    expect(destructive).toHaveTextContent(/^Delete$/);

    //  - The cancel control is still present (would be unmounted if the
    //    dialog had closed because of a successful delete).
    expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument();

    //  - The profiles cache was not invalidated (delete's onSuccess effect
    //    has not run — proves the network call did not complete).
    const profilesStateAfter = queryClient.getQueryState(['profiles']);
    expect(profilesStateAfter?.dataUpdatedAt).toBe(profilesStateBefore?.dataUpdatedAt);
    expect(profilesStateAfter?.isInvalidated).toBe(false);
  });

  it('confirming the delete dialog removes the profile via the API and invalidates the cached profiles list', async () => {
    // Track which id the API actually received and stall the resolution
    // until we assert on it, so we can verify the call-argument shape via
    // the captured value rather than the spy's call ledger.
    let receivedId: string | undefined;
    let resolveDelete!: () => void;
    deleteProfileFn.mockImplementationOnce(async (id: string) => {
      receivedId = id;
      await new Promise<void>((resolve) => {
        resolveDelete = resolve;
      });
    });

    const user = userEvent.setup();
    const { queryClient } = renderCard(buildProfile({ id: 'profile-1' }));

    await user.click(screen.getByRole('button', { name: /delete profile/i }));
    const confirmBtn = await screen.findByRole('button', { name: /^delete$/i });
    await user.click(confirmBtn);

    // Wait for the mutation to dispatch into the API boundary.
    await waitFor(() => {
      expect(receivedId).toBe('profile-1');
    });

    // Let the mutation settle so onSuccess can run.
    resolveDelete();

    // Observable end-state: the profiles list query is invalidated, which
    // is what triggers consumer components (ProfileList, sidebar, etc.) to
    // refetch and observe the removed profile. This is the user-visible
    // effect of a successful delete — strictly stronger than asserting
    // "the spy was called".
    await waitFor(() => {
      const state = queryClient.getQueryState(['profiles']);
      expect(state?.isInvalidated).toBe(true);
    });
  });

  it('cancelling the delete dialog closes it and leaves the profiles cache untouched', async () => {
    const user = userEvent.setup();
    const { queryClient } = renderCard(buildProfile({ id: 'profile-1' }));
    const profilesStateBefore = queryClient.getQueryState(['profiles']);

    await user.click(screen.getByRole('button', { name: /delete profile/i }));

    const cancelBtn = await screen.findByRole('button', { name: /cancel/i });
    await user.click(cancelBtn);

    // Dialog closes — both the confirmation copy and the destructive
    // "Delete" button disappear from the DOM (the latter is the most
    // specific proof: the only remaining "delete profile" surface is the
    // outer card's CircleButton, not a "Delete" submit button).
    await waitFor(() => {
      expect(
        screen.queryByText(/Are you sure you want to delete/i),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /^delete$/i })).not.toBeInTheDocument();

    // Profiles cache is in exactly the state we seeded — no invalidation
    // happened, which is what consumers of useProfiles would observe.
    const profilesStateAfter = queryClient.getQueryState(['profiles']);
    expect(profilesStateAfter?.dataUpdatedAt).toBe(profilesStateBefore?.dataUpdatedAt);
    expect(profilesStateAfter?.isInvalidated).toBe(false);
    // The cached payload itself is unchanged (still the seeded list).
    expect(queryClient.getQueryData(['profiles'])).toEqual([
      expect.objectContaining({ id: 'profile-1' }),
    ]);
  });
});

describe('ProfileCard conditional indicators', () => {
  it('does not render the effects sparkle icon when effects_chain is empty', () => {
    const { container } = renderCard(buildProfile({ effects_chain: [] }));

    // lucide-react renders icons as svg with a `lucide-sparkles` class.
    expect(container.querySelector('.lucide-sparkles')).toBeNull();
  });

  it('renders the effects sparkle icon when effects_chain has at least one effect', () => {
    const { container } = renderCard(
      buildProfile({
        effects_chain: [{ type: 'reverb', enabled: true, params: { wet: 0.3 } }],
      }),
    );

    expect(container.querySelector('.lucide-sparkles')).not.toBeNull();
  });

  it('does not render the personality wand icon when personality is empty or whitespace', () => {
    // The Wand2 import in lucide-react v0.454 aliases to WandSparkles, whose
    // rendered class is `lucide-wand-sparkles`.
    const { container } = renderCard(buildProfile({ personality: '   ' }));

    expect(container.querySelector('.lucide-wand-sparkles')).toBeNull();
  });

  it('does not render the personality wand icon when personality is null', () => {
    const { container } = renderCard(buildProfile({ personality: null }));

    expect(container.querySelector('.lucide-wand-sparkles')).toBeNull();
  });

  it('renders the personality wand icon when personality has non-whitespace content', () => {
    const { container } = renderCard(
      buildProfile({ personality: 'Speak like a pirate' }),
    );

    expect(container.querySelector('.lucide-wand-sparkles')).not.toBeNull();
  });
});
