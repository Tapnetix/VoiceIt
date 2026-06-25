/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EventCallback, UnlistenFn } from '@tauri-apps/api/event';
import type { FocusSnapshot } from '@/lib/api/types';
import type { UseCaptureRecordingSessionResult } from '@/lib/hooks/useCaptureRecordingSession';

// ── Tauri OS/runtime boundary mocks ──────────────────────────────────────────
//
// `@tauri-apps/api/core` and `@tauri-apps/api/event` are the runtime IPC bridge
// to the Rust side. They are the OS boundary for this component, not
// first-party code, so mocking them here is the intended seam (see design.md
// §4 "S19/S20 macOS-specificity gap" — the unit layer asserts component
// reaction logic given mocked IPC events; the real OS/Rust side is verified
// by the tauri-driver E2E in S18).
//
// We capture each `listen()` registration so tests can fire payloads through
// the same callback Rust would have invoked, and so we can later confirm that
// the unlisten functions returned to the component are the ones invoked on
// unmount.

interface ListenRegistration<T = unknown> {
  event: string;
  handler: EventCallback<T>;
  unlisten: UnlistenFn;
}

const listenRegistrations: ListenRegistration[] = [];
const unlistensCalled: UnlistenFn[] = [];
const invokeCalls: Array<{ cmd: string; args: Record<string, unknown> | undefined }> = [];
const emitCalls: Array<{ event: string; payload: unknown }> = [];

vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(async (cmd: string, args?: Record<string, unknown>) => {
    invokeCalls.push({ cmd, args });
    return undefined;
  }),
}));

vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn(async <T,>(event: string, handler: EventCallback<T>) => {
    const unlisten: UnlistenFn = vi.fn(() => {
      unlistensCalled.push(unlisten);
    });
    listenRegistrations.push({
      event,
      handler: handler as EventCallback<unknown>,
      unlisten,
    });
    return unlisten;
  }),
  emit: vi.fn(async (event: string, payload?: unknown) => {
    emitCalls.push({ event, payload });
    return undefined;
  }),
}));

// ── First-party contract boundary: useCaptureRecordingSession ────────────────
//
// The recording-session hook owns the MediaRecorder pipeline + react-query
// mutation chain + apiClient calls. It is explicitly excluded from the unit
// coverage gate (vitest.config.ts:42) and is verified end-to-end at the S21
// acceptance scenario against the real Tauri runtime. From DictateWindow's
// point of view, the hook is the contract it dispatches event payloads into
// and consumes pill state from — so we substitute a hand-rolled fake here so
// the test can observe DictateWindow's own behavior: which `listen()` events
// it subscribes to, which session methods each event drives, and how the
// pill state surfaces into the DOM. (Same boundary-mock discipline as
// BookImport.test.tsx mocking `useBooks`: that wraps network I/O via
// react-query; this wraps audio I/O + IPC via react-query and MediaRecorder.)

let currentSession: UseCaptureRecordingSessionResult;
const startRecordingSpy = vi.fn();
const stopRecordingSpy = vi.fn();
const toggleRecordingSpy = vi.fn();
const dismissErrorSpy = vi.fn();
const uploadFileSpy = vi.fn();
const refineSpy = vi.fn();

function makeSession(
  overrides: Partial<UseCaptureRecordingSessionResult> = {},
): UseCaptureRecordingSessionResult {
  return {
    pillState: 'hidden',
    pillElapsedMs: 0,
    errorMessage: null,
    isRecording: false,
    isUploading: false,
    isRefining: false,
    startRecording: startRecordingSpy,
    stopRecording: stopRecordingSpy,
    toggleRecording: toggleRecordingSpy,
    dismissError: dismissErrorSpy,
    uploadFile: uploadFileSpy,
    refine: refineSpy,
    ...overrides,
  };
}

// Captured so individual tests can read what payload was passed to
// `onFinalText` if they need to trigger the auto-paste branch.
let lastOnFinalText:
  | ((
      text: string,
      capture: unknown,
      allowAutoPaste: boolean,
    ) => void | Promise<void>)
  | undefined;

vi.mock('@/lib/hooks/useCaptureRecordingSession', () => ({
  useCaptureRecordingSession: (
    options: {
      onFinalText?: (
        text: string,
        capture: unknown,
        allowAutoPaste: boolean,
      ) => void | Promise<void>;
    } = {},
  ) => {
    lastOnFinalText = options.onFinalText;
    return currentSession;
  },
}));

// Reset module-level capture state between tests so listener arrays don't bleed.
beforeEach(() => {
  listenRegistrations.length = 0;
  unlistensCalled.length = 0;
  invokeCalls.length = 0;
  emitCalls.length = 0;
  startRecordingSpy.mockClear();
  stopRecordingSpy.mockClear();
  toggleRecordingSpy.mockClear();
  dismissErrorSpy.mockClear();
  uploadFileSpy.mockClear();
  refineSpy.mockClear();
  currentSession = makeSession();
  lastOnFinalText = undefined;
});

afterEach(() => {
  vi.restoreAllMocks();
});

import { DictateWindow } from '@/components/DictateWindow/DictateWindow';

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Drains the microtask queue so the `listen()` promises returned to the
 * component resolve (the component's effect awaits them before the unlisten
 * functions exist). Without this, the unmount cleanup would be running before
 * the registration promise has settled.
 */
async function flushListens(): Promise<void> {
  // Two macrotasks: the first lets `listen(...)` resolve, the second lets
  // any `.then(fn => fn())` chain run.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function findListener<T = unknown>(event: string): ListenRegistration<T> {
  const match = listenRegistrations.find((r) => r.event === event);
  if (!match) {
    throw new Error(
      `Expected DictateWindow to subscribe to "${event}" but registrations were: ` +
        JSON.stringify(listenRegistrations.map((r) => r.event)),
    );
  }
  return match as ListenRegistration<T>;
}

const fakeFocus: FocusSnapshot = {
  pid: 4242,
  bundle_id: 'com.example.test',
  role: 'AXTextField',
};

// ── Tests: chord routing into the recording session ─────────────────────────

describe('DictateWindow — dictate:start chord', () => {
  it('drives startRecording() on the session when Rust fires dictate:start', async () => {
    render(<DictateWindow />);
    await flushListens();

    const startReg = findListener<{ focus: FocusSnapshot | null }>('dictate:start');

    await act(async () => {
      startReg.handler({
        event: 'dictate:start',
        id: 1,
        payload: { focus: fakeFocus },
      });
    });

    // Behavior shape on the session boundary: the chord must trigger exactly
    // one parameterless startRecording() call. Asserting the captured calls
    // array (not a count) so a regression that started accepting an argument
    // — or stopped fire-once semantics — would change the recorded shape.
    expect(startRecordingSpy.mock.calls).toEqual([[]]);
    // And it must NOT have flipped the stop pathway by mistake.
    expect(stopRecordingSpy.mock.calls).toEqual([]);
  });

  it('subscribes via @tauri-apps event listen() at the dictate:start name', async () => {
    render(<DictateWindow />);
    await flushListens();

    // Observable shape at the IPC boundary: DictateWindow registered the
    // exact event names Rust emits (typo-resistant). We assert on the set,
    // not the order, since the component subscribes to chord + speak in
    // separate effects whose ordering is an implementation detail.
    const registeredEvents = listenRegistrations.map((r) => r.event).sort();
    expect(registeredEvents).toEqual(
      ['dictate:speak-end', 'dictate:speak-start', 'dictate:start', 'dictate:stop'].sort(),
    );
  });

  it('tolerates a dictate:start payload with a null focus snapshot without throwing', async () => {
    render(<DictateWindow />);
    await flushListens();

    const startReg = findListener<{ focus: FocusSnapshot | null }>('dictate:start');

    await act(async () => {
      startReg.handler({
        event: 'dictate:start',
        id: 2,
        payload: { focus: null },
      });
    });

    // Even with a null focus, the chord still has to start recording — the
    // late-arriving paste pipeline is the part that consults focus, not the
    // record-start. Asserting the shape of the call (no arguments) catches a
    // regression that started forwarding the payload by mistake.
    expect(startRecordingSpy.mock.calls).toEqual([[]]);
  });
});

describe('DictateWindow — dictate:stop chord', () => {
  it('drives stopRecording() when the chord fires AND the session is currently recording', async () => {
    currentSession = makeSession({ isRecording: true, pillState: 'recording' });
    render(<DictateWindow />);
    await flushListens();

    const stopReg = findListener('dictate:stop');

    await act(async () => {
      stopReg.handler({
        event: 'dictate:stop',
        id: 3,
        payload: undefined,
      });
    });

    // Behavior shape: exactly one parameterless stopRecording(); no spurious
    // restart call. Inspecting the recorded calls (not a count) so the next
    // reviewer sees what was actually delivered to the boundary.
    expect(stopRecordingSpy.mock.calls).toEqual([[]]);
    expect(startRecordingSpy.mock.calls).toEqual([]);
  });

  it('does NOT call stopRecording() when the chord fires while the session is idle', async () => {
    currentSession = makeSession({ isRecording: false, pillState: 'hidden' });
    render(<DictateWindow />);
    await flushListens();

    const stopReg = findListener('dictate:stop');

    await act(async () => {
      stopReg.handler({
        event: 'dictate:stop',
        id: 4,
        payload: undefined,
      });
    });

    // The chord is a toggle the user can spam — when nothing is recording,
    // the component must NOT call stop (which would no-op on a real recorder
    // but, on the spied surface, would still register a spurious call). The
    // boundary stays untouched.
    expect(stopRecordingSpy.mock.calls).toEqual([]);
  });
});

// ── Tests: cleanup on unmount ────────────────────────────────────────────────

describe('DictateWindow — listener cleanup on unmount', () => {
  it('invokes every unlisten function returned by listen() when the component unmounts', async () => {
    const { unmount } = render(<DictateWindow />);
    await flushListens();

    // Snapshot the unlisten fns that were handed to the component before
    // tearing it down so we can compare what was released vs. what was held.
    const expectedUnlistens = listenRegistrations.map((r) => r.unlisten);
    expect(expectedUnlistens.length).toBe(4); // dictate:start, dictate:stop, speak-start, speak-end

    await act(async () => {
      unmount();
      // The cleanup pattern is `for (const p of unlistens) p.then(fn => fn())`
      // — flush microtasks twice so each .then resolves.
      await Promise.resolve();
      await Promise.resolve();
    });

    // Observable boundary shape: every unlisten the component received was
    // invoked exactly once. We compare sorted-by-identity rather than
    // assuming a specific cleanup order because the component fans out into
    // two `useEffect` blocks whose teardown order is a React internal detail.
    const releasedSet = new Set(unlistensCalled);
    const expectedSet = new Set(expectedUnlistens);
    expect(releasedSet.size).toBe(expectedSet.size);
    for (const fn of expectedSet) {
      expect(releasedSet.has(fn)).toBe(true);
    }
    // And no extra unlisten calls leaked from elsewhere.
    expect(unlistensCalled.length).toBe(expectedUnlistens.length);
  });
});

// ── Tests: emit() side effects from state transitions ────────────────────────

describe('DictateWindow — dictate:hide emission on hidden state', () => {
  it('emits dictate:hide to Rust when the effective pill state is hidden', async () => {
    currentSession = makeSession({ pillState: 'hidden' });
    render(<DictateWindow />);
    await flushListens();

    // Behavior shape at the emit boundary: the hidden-state effect fires
    // exactly one `dictate:hide` event with no payload. We filter by event
    // name (other emits from other effects don't pollute this assertion).
    const hideEmits = emitCalls.filter((e) => e.event === 'dictate:hide');
    expect(hideEmits).toEqual([{ event: 'dictate:hide', payload: undefined }]);
  });

  it('does NOT emit dictate:hide while the session is actively recording', async () => {
    currentSession = makeSession({ pillState: 'recording', isRecording: true });
    render(<DictateWindow />);
    await flushListens();

    // The pill is supposed to be visible — emitting hide would tell Rust to
    // park the window off-screen mid-recording, which would be a regression.
    const hideEmits = emitCalls.filter((e) => e.event === 'dictate:hide');
    expect(hideEmits).toEqual([]);
  });
});

// ── Tests: paste pipeline via invoke() on final text ─────────────────────────

describe('DictateWindow — paste_final_text invoke pipeline', () => {
  it('invokes paste_final_text with the final text + focus snapshot after a successful chord cycle', async () => {
    render(<DictateWindow />);
    await flushListens();

    // 1. Chord-start delivers a focus snapshot — the component must stash it.
    const startReg = findListener<{ focus: FocusSnapshot | null }>('dictate:start');
    await act(async () => {
      startReg.handler({
        event: 'dictate:start',
        id: 10,
        payload: { focus: fakeFocus },
      });
    });

    // 2. The session's onFinalText callback (registered with the hook) fires
    //    when the refined text lands. We invoke it through the captured
    //    reference, matching the contract the real hook honors.
    expect(lastOnFinalText).toBeTypeOf('function');
    const capture = {
      id: 'cap-1',
      audio_path: '/x.wav',
      source: 'dictation' as const,
      transcript_raw: 'hello world',
      created_at: '2026-06-25T00:00:00Z',
    };
    await act(async () => {
      await lastOnFinalText!('hello world', capture, true);
    });

    // Behavior shape: the paste boundary received exactly one call with the
    // command name Rust registered and the (text, focus) tuple it expects.
    // Asserting the array shape (not a count) so a regression that started
    // packaging args differently — or fired the call twice — would surface
    // here.
    expect(invokeCalls).toEqual([
      {
        cmd: 'paste_final_text',
        args: { text: 'hello world', focus: fakeFocus },
      },
    ]);
  });

  it('does NOT invoke paste_final_text when allowAutoPaste is false', async () => {
    render(<DictateWindow />);
    await flushListens();

    const startReg = findListener<{ focus: FocusSnapshot | null }>('dictate:start');
    await act(async () => {
      startReg.handler({
        event: 'dictate:start',
        id: 11,
        payload: { focus: fakeFocus },
      });
    });

    const capture = {
      id: 'cap-2',
      audio_path: '/y.wav',
      source: 'dictation' as const,
      transcript_raw: 'do not paste',
      created_at: '2026-06-25T00:00:00Z',
    };
    await act(async () => {
      await lastOnFinalText!('do not paste', capture, /* allowAutoPaste */ false);
    });

    // Boundary stays untouched — the user's "auto-paste off" setting MUST
    // gate the IPC call entirely, not just suppress the side effect inside
    // Rust. Asserting an empty calls array (not a not.toHaveBeenCalled
    // matcher) so the recorded shape is the assertion target.
    expect(invokeCalls.filter((c) => c.cmd === 'paste_final_text')).toEqual([]);
  });

  it('emits system:accessibility-missing when paste_final_text rejects with an accessibility error', async () => {
    // Re-arrange the invoke mock so paste_final_text fails the way the Rust
    // side does when the app lacks Accessibility permission.
    const core = await import('@tauri-apps/api/core');
    (core.invoke as ReturnType<typeof vi.fn>).mockImplementationOnce(
      async (cmd: string, args?: Record<string, unknown>) => {
        invokeCalls.push({ cmd, args });
        throw new Error('Accessibility permission denied');
      },
    );
    // Silence the component's console.warn for the failed paste so test
    // output stays clean; the warning itself isn't a contract we're asserting.
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    render(<DictateWindow />);
    await flushListens();

    const startReg = findListener<{ focus: FocusSnapshot | null }>('dictate:start');
    await act(async () => {
      startReg.handler({
        event: 'dictate:start',
        id: 12,
        payload: { focus: fakeFocus },
      });
    });

    const capture = {
      id: 'cap-3',
      audio_path: '/z.wav',
      source: 'dictation' as const,
      transcript_raw: 'needs accessibility',
      created_at: '2026-06-25T00:00:00Z',
    };
    await act(async () => {
      await lastOnFinalText!('needs accessibility', capture, true);
    });

    // Behavior shape at the emit boundary: an accessibility-missing event
    // must have been emitted exactly once with no payload, so the main
    // window can surface the permission prompt. Filter to the event name to
    // isolate from the dictate:hide emit that fires on the initial hidden
    // state.
    const a11yEmits = emitCalls.filter(
      (e) => e.event === 'system:accessibility-missing',
    );
    expect(a11yEmits).toEqual([
      { event: 'system:accessibility-missing', payload: undefined },
    ]);

    warnSpy.mockRestore();
  });
});

// ── Tests: rendering the CapturePill from session state ─────────────────────

describe('DictateWindow — pill rendering surface', () => {
  it('does not render the pill when the session is hidden', async () => {
    currentSession = makeSession({ pillState: 'hidden' });
    render(<DictateWindow />);
    await flushListens();

    // i18n labels for the visible pill states; if any of them appear in the
    // DOM, the pill mounted unexpectedly.
    expect(screen.queryByText(/recording/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/transcribing/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/refining/i)).not.toBeInTheDocument();
  });

  it('renders the Recording pill (with stop control) while the session is recording', async () => {
    currentSession = makeSession({
      pillState: 'recording',
      isRecording: true,
      pillElapsedMs: 1500,
    });
    render(<DictateWindow />);
    await flushListens();

    // Observable DOM: the Recording label is visible AND the Stop Recording
    // control is wired up. CapturePill is a real child component here — we
    // don't stub it — so this also verifies the wiring between
    // DictateWindow's effective state computation and the pill's API.
    expect(screen.getByText(/recording/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /stop recording/i })).toBeInTheDocument();
  });

  it('renders the Transcribing pill and HIDES the stop control while transcribing', async () => {
    currentSession = makeSession({
      pillState: 'transcribing',
      isRecording: false,
      pillElapsedMs: 3200,
    });
    render(<DictateWindow />);
    await flushListens();

    expect(screen.getByText(/transcribing/i)).toBeInTheDocument();
    // Once the recorder has handed off, there's nothing to stop — the stop
    // button must not be present, otherwise the user could "stop" a network
    // request, which is a UX trap.
    expect(screen.queryByRole('button', { name: /stop recording/i })).not.toBeInTheDocument();
  });
});
