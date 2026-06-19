/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// ── Mocks ─────────────────────────────────────────────────────────────────────
// We mock the data hook and the UI store so we can drive ProfileList's public
// contract from the test, plus stub out the child components so each assertion
// is about ProfileList's own behavior (filtering, sorting, empty-state, error,
// loading) rather than the children.

type FakeProfile = {
  id: string;
  name: string;
  description?: string;
  language: string;
  voice_type: 'cloned' | 'preset' | 'designed';
  preset_engine?: string;
  generation_count: number;
  sample_count: number;
  created_at: string;
  updated_at: string;
};

const useProfilesReturn: {
  data: FakeProfile[] | undefined;
  isLoading: boolean;
  error: Error | null;
} = {
  data: [],
  isLoading: false,
  error: null,
};

vi.mock('@/lib/hooks/useProfiles', () => ({
  useProfiles: () => useProfilesReturn,
}));

// The store under test exposes three pieces of state read by selectors:
const storeState: {
  setProfileDialogOpen: (open: boolean) => void;
  selectedEngine: string;
  selectedProfileId: string | null;
} = {
  setProfileDialogOpen: vi.fn(),
  selectedEngine: 'qwen',
  selectedProfileId: null,
};

vi.mock('@/stores/uiStore', () => ({
  useUIStore: (selector: (state: typeof storeState) => unknown) => selector(storeState),
}));

// Stub ProfileCard so we can observe what ProfileList passes to it: the profile
// id (ordering) and the disabled flag (supported filtering).
vi.mock('@/components/VoiceProfiles/ProfileCard', () => ({
  ProfileCard: ({
    profile,
    disabled,
  }: {
    profile: FakeProfile;
    disabled?: boolean;
  }) => (
    <div
      data-testid="profile-card"
      data-profile-id={profile.id}
      data-disabled={disabled ? 'true' : 'false'}
    >
      {profile.name}
    </div>
  ),
}));

// ProfileForm is always mounted by ProfileList — stub it so we don't need to
// mock its many dependencies.
vi.mock('@/components/VoiceProfiles/ProfileForm', () => ({
  ProfileForm: () => <div data-testid="profile-form" />,
}));

// jsdom doesn't implement scrollIntoView; ProfileList calls it inside an rAF
// when selectedProfileId is set.
Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
  configurable: true,
  writable: true,
  value: vi.fn(),
});

import { ProfileList } from '@/components/VoiceProfiles/ProfileList';

function makeProfile(
  id: string,
  overrides: Partial<FakeProfile> = {},
): FakeProfile {
  return {
    id,
    name: `Profile ${id}`,
    language: 'en',
    voice_type: 'cloned',
    generation_count: 0,
    sample_count: 0,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useProfilesReturn.data = [];
  useProfilesReturn.isLoading = false;
  useProfilesReturn.error = null;
  storeState.setProfileDialogOpen = vi.fn();
  storeState.selectedEngine = 'qwen';
  storeState.selectedProfileId = null;
});

describe('ProfileList', () => {
  it('renders nothing while profiles are loading', () => {
    useProfilesReturn.data = undefined;
    useProfilesReturn.isLoading = true;

    const { container } = render(<ProfileList />);

    // The component returns null while loading; nothing should be queryable.
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId('profile-card')).not.toBeInTheDocument();
    expect(screen.queryByTestId('profile-form')).not.toBeInTheDocument();
  });

  it('shows the error message when the profiles query fails', () => {
    useProfilesReturn.data = undefined;
    useProfilesReturn.error = new Error('boom');

    render(<ProfileList />);

    // The error message includes the underlying message via i18n interpolation.
    expect(screen.getByText(/error loading profiles/i)).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
    // No cards/form when in error state.
    expect(screen.queryByTestId('profile-card')).not.toBeInTheDocument();
    expect(screen.queryByTestId('profile-form')).not.toBeInTheDocument();
  });

  it('renders the empty-state CTA when there are zero profiles and opens the dialog on click', async () => {
    useProfilesReturn.data = [];

    const setDialogOpen = vi.fn();
    storeState.setProfileDialogOpen = setDialogOpen;

    render(<ProfileList />);

    // Empty-state copy is present, no cards are rendered.
    expect(screen.getByText(/no voice profiles yet/i)).toBeInTheDocument();
    expect(screen.queryByTestId('profile-card')).not.toBeInTheDocument();

    // The "Create Voice" CTA opens the profile dialog.
    const createBtn = screen.getByRole('button', { name: /create voice/i });
    await userEvent.click(createBtn);

    expect(setDialogOpen).toHaveBeenCalledWith(true);

    // The ProfileForm is always mounted.
    expect(screen.getByTestId('profile-form')).toBeInTheDocument();
  });

  it('renders a card for every profile when the list is non-empty', () => {
    useProfilesReturn.data = [
      makeProfile('a', { name: 'Alice' }),
      makeProfile('b', { name: 'Bob' }),
      makeProfile('c', { name: 'Carol' }),
    ];

    render(<ProfileList />);

    const cards = screen.getAllByTestId('profile-card');
    expect(cards).toHaveLength(3);
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Bob')).toBeInTheDocument();
    expect(screen.getByText('Carol')).toBeInTheDocument();

    // The empty-state copy is gone, and the form is still mounted.
    expect(screen.queryByText(/no voice profiles yet/i)).not.toBeInTheDocument();
    expect(screen.getByTestId('profile-form')).toBeInTheDocument();
  });

  it('on a non-preset engine, cloned/designed profiles are enabled and preset ones are disabled', () => {
    storeState.selectedEngine = 'qwen'; // not in PRESET_ENGINES
    useProfilesReturn.data = [
      makeProfile('cloned-1', { voice_type: 'cloned' }),
      makeProfile('designed-1', { voice_type: 'designed' }),
      makeProfile('preset-kokoro', { voice_type: 'preset', preset_engine: 'kokoro' }),
    ];

    render(<ProfileList />);

    const cards = screen.getAllByTestId('profile-card');
    const byId = Object.fromEntries(
      cards.map((c) => [c.getAttribute('data-profile-id'), c.getAttribute('data-disabled')]),
    );

    expect(byId['cloned-1']).toBe('false');
    expect(byId['designed-1']).toBe('false');
    expect(byId['preset-kokoro']).toBe('true');
  });

  it('on a preset engine, only matching-preset profiles are enabled; non-preset and other-preset profiles are disabled', () => {
    storeState.selectedEngine = 'kokoro';
    useProfilesReturn.data = [
      makeProfile('cloned-1', { voice_type: 'cloned' }),
      makeProfile('preset-kokoro', { voice_type: 'preset', preset_engine: 'kokoro' }),
      makeProfile('preset-qwen', { voice_type: 'preset', preset_engine: 'qwen_custom_voice' }),
    ];

    render(<ProfileList />);

    const cards = screen.getAllByTestId('profile-card');
    const byId = Object.fromEntries(
      cards.map((c) => [c.getAttribute('data-profile-id'), c.getAttribute('data-disabled')]),
    );

    expect(byId['cloned-1']).toBe('true');
    expect(byId['preset-kokoro']).toBe('false');
    expect(byId['preset-qwen']).toBe('true');
  });

  it('also treats qwen_custom_voice as a preset engine', () => {
    storeState.selectedEngine = 'qwen_custom_voice';
    useProfilesReturn.data = [
      makeProfile('cloned-1', { voice_type: 'cloned' }),
      makeProfile('preset-qwen', { voice_type: 'preset', preset_engine: 'qwen_custom_voice' }),
      makeProfile('preset-kokoro', { voice_type: 'preset', preset_engine: 'kokoro' }),
    ];

    render(<ProfileList />);

    const cards = screen.getAllByTestId('profile-card');
    const byId = Object.fromEntries(
      cards.map((c) => [c.getAttribute('data-profile-id'), c.getAttribute('data-disabled')]),
    );

    expect(byId['cloned-1']).toBe('true');
    expect(byId['preset-qwen']).toBe('false');
    expect(byId['preset-kokoro']).toBe('true');
  });

  it('sorts supported profiles before unsupported ones', () => {
    // Engine is "qwen" (non-preset), so cloned/designed are supported and presets are not.
    storeState.selectedEngine = 'qwen';
    useProfilesReturn.data = [
      makeProfile('preset-first', { voice_type: 'preset', preset_engine: 'kokoro' }),
      makeProfile('cloned-mid', { voice_type: 'cloned' }),
      makeProfile('preset-last', { voice_type: 'preset', preset_engine: 'kokoro' }),
      makeProfile('designed-late', { voice_type: 'designed' }),
    ];

    render(<ProfileList />);

    const cards = screen.getAllByTestId('profile-card');
    const orderedIds = cards.map((c) => c.getAttribute('data-profile-id'));

    // The two supported ones (cloned-mid, designed-late) come first; the two
    // unsupported preset profiles come after. Their internal relative order is
    // preserved by a stable sort, but we only assert the partitioning here.
    const supportedSlice = orderedIds.slice(0, 2);
    const unsupportedSlice = orderedIds.slice(2);
    expect(supportedSlice).toEqual(expect.arrayContaining(['cloned-mid', 'designed-late']));
    expect(unsupportedSlice).toEqual(expect.arrayContaining(['preset-first', 'preset-last']));
  });

  it('shows the unsupported-note hint when at least one unsupported profile is present', () => {
    storeState.selectedEngine = 'qwen';
    useProfilesReturn.data = [
      makeProfile('cloned-1', { voice_type: 'cloned' }),
      makeProfile('preset-1', { voice_type: 'preset', preset_engine: 'kokoro' }),
    ];

    render(<ProfileList />);

    expect(screen.getByText(/only supported voice profiles/i)).toBeInTheDocument();
  });

  it('hides the unsupported-note hint when every profile is supported', () => {
    storeState.selectedEngine = 'qwen';
    useProfilesReturn.data = [
      makeProfile('cloned-1', { voice_type: 'cloned' }),
      makeProfile('designed-1', { voice_type: 'designed' }),
    ];

    render(<ProfileList />);

    expect(screen.queryByText(/only supported voice profiles/i)).not.toBeInTheDocument();
  });

  it('treats a missing profiles array (undefined data) as an empty list', () => {
    // isLoading is false and error is null, but data is still undefined — this
    // can happen briefly with react-query. The component should render the
    // empty-state CTA, not crash.
    useProfilesReturn.data = undefined;

    render(<ProfileList />);

    expect(screen.getByText(/no voice profiles yet/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create voice/i })).toBeInTheDocument();
  });

  it('scrolls the selected profile into view via requestAnimationFrame when selectedProfileId changes', async () => {
    // Stub rAF so we can flush the queued callback synchronously.
    const rafCallbacks: FrameRequestCallback[] = [];
    const rafSpy = vi
      .spyOn(window, 'requestAnimationFrame')
      .mockImplementation((cb) => {
        rafCallbacks.push(cb);
        return rafCallbacks.length;
      });
    const cancelSpy = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
    const scrollIntoViewMock = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoViewMock,
    });

    storeState.selectedEngine = 'qwen';
    storeState.selectedProfileId = 'cloned-1';
    useProfilesReturn.data = [makeProfile('cloned-1', { voice_type: 'cloned' })];

    render(<ProfileList />);

    // rAF was queued; flush the callback to trigger the scroll.
    expect(rafSpy).toHaveBeenCalled();
    act(() => {
      rafCallbacks.forEach((cb) => cb(0));
    });

    expect(scrollIntoViewMock).toHaveBeenCalledWith(
      expect.objectContaining({ behavior: 'smooth', block: 'nearest' }),
    );

    rafSpy.mockRestore();
    cancelSpy.mockRestore();
  });

  it('does not attempt to scroll when no profile is selected', () => {
    const rafSpy = vi.spyOn(window, 'requestAnimationFrame');

    storeState.selectedProfileId = null;
    useProfilesReturn.data = [makeProfile('cloned-1', { voice_type: 'cloned' })];

    render(<ProfileList />);

    // The effect runs but bails out early before queuing rAF.
    expect(rafSpy).not.toHaveBeenCalled();
    rafSpy.mockRestore();
  });
});
