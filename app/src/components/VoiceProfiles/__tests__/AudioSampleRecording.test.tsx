/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Stub out the shadcn form wrappers so they render without a react-hook-form context.
vi.mock('@/components/ui/form', () => ({
  FormItem: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  FormControl: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  FormMessage: () => null,
}));

// Stub the third-party visualizer so it renders deterministically without touching
// a real <canvas>/AnalyserNode pipeline. We render a sentinel element so the test
// can assert "the waveform was mounted" as an observable outcome.
vi.mock('react-sound-visualizer', () => ({
  Visualizer: ({
    audio,
    children,
  }: {
    audio: MediaStream | null;
    autoStart?: boolean;
    strokeColor?: string;
    children: (args: { canvasRef: React.RefObject<HTMLCanvasElement> }) => React.ReactNode;
  }) => (
    <div data-testid="waveform-visualizer" data-has-audio={audio ? 'true' : 'false'}>
      {children({ canvasRef: { current: null } as React.RefObject<HTMLCanvasElement> })}
    </div>
  ),
}));

import { AudioSampleRecording } from '@/components/VoiceProfiles/AudioSampleRecording';

const sampleFile = new File(['x'], 'clip.wav', { type: 'audio/wav' });

type GetUserMedia = (constraints: MediaStreamConstraints) => Promise<MediaStream>;

function installMediaDevices(getUserMedia: GetUserMedia | null): {
  stopTrack: ReturnType<typeof vi.fn>;
} {
  const stopTrack = vi.fn();
  const fakeTrack = { stop: stopTrack } as unknown as MediaStreamTrack;
  const fakeStream = {
    getTracks: () => [fakeTrack],
  } as unknown as MediaStream;

  if (getUserMedia === null) {
    // Simulate environments where mediaDevices is unavailable (e.g. some Tauri webviews).
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: undefined,
    });
  } else {
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: vi.fn(async (constraints: MediaStreamConstraints) => {
          const result = await getUserMedia(constraints);
          // If the caller resolved with their own stream, prefer it; otherwise use the fake.
          return result ?? fakeStream;
        }),
      },
    });
  }
  return { stopTrack };
}

const originalMediaDevices = Object.getOwnPropertyDescriptor(
  Navigator.prototype,
  'mediaDevices',
);

afterEach(() => {
  // Restore mediaDevices descriptor between tests so suites stay isolated.
  if (originalMediaDevices) {
    Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices);
  } else {
    delete (navigator as { mediaDevices?: unknown }).mediaDevices;
  }
  vi.restoreAllMocks();
});

describe('AudioSampleRecording — idle state', () => {
  beforeEach(() => {
    installMediaDevices(null); // no mic so the waveform path stays inert
  });

  it('shows the Start Recording button and hint when there is no file and not recording', () => {
    render(
      <AudioSampleRecording
        file={null}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
      />,
    );

    expect(screen.getByRole('button', { name: /start recording/i })).toBeInTheDocument();
    expect(screen.getByText(/click to start recording/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /stop recording/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/recording complete/i)).not.toBeInTheDocument();
  });

  it('invokes onStart when the user clicks Start Recording', async () => {
    const user = userEvent.setup();
    const onStart = vi.fn();
    render(
      <AudioSampleRecording
        file={undefined}
        isRecording={false}
        duration={0}
        onStart={onStart}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
      />,
    );

    await user.click(screen.getByRole('button', { name: /start recording/i }));

    // Behavior shape: clicking Start Recording forwards exactly one click event
    // sourced from the Start Recording button. We capture event.type and the
    // target button's accessible label so a regression that re-binds onStart to
    // a different control (or fires from a non-click lifecycle) changes shape.
    // We read `target` rather than `currentTarget` because React nulls out
    // `currentTarget` after the synthetic event is dispatched.
    const startInvocations = onStart.mock.calls.map(
      ([e]: [React.MouseEvent<HTMLButtonElement>]) => ({
        type: e.type,
        label: (e.target as HTMLElement).closest('button')?.textContent?.trim().toLowerCase(),
      }),
    );
    expect(startInvocations).toEqual([{ type: 'click', label: 'start recording' }]);
  });
});

describe('AudioSampleRecording — recording state', () => {
  beforeEach(() => {
    installMediaDevices(null);
  });

  it('shows the current duration and remaining time formatted as M:SS', () => {
    render(
      <AudioSampleRecording
        file={null}
        isRecording={true}
        duration={7}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
      />,
    );

    // Current elapsed: 7s -> "0:07"
    expect(screen.getByText('0:07')).toBeInTheDocument();
    // Remaining of the 30s cap: 30 - 7 = 23s -> "0:23 remaining"
    expect(screen.getByText(/0:23 remaining/i)).toBeInTheDocument();
    // Idle UI hidden while recording
    expect(screen.queryByRole('button', { name: /start recording/i })).not.toBeInTheDocument();
  });

  it('invokes onStop when the user clicks Stop Recording', async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(
      <AudioSampleRecording
        file={null}
        isRecording={true}
        duration={3}
        onStart={vi.fn()}
        onStop={onStop}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
      />,
    );

    await user.click(screen.getByRole('button', { name: /stop recording/i }));

    // Behavior shape: clicking Stop Recording forwards exactly one click event
    // sourced from the Stop Recording button. Asserting the label guards against
    // a regression that re-binds onStop to the wrong control (e.g. swapping it
    // with Start) — the recorded shape would then carry the wrong label.
    // `target` is used over `currentTarget` because React clears
    // `currentTarget` after synthetic event dispatch.
    const stopInvocations = onStop.mock.calls.map(
      ([e]: [React.MouseEvent<HTMLButtonElement>]) => ({
        type: e.type,
        label: (e.target as HTMLElement).closest('button')?.textContent?.trim().toLowerCase(),
      }),
    );
    expect(stopInvocations).toEqual([{ type: 'click', label: 'stop recording' }]);
  });

  it('prefers the recording UI over the file-complete UI when both are set', () => {
    // Defensive: if isRecording is true, the "Recording complete" panel must not render
    // even if a stale file was passed in.
    render(
      <AudioSampleRecording
        file={sampleFile}
        isRecording={true}
        duration={1}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
      />,
    );

    expect(screen.getByRole('button', { name: /stop recording/i })).toBeInTheDocument();
    expect(screen.queryByText(/recording complete/i)).not.toBeInTheDocument();
  });
});

describe('AudioSampleRecording — file-present state', () => {
  beforeEach(() => {
    installMediaDevices(null);
  });

  it('renders the completion panel with the file name when a file is present and not recording', () => {
    render(
      <AudioSampleRecording
        file={sampleFile}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
      />,
    );

    expect(screen.getByText(/recording complete/i)).toBeInTheDocument();
    expect(screen.getByText(/clip\.wav/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^play$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /record again/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start recording/i })).not.toBeInTheDocument();
  });

  it('labels the play/pause control as "Pause" while the audio is playing', () => {
    render(
      <AudioSampleRecording
        file={sampleFile}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={true}
      />,
    );

    expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^play$/i })).not.toBeInTheDocument();
  });

  it('invokes onPlayPause when the play/pause control is clicked', async () => {
    const user = userEvent.setup();
    const onPlayPause = vi.fn();
    render(
      <AudioSampleRecording
        file={sampleFile}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={onPlayPause}
        isPlaying={false}
      />,
    );

    await user.click(screen.getByRole('button', { name: /^play$/i }));

    // Behavior shape: clicking the play/pause icon button forwards exactly one
    // click event. The button's aria-label encodes the current toggle state
    // ("Play" when paused, "Pause" when playing); asserting on it guards
    // against a regression that inverts the play/pause icon mapping.
    // `target` is used over `currentTarget` because React clears
    // `currentTarget` after synthetic event dispatch.
    const playInvocations = onPlayPause.mock.calls.map(
      ([e]: [React.MouseEvent<HTMLButtonElement>]) => ({
        type: e.type,
        ariaLabel: (e.target as HTMLElement)
          .closest('button')
          ?.getAttribute('aria-label')
          ?.toLowerCase(),
      }),
    );
    expect(playInvocations).toEqual([{ type: 'click', ariaLabel: 'play' }]);
  });

  it('invokes onCancel when Record Again is clicked', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(
      <AudioSampleRecording
        file={sampleFile}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={onCancel}
        onPlayPause={vi.fn()}
        isPlaying={false}
      />,
    );

    await user.click(screen.getByRole('button', { name: /record again/i }));

    // Behavior shape: clicking Record Again forwards exactly one click event
    // sourced from the Record Again button. Asserting the label guards against
    // a regression where onCancel is silently re-bound to the Play control
    // (which would clobber a completed recording on a tap-to-preview).
    // `target` is used over `currentTarget` because React clears
    // `currentTarget` after synthetic event dispatch.
    const cancelInvocations = onCancel.mock.calls.map(
      ([e]: [React.MouseEvent<HTMLButtonElement>]) => ({
        type: e.type,
        label: (e.target as HTMLElement).closest('button')?.textContent?.trim().toLowerCase(),
      }),
    );
    expect(cancelInvocations).toEqual([{ type: 'click', label: 'record again' }]);
  });
});

describe('AudioSampleRecording — waveform microphone access', () => {
  it('mounts the visualizer with the captured stream once getUserMedia resolves', async () => {
    installMediaDevices(async () => {
      const track = { stop: vi.fn() } as unknown as MediaStreamTrack;
      return { getTracks: () => [track] } as unknown as MediaStream;
    });

    render(
      <AudioSampleRecording
        file={null}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        showWaveform={true}
      />,
    );

    const visualizer = await screen.findByTestId('waveform-visualizer');
    expect(visualizer).toHaveAttribute('data-has-audio', 'true');
  });

  it('does not request microphone access when showWaveform is false', async () => {
    // If the component were to call getUserMedia anyway, this rejection would
    // trigger the catch branch in AudioSampleRecording and emit the
    // "Could not access microphone" warning. We assert on those two observable
    // side effects (no warning, no visualizer) instead of a call-count.
    installMediaDevices(async () => {
      throw new Error('should not be called');
    });
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    render(
      <AudioSampleRecording
        file={null}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        showWaveform={false}
      />,
    );

    // Wait a microtask to let any (mistakenly scheduled) effect run.
    await Promise.resolve();
    // Observable: no microphone-access warning was emitted (which would only
    // happen if getUserMedia had been invoked and rejected).
    const micWarnings = warnSpy.mock.calls.filter(([msg]) =>
      typeof msg === 'string' && msg.includes('Could not access microphone'),
    );
    expect(micWarnings).toEqual([]);
    // Observable: nothing in the DOM consumes a captured stream.
    expect(screen.queryByTestId('waveform-visualizer')).not.toBeInTheDocument();
  });

  it('does not render the visualizer when the browser has no mediaDevices API', async () => {
    installMediaDevices(null);

    render(
      <AudioSampleRecording
        file={null}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        showWaveform={true}
      />,
    );

    await Promise.resolve();
    expect(screen.queryByTestId('waveform-visualizer')).not.toBeInTheDocument();
  });

  it('does not render the visualizer when getUserMedia rejects (permission denied)', async () => {
    installMediaDevices(async () => {
      throw new Error('denied');
    });
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    render(
      <AudioSampleRecording
        file={null}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        showWaveform={true}
      />,
    );

    // Observable shape: when getUserMedia rejects, AudioSampleRecording's catch
    // branch must surface a console warning whose message includes the
    // "Could not access microphone" prefix and whose payload is the rejection
    // error — so an operator reading logs can attribute the failure.
    await waitFor(() => {
      const micWarning = warnSpy.mock.calls.find(([msg]) =>
        typeof msg === 'string' && msg.includes('Could not access microphone'),
      );
      expect(micWarning).toBeDefined();
      expect(micWarning?.[1]).toBeInstanceOf(Error);
      expect((micWarning?.[1] as Error).message).toBe('denied');
    });
    expect(screen.queryByTestId('waveform-visualizer')).not.toBeInTheDocument();
  });

  it('stops microphone tracks when the component unmounts', async () => {
    const stopTrack = vi.fn();
    installMediaDevices(async () => {
      const track = { stop: stopTrack } as unknown as MediaStreamTrack;
      return { getTracks: () => [track] } as unknown as MediaStream;
    });

    const { unmount } = render(
      <AudioSampleRecording
        file={null}
        isRecording={false}
        duration={0}
        onStart={vi.fn()}
        onStop={vi.fn()}
        onCancel={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        showWaveform={true}
      />,
    );

    // Wait until the captured stream has been wired up before tearing down.
    await screen.findByTestId('waveform-visualizer');

    await act(async () => {
      unmount();
    });

    // Observable shape on the MediaStreamTrack boundary: the track must be
    // released exactly once, with no arguments (MediaStreamTrack.stop() is
    // parameterless per spec). Releasing twice would throw in real browsers;
    // not releasing leaks the microphone capture indicator.
    expect(stopTrack.mock.calls).toEqual([[]]);
  });
});
