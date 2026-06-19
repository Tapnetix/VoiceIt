/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

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
  return render(
    <QueryClientProvider client={queryClient}>
      <ProfileCard profile={profile} disabled={disabled} />
    </QueryClientProvider>,
  );
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
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1', name: 'My Voice' }));

    await user.click(screen.getByRole('button', { name: /export profile/i }));

    await waitFor(() => {
      expect(exportProfileFn).toHaveBeenCalledWith('profile-1');
    });
    await waitFor(() => {
      // filename is sluggified from the profile name.
      expect(saveFileFn).toHaveBeenCalledWith(
        'profile-my-voice.voiceit.zip',
        expect.any(Blob),
        expect.arrayContaining([
          expect.objectContaining({ extensions: expect.arrayContaining(['zip']) }),
        ]),
      );
    });
  });

  it('clicking delete opens a confirmation dialog but does not yet call the delete API', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1', name: 'My Voice' }));

    await user.click(screen.getByRole('button', { name: /delete profile/i }));

    // Dialog body includes the profile name.
    expect(await screen.findByText(/Are you sure you want to delete "My Voice"/i)).toBeInTheDocument();
    expect(deleteProfileFn).not.toHaveBeenCalled();
  });

  it('confirming the delete dialog calls the delete API with the profile id', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    await user.click(screen.getByRole('button', { name: /delete profile/i }));

    const confirmBtn = await screen.findByRole('button', { name: /^delete$/i });
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(deleteProfileFn).toHaveBeenCalledWith('profile-1');
    });
  });

  it('cancelling the delete dialog does not call the delete API', async () => {
    const user = userEvent.setup();
    renderCard(buildProfile({ id: 'profile-1' }));

    await user.click(screen.getByRole('button', { name: /delete profile/i }));

    const cancelBtn = await screen.findByRole('button', { name: /cancel/i });
    await user.click(cancelBtn);

    await waitFor(() => {
      expect(screen.queryByText(/Are you sure you want to delete/i)).not.toBeInTheDocument();
    });
    expect(deleteProfileFn).not.toHaveBeenCalled();
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
