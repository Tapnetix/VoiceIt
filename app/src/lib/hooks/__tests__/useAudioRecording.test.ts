/// <reference types="@testing-library/jest-dom/vitest" />
import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useAudioRecording } from '@/lib/hooks/useAudioRecording';
import { PlatformProvider } from '@/platform/PlatformContext';
import type { Platform } from '@/platform/types';

// ---------------------------------------------------------------------------
// T-AR-01: MediaStream cleanup contract for useAudioRecording
// ---------------------------------------------------------------------------
//
// Background:
//   T-LQ-01 rewrote app/src/__tests__/acceptance/s5.test.ts to target the
//   ChapterEditor "reassign dialogue" scenario, removing the only test in
//   the repo that exercised useAudioRecording at the hook level. Two
//   user-observable contracts of that hook were left without coverage:
//
//     • cancelRecording() must call .stop() on every track of the active
//       MediaStream (i.e. release the microphone so the OS LED goes off).
//     • The cleanup effect on unmount must do the same — a parent that
//       tears down the recorder mid-recording (closed dialog, route
//       change, tab swap that unmounts the form) must not leak the mic.
//
//   These are the contracts caller surfaces such as ProfileForm.tsx
//   (lines ~894-907, the Tabs onValueChange handler) rely on. The
//   helpers below (createFakeStream, installFakeMediaRecorder,
//   makePlatform) are lifted verbatim from the pre-T-LQ-01 s5 test.
//
// Boundary policy:
//   • The real useAudioRecording hook runs end-to-end (renderHook).
//   • Only browser-global surfaces jsdom does not implement are stubbed:
//     navigator.mediaDevices.getUserMedia and the MediaRecorder global.
//   • The PlatformProvider gets a minimal real Platform value (no
//     first-party project module is mocked).
//   • Observable outcomes are captured into plain arrays (the `stops`
//     log) so assertions name WHAT happened — "the track for stream A
//     was released" — without relying on .toHaveBeenCalled* on internal
//     collaborators.

interface FakeTrack {
  stop: () => void;
  // Stable identifier so the assertions can say which stream a stop came from
  // rather than just counting calls.
  streamLabel: string;
}

interface FakeStreamHandle {
  stream: MediaStream;
  label: string;
  tracks: FakeTrack[];
}

/**
 * Build a fake MediaStream whose tracks record their stop() invocations into
 * the shared `stopLog` array as `{ streamLabel }`. The log makes it possible
 * to assert WHICH streams were released and in which order without using
 * call-count assertions on a vi.fn().
 */
function createFakeStream(
  label: string,
  trackCount: number,
  stopLog: Array<{ streamLabel: string }>,
): FakeStreamHandle {
  const tracks: FakeTrack[] = [];
  for (let i = 0; i < trackCount; i++) {
    tracks.push({
      streamLabel: label,
      stop() {
        stopLog.push({ streamLabel: label });
      },
    });
  }
  const stream = {
    getTracks: () => tracks,
  } as unknown as MediaStream;
  return { stream, label, tracks };
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
      // Invoke onstop asynchronously — the hook installs it as an async
      // function and we want its body to run after stop() returns.
      const cb = this.onstop;
      if (cb) {
        void Promise.resolve().then(() => cb());
      }
    }
  }

  (FakeMediaRecorder as unknown as {
    isTypeSupported: (mime: string) => boolean;
  }).isTypeSupported = () => true;

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

function installMediaDevicesSingle(stream: MediaStream): void {
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: async () => stream,
    },
  });
}

function installMediaDevicesSequence(streams: MediaStream[]): void {
  let i = 0;
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: async () => {
        const s = streams[Math.min(i, streams.length - 1)];
        i += 1;
        return s;
      },
    },
  });
}

function restoreMediaDevices(): void {
  if (originalMediaDevices) {
    Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices);
  } else {
    delete (navigator as { mediaDevices?: unknown }).mediaDevices;
  }
}

function makePlatform(): Platform {
  // A minimal real Platform: only `metadata.isTauri` is read inside the hook.
  // Methods that should never fire for these scenarios throw on use so
  // accidental boundary crossings surface as test failures.
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
      checkForUpdates: unreachable(
        'updater.checkForUpdates',
      ) as Platform['updater']['checkForUpdates'],
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
      startServer: unreachable(
        'lifecycle.startServer',
      ) as Platform['lifecycle']['startServer'],
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
    return React.createElement(PlatformProvider, { platform, children });
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

describe('useAudioRecording — MediaStream cleanup', () => {
  it('cancelRecording stops every track on the active MediaStream', async () => {
    const stops: Array<{ streamLabel: string }> = [];
    const handle = createFakeStream('mic-A', 2, stops);
    installMediaDevicesSingle(handle.stream);

    const Wrapper = wrapperWithPlatform(makePlatform());
    const { result } = renderHook(() => useAudioRecording({ maxDurationSeconds: 30 }), {
      wrapper: Wrapper,
    });

    await act(async () => {
      await result.current.startRecording();
    });
    await waitFor(() => expect(result.current.isRecording).toBe(true));

    // While the recording is in progress no track has yet been released.
    expect(stops).toEqual([]);

    await act(async () => {
      result.current.cancelRecording();
    });

    // Observable outcome: every track of the active stream was stopped.
    // Two-track stream means two stop entries from "mic-A".
    expect(stops).toEqual([
      { streamLabel: 'mic-A' },
      { streamLabel: 'mic-A' },
    ]);
    // And the hook surfaces the cancelled state to its consumer.
    expect(result.current.isRecording).toBe(false);
    expect(result.current.duration).toBe(0);
  });

  it('unmounting the recorder mid-recording releases every track on the stream', async () => {
    const stops: Array<{ streamLabel: string }> = [];
    const handle = createFakeStream('mic-unmount', 3, stops);
    installMediaDevicesSingle(handle.stream);

    const Wrapper = wrapperWithPlatform(makePlatform());
    const { result, unmount } = renderHook(
      () => useAudioRecording({ maxDurationSeconds: 30 }),
      { wrapper: Wrapper },
    );

    await act(async () => {
      await result.current.startRecording();
    });
    await waitFor(() => expect(result.current.isRecording).toBe(true));
    expect(stops).toEqual([]);

    await act(async () => {
      unmount();
    });

    // The cleanup effect ran on unmount and released every track — the
    // contract that keeps the microphone from leaking when the parent
    // (e.g. ProfileForm dialog) tears down mid-recording.
    expect(stops).toEqual([
      { streamLabel: 'mic-unmount' },
      { streamLabel: 'mic-unmount' },
      { streamLabel: 'mic-unmount' },
    ]);
  });

  it(
    'a second start/cancel cycle releases the new stream without re-stopping the prior one',
    async () => {
      // Regression guard for stream-reference leaks. After cycle 1's
      // cancelRecording, the hook must drop its reference to stream A and
      // acquire a fresh stream B on the next startRecording. Cancelling
      // again must release B's tracks — and only B's. If the hook kept
      // a stale handle to A, the stop log would show A's label twice.
      const stops: Array<{ streamLabel: string }> = [];
      const streamA = createFakeStream('mic-A', 1, stops);
      const streamB = createFakeStream('mic-B', 1, stops);
      installMediaDevicesSequence([streamA.stream, streamB.stream]);

      const Wrapper = wrapperWithPlatform(makePlatform());
      const { result } = renderHook(() => useAudioRecording({ maxDurationSeconds: 30 }), {
        wrapper: Wrapper,
      });

      // Cycle 1: start + cancel — should release exactly the tracks of A.
      await act(async () => {
        await result.current.startRecording();
      });
      await waitFor(() => expect(result.current.isRecording).toBe(true));

      await act(async () => {
        result.current.cancelRecording();
      });

      expect(stops).toEqual([{ streamLabel: 'mic-A' }]);

      // Cycle 2: start again — new getUserMedia call hands back stream B.
      await act(async () => {
        await result.current.startRecording();
      });
      await waitFor(() => expect(result.current.isRecording).toBe(true));

      // No additional stops occurred between cycle 1's cancel and cycle 2's
      // start (proving start did not double-stop the previous stream).
      expect(stops).toEqual([{ streamLabel: 'mic-A' }]);

      await act(async () => {
        result.current.cancelRecording();
      });

      // Observable outcome: cycle 2's cancel released only B's track.
      // A's stop count is unchanged (no stale reference), B's track was
      // released exactly once.
      expect(stops).toEqual([
        { streamLabel: 'mic-A' },
        { streamLabel: 'mic-B' },
      ]);
      expect(result.current.isRecording).toBe(false);
    },
  );
});
