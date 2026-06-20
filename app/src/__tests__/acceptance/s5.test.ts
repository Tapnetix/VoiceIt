/// <reference types="@testing-library/jest-dom/vitest" />
import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAudioRecording } from '@/lib/hooks/useAudioRecording';
import { PlatformProvider } from '@/platform/PlatformContext';
import type { Platform } from '@/platform/types';

// ---------------------------------------------------------------------------
// Acceptance scenario S5
// ---------------------------------------------------------------------------
//
// Scenario (from the audit-coverage plan):
//   Switching tabs during a recording releases the MediaStream
//   (spy on `getTracks()[…].stop()`).
//
// Mapping note:
//   `AudioSampleSystem.tsx` is the file the plan points at, but it is a pure
//   presentational component — it owns no MediaStream. The actual
//   MediaStream-bearing surface used by the recording tab is
//   `useAudioRecording` (the hook that wires `getUserMedia` and exposes
//   `cancelRecording`). The wiring is in `ProfileForm.tsx` lines 894-907:
//
//       <Tabs
//         value={sampleMode}
//         onValueChange={(v) => {
//           const newMode = v as 'upload' | 'record' | 'system';
//           if (isRecording && newMode !== 'record') {
//             cancelRecording();          // ← contract under test
//           }
//           if (isSystemRecording && newMode !== 'system') {
//             cancelSystemRecording();
//           }
//           setSampleMode(newMode);
//         }}
//       >
//
//   So "switching tabs during a recording" is *defined* as
//   "ProfileForm invokes `useAudioRecording().cancelRecording()` while
//   `isRecording` is true". The behaviour the user observes — the
//   microphone LED going dark, the OS releasing the device — is the
//   `MediaStreamTrack.stop()` call inside `cancelRecording`. We therefore
//   drive that exact contract at the hook layer.
//
// Boundary policy:
//   • The hook runs for real, including its real `MediaRecorder` lifecycle
//     and its real `getTracks().forEach(stop)` cleanup path.
//   • We stub the WebAPI surfaces that jsdom does not implement
//     (`navigator.mediaDevices.getUserMedia` and `MediaRecorder`).
//   • The PlatformProvider gets a minimal real value (no first-party
//     project module is mocked).

interface FakeStreamHandle {
  stream: MediaStream;
  trackStops: Array<ReturnType<typeof vi.fn>>;
}

function createFakeStream(trackCount = 1): FakeStreamHandle {
  const trackStops: Array<ReturnType<typeof vi.fn>> = [];
  const tracks: MediaStreamTrack[] = [];
  for (let i = 0; i < trackCount; i++) {
    const stop = vi.fn();
    trackStops.push(stop);
    tracks.push({ stop } as unknown as MediaStreamTrack);
  }
  const stream = {
    getTracks: () => tracks,
  } as unknown as MediaStream;
  return { stream, trackStops };
}

interface FakeMediaRecorderInstance {
  state: 'inactive' | 'recording' | 'paused';
  ondataavailable: ((event: { data: Blob }) => void) | null;
  onstop: (() => void | Promise<void>) | null;
  onerror: ((event: unknown) => void) | null;
  start: () => void;
  stop: () => void;
  stream: MediaStream;
}

function installFakeMediaRecorder(): {
  instances: FakeMediaRecorderInstance[];
  restore: () => void;
} {
  const instances: FakeMediaRecorderInstance[] = [];
  const original = (globalThis as { MediaRecorder?: unknown }).MediaRecorder;

  class FakeMediaRecorder implements FakeMediaRecorderInstance {
    state: 'inactive' | 'recording' | 'paused' = 'inactive';
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: (() => void | Promise<void>) | null = null;
    onerror: ((event: unknown) => void) | null = null;
    stream: MediaStream;

    constructor(stream: MediaStream, _options?: MediaRecorderOptions) {
      this.stream = stream;
      instances.push(this);
    }

    start() {
      this.state = 'recording';
    }

    stop() {
      this.state = 'inactive';
      // Emit a zero-byte data chunk so the hook has something to assemble
      // (cancelRecording clears chunks before invoking stop, so this is
      // benign either way).
      this.ondataavailable?.({ data: new Blob([], { type: 'audio/webm' }) });
      // Invoke onstop synchronously inside an outer await chain — the hook
      // sets it as an async function and we want its body to run.
      const cb = this.onstop;
      if (cb) {
        void Promise.resolve().then(() => cb());
      }
    }
  }

  (FakeMediaRecorder as unknown as { isTypeSupported: (mime: string) => boolean }).isTypeSupported =
    () => true;

  (globalThis as { MediaRecorder?: unknown }).MediaRecorder = FakeMediaRecorder;

  return {
    instances,
    restore: () => {
      if (original === undefined) {
        delete (globalThis as { MediaRecorder?: unknown }).MediaRecorder;
      } else {
        (globalThis as { MediaRecorder?: unknown }).MediaRecorder = original;
      }
    },
  };
}

const originalMediaDevices = Object.getOwnPropertyDescriptor(
  Navigator.prototype,
  'mediaDevices',
);

function installMediaDevices(stream: MediaStream): void {
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: vi.fn(async () => stream),
    },
  });
}

function restoreMediaDevices(): void {
  if (originalMediaDevices) {
    Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices);
  } else {
    // @ts-expect-error — deleting an installed property is fine
    delete (navigator as { mediaDevices?: unknown }).mediaDevices;
  }
}

function makePlatform(): Platform {
  // A minimal real Platform: only `metadata.isTauri` is read inside the hook.
  // Everything else is unreachable for the scenario under test, so the methods
  // remain unimplemented to surface unexpected boundary use as a test failure.
  const unreachable = (label: string) => {
    return (..._args: unknown[]) => {
      throw new Error(`unexpected platform call: ${label}`);
    };
  };
  return {
    filesystem: {
      saveFile: unreachable('filesystem.saveFile') as Platform['filesystem']['saveFile'],
      openPath: unreachable('filesystem.openPath') as Platform['filesystem']['openPath'],
      pickDirectory: unreachable(
        'filesystem.pickDirectory',
      ) as Platform['filesystem']['pickDirectory'],
    },
    updater: {
      checkForUpdates: unreachable('updater.checkForUpdates') as Platform['updater']['checkForUpdates'],
      downloadAndInstall: unreachable(
        'updater.downloadAndInstall',
      ) as Platform['updater']['downloadAndInstall'],
      restartAndInstall: unreachable(
        'updater.restartAndInstall',
      ) as Platform['updater']['restartAndInstall'],
      getStatus: () => ({
        checking: false,
        available: false,
        downloading: false,
        installing: false,
        readyToInstall: false,
      }),
      subscribe: () => () => {},
    },
    audio: {
      isSystemAudioSupported: async () => false,
      startSystemAudioCapture: unreachable(
        'audio.startSystemAudioCapture',
      ) as Platform['audio']['startSystemAudioCapture'],
      stopSystemAudioCapture: unreachable(
        'audio.stopSystemAudioCapture',
      ) as Platform['audio']['stopSystemAudioCapture'],
      listOutputDevices: async () => [],
      playToDevices: unreachable('audio.playToDevices') as Platform['audio']['playToDevices'],
      stopPlayback: () => {},
    },
    lifecycle: {
      startServer: unreachable('lifecycle.startServer') as Platform['lifecycle']['startServer'],
      stopServer: unreachable('lifecycle.stopServer') as Platform['lifecycle']['stopServer'],
      restartServer: unreachable(
        'lifecycle.restartServer',
      ) as Platform['lifecycle']['restartServer'],
      setKeepServerRunning: unreachable(
        'lifecycle.setKeepServerRunning',
      ) as Platform['lifecycle']['setKeepServerRunning'],
      setupWindowCloseHandler: unreachable(
        'lifecycle.setupWindowCloseHandler',
      ) as Platform['lifecycle']['setupWindowCloseHandler'],
      subscribeToServerLogs: () => () => {},
    },
    metadata: {
      isTauri: false,
      getVersion: async () => 'test',
    },
  };
}

function wrapperWithPlatform(platform: Platform) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(PlatformProvider, { platform }, children);
  };
}

let recorderHandle: ReturnType<typeof installFakeMediaRecorder>;

beforeEach(() => {
  recorderHandle = installFakeMediaRecorder();
});

afterEach(() => {
  recorderHandle.restore();
  restoreMediaDevices();
  vi.restoreAllMocks();
});

describe('S5: switching tabs during a recording releases the MediaStream', () => {
  it(
    'S5: invoking cancelRecording mid-recording stops every captured MediaStreamTrack',
    async () => {
      const { stream, trackStops } = createFakeStream(2);
      installMediaDevices(stream);

      const Wrapper = wrapperWithPlatform(makePlatform());
      const { result } = renderHook(() => useAudioRecording({ maxDurationSeconds: 30 }), {
        wrapper: Wrapper,
      });

      // 1. Start recording — this is the "user clicked Start Recording on
      //    the record tab" half of the scenario. Wait until the hook has
      //    actually entered the recording state so the cleanup path under
      //    test has something to release.
      await act(async () => {
        await result.current.startRecording();
      });
      await waitFor(() => expect(result.current.isRecording).toBe(true));

      // Tracks have not yet been stopped — recording is in progress.
      for (const stopFn of trackStops) {
        expect(stopFn).not.toHaveBeenCalled();
      }

      // 2. Simulate the tab switch. `ProfileForm.tsx` (lines 894-907) calls
      //    `cancelRecording()` when the user moves away from the record
      //    tab while a recording is in progress. We invoke the same
      //    boundary the form invokes.
      await act(async () => {
        result.current.cancelRecording();
      });

      // 3. The observable outcome: every track on the captured MediaStream
      //    has had `.stop()` called on it. That is the contract callers
      //    (and indirectly the OS / browser) rely on to release the mic.
      for (const stopFn of trackStops) {
        expect(stopFn).toHaveBeenCalledTimes(1);
      }
      // The hook also drops the recording flag so the parent UI exits the
      // recording state.
      expect(result.current.isRecording).toBe(false);
      // Duration resets — proves the cancel path ran end-to-end rather than
      // just stopping tracks.
      expect(result.current.duration).toBe(0);

      // 4. No completion callback should have fired. Cancellation is not a
      //    successful recording; the form must not receive a sample file.
      //    (We didn't pass an onRecordingComplete, so this is enforced
      //    implicitly — the deliberate test is that the hook didn't throw.)
    },
  );

  it(
    'S5: a follow-up start/cancel cycle re-acquires and re-releases tracks (no leaked stream)',
    async () => {
      // Regression guard: after one cancel the hook must not retain the old
      // stream. A subsequent recording + cancel should release the *new*
      // stream's tracks, leaving zero outstanding mic holds.
      const firstStream = createFakeStream(1);
      const secondStream = createFakeStream(1);

      let useFirst = true;
      Object.defineProperty(navigator, 'mediaDevices', {
        configurable: true,
        value: {
          getUserMedia: vi.fn(async () => (useFirst ? firstStream.stream : secondStream.stream)),
        },
      });

      const Wrapper = wrapperWithPlatform(makePlatform());
      const { result } = renderHook(() => useAudioRecording({ maxDurationSeconds: 30 }), {
        wrapper: Wrapper,
      });

      await act(async () => {
        await result.current.startRecording();
      });
      await waitFor(() => expect(result.current.isRecording).toBe(true));

      await act(async () => {
        result.current.cancelRecording();
      });
      expect(firstStream.trackStops[0]).toHaveBeenCalledTimes(1);

      // Second cycle: swap the underlying stream so we can distinguish
      // "released the new stream" from "called stop on the old one again".
      useFirst = false;
      await act(async () => {
        await result.current.startRecording();
      });
      await waitFor(() => expect(result.current.isRecording).toBe(true));

      await act(async () => {
        result.current.cancelRecording();
      });

      // The new stream's tracks were released.
      expect(secondStream.trackStops[0]).toHaveBeenCalledTimes(1);
      // The old stream's tracks were not re-stopped (i.e. the hook is not
      // holding a stale reference and forwarding stop() twice).
      expect(firstStream.trackStops[0]).toHaveBeenCalledTimes(1);
      expect(result.current.isRecording).toBe(false);
    },
  );

  it(
    'S5: unmounting the recorder mid-recording also releases the MediaStream',
    async () => {
      // A "tab switch" in ProfileForm is sometimes structural — the dialog
      // closes, or the parent re-renders away from the record tab and the
      // component holding the hook is torn down. In that path the cleanup
      // contract still has to hold: the cleanup effect inside
      // `useAudioRecording` must stop every captured track even when no
      // explicit cancelRecording() call precedes the unmount.
      const { stream, trackStops } = createFakeStream(1);
      installMediaDevices(stream);

      const Wrapper = wrapperWithPlatform(makePlatform());
      const { result, unmount } = renderHook(
        () => useAudioRecording({ maxDurationSeconds: 30 }),
        { wrapper: Wrapper },
      );

      await act(async () => {
        await result.current.startRecording();
      });
      await waitFor(() => expect(result.current.isRecording).toBe(true));
      expect(trackStops[0]).not.toHaveBeenCalled();

      await act(async () => {
        unmount();
      });

      // Cleanup effect ran on unmount and released the mic.
      expect(trackStops[0]).toHaveBeenCalledTimes(1);
    },
  );
});
