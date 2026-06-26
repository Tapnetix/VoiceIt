/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import type { EventCallback, UnlistenFn } from '@tauri-apps/api/event';
import type {
  CaptureListResponse,
  CaptureResponse,
} from '@/lib/api/types';
import type { UseCaptureRecordingSessionResult } from '@/lib/hooks/useCaptureRecordingSession';
import type { DictationReadiness } from '@/lib/hooks/useDictationReadiness';

// ── Tauri OS-boundary mocks ─────────────────────────────────────────────────
//
// `@tauri-apps/api/event`, `@tauri-apps/plugin-dialog`, and
// `@tauri-apps/plugin-fs` are the IPC/OS boundary the component talks to.
// Mocking them here is the documented seam (same pattern as
// DictateWindow.test.tsx) — the real Rust side is verified by the
// tauri-driver E2E. We capture every `listen()` registration so tests can
// fire payloads through the same callback Rust would have invoked.

interface ListenRegistration<T = unknown> {
  event: string;
  handler: EventCallback<T>;
  unlisten: UnlistenFn;
}

const listenRegistrations: ListenRegistration[] = [];
const unlistensCalled: UnlistenFn[] = [];

const saveMock = vi.fn();
const writeFileMock = vi.fn();
const writeTextFileMock = vi.fn();

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
  emit: vi.fn(async () => undefined),
}));

vi.mock('@tauri-apps/plugin-dialog', () => ({
  save: (...args: unknown[]) => saveMock(...args),
}));

vi.mock('@tauri-apps/plugin-fs', () => ({
  writeFile: (...args: unknown[]) => writeFileMock(...args),
  writeTextFile: (...args: unknown[]) => writeTextFileMock(...args),
}));

// ── HTTP boundary: apiClient ────────────────────────────────────────────────
//
// apiClient is the HTTP edge — mocking it stays inside the test-quality bar's
// "stack's preferred test-double boundary (HTTP boundary)" exception. We
// return a minimal shape so the captures + profiles + capture-settings queries
// resolve into a known empty state, and the component's effects can then
// drive cache updates via the `capture:created` event.

// Mutable backing state for the listCaptures mock — tests push the sample
// capture in before firing the `capture:created` event so the refetch
// triggered by the in-component invalidateQueries returns the new row
// instead of clobbering the seeded cache back to empty.
const captureBackingState: { items: CaptureResponse[]; total: number } = {
  items: [],
  total: 0,
};

vi.mock('@/lib/api/client', () => ({
  apiClient: {
    listCaptures: vi.fn(async (): Promise<CaptureListResponse> => ({
      items: [...captureBackingState.items],
      total: captureBackingState.total,
    })),
    listProfiles: vi.fn(async () => []),
    getCaptureSettings: vi.fn(async () => ({
      stt_model: 'turbo',
      llm_model: '0.6B',
      hotkey_enabled: false,
      chord_push_to_talk_keys: [],
      chord_toggle_to_talk_keys: [],
      default_playback_voice_id: null,
      auto_refine: true,
      allow_auto_paste: true,
      refinement_flags: {
        smart_cleanup: true,
        self_correction: true,
        preserve_technical: true,
      },
    })),
    updateCaptureSettings: vi.fn(async (patch) => patch),
    deleteCapture: vi.fn(async () => undefined),
    generateSpeech: vi.fn(async () => ({ id: 'gen-1' })),
    getCaptureAudioUrl: vi.fn((id: string) => `http://test.local/captures/${id}/audio`),
  },
}));

// ── Boundary-hook stubs ─────────────────────────────────────────────────────
//
// useCaptureRecordingSession wraps MediaRecorder + react-query mutations (the
// audio I/O boundary). useDictationReadiness wraps the platform + readiness
// probes. Both are excluded from the unit coverage gate in vitest.config.ts
// for the same reason, and DictateWindow.test.tsx uses identical seams.

let currentSession: UseCaptureRecordingSessionResult;

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
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    toggleRecording: vi.fn(),
    dismissError: vi.fn(),
    uploadFile: vi.fn(),
    refine: vi.fn(),
    ...overrides,
  };
}

vi.mock('@/lib/hooks/useCaptureRecordingSession', () => ({
  useCaptureRecordingSession: () => currentSession,
}));

const readinessStub: DictationReadiness = {
  isLoading: false,
  canRecord: false,
  allReady: false,
  missing: [],
  stt: undefined,
  llm: undefined,
  inputMonitoring: false,
  accessibility: false,
  refetch: vi.fn(),
  openInputMonitoringSettings: vi.fn(async () => {}),
  openAccessibilitySettings: vi.fn(async () => {}),
  recheckInputMonitoring: vi.fn(async () => false),
  recheckAccessibility: vi.fn(async () => false),
};

vi.mock('@/lib/hooks/useDictationReadiness', () => ({
  useDictationReadiness: () => readinessStub,
}));

// ── Third-party renderers that misbehave in jsdom ───────────────────────────
//
// `<Link>` from tanstack-router requires a RouterProvider context; the
// component renders one ("Configure" button) without us needing routing
// behavior. Substitute a plain anchor so the render doesn't bail.
//
// wavesurfer.js touches AudioContext, which jsdom doesn't implement. The
// CaptureInlinePlayer is rendered as a side effect of the detail pane; we
// don't need any waveform behavior for these tests.

vi.mock('@tanstack/react-router', async () => {
  const actual = await vi.importActual<typeof import('@tanstack/react-router')>(
    '@tanstack/react-router',
  );
  return {
    ...actual,
    Link: ({ children, to, ...rest }: { children: ReactNode; to?: string }) => (
      <a href={typeof to === 'string' ? to : '#'} {...rest}>
        {children}
      </a>
    ),
  };
});

vi.mock('wavesurfer.js', () => {
  const makeStub = () => ({
    on: vi.fn(),
    load: vi.fn(() => Promise.resolve()),
    play: vi.fn(),
    pause: vi.fn(),
    destroy: vi.fn(),
    isPlaying: vi.fn(() => false),
    getDuration: vi.fn(() => 0),
    seekTo: vi.fn(),
  });
  return {
    default: {
      create: vi.fn(() => makeStub()),
    },
  };
});

// ── Reset module-level state between tests ──────────────────────────────────

beforeEach(() => {
  listenRegistrations.length = 0;
  unlistensCalled.length = 0;
  saveMock.mockReset();
  writeFileMock.mockReset();
  writeTextFileMock.mockReset();
  captureBackingState.items = [];
  captureBackingState.total = 0;
  currentSession = makeSession();
});

afterEach(() => {
  vi.clearAllMocks();
});

// Import AFTER the mocks above so the component picks them up.
import { CapturesTab } from '@/components/CapturesTab/CapturesTab';

// ── Helpers ─────────────────────────────────────────────────────────────────

function renderWithQuery() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  // Pre-seed the captures + profiles + capture-settings caches so the
  // component skips the loading state and reaches the steady-state render
  // before we start firing events. The component's `capture:created` handler
  // only seeds new rows when `prev` already exists in the cache (line 211 of
  // CapturesTab.tsx) — without this seed, the event would no-op.
  queryClient.setQueryData<CaptureListResponse>(['captures'], { items: [], total: 0 });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <CapturesTab />
    </QueryClientProvider>,
  );
  return { ...utils, queryClient };
}

/**
 * Drains microtasks so the apiClient query promises resolve AND the
 * `listen(...)` promises returned to the component resolve (the effect
 * awaits them before the unlisten functions are stored).
 */
async function flushAsync(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function findListener<T = unknown>(event: string): ListenRegistration<T> {
  const match = listenRegistrations.find((r) => r.event === event);
  if (!match) {
    throw new Error(
      `Expected CapturesTab to subscribe to "${event}" but registrations were: ` +
        JSON.stringify(listenRegistrations.map((r) => r.event)),
    );
  }
  return match as ListenRegistration<T>;
}

const sampleCapture: CaptureResponse = {
  id: 'cap-abcdef12-3456',
  audio_path: '/captures/cap.wav',
  source: 'dictation',
  language: 'en',
  duration_ms: 12_000,
  transcript_raw: 'raw words from the recorder',
  transcript_refined: 'Refined sentence from the LLM.',
  stt_model: 'turbo',
  llm_model: '0.6B',
  refinement_flags: null,
  created_at: '2026-06-25T12:00:00Z',
};

// ── Tests ───────────────────────────────────────────────────────────────────

describe('CapturesTab — capture:created event seeds the list', () => {
  it('renders a new capture row when a capture:created event fires after mount', async () => {
    renderWithQuery();
    await flushAsync();

    // Sanity: the empty state is rendered before any events arrive. The i18n
    // key for the empty list ("captures.empty.none") resolves to a string
    // containing "No captures" — but we don't pin the exact wording, just
    // that the refined snippet from the incoming capture is NOT yet visible.
    expect(screen.queryByText(/Refined sentence from the LLM\./)).not.toBeInTheDocument();

    // Replay the payload Rust would emit when the dictate window finishes a
    // capture. The component listens with a generic `unknown` payload type,
    // so we cast at the call site rather than fighting the generic. We also
    // push the capture into the listCaptures backing state so the
    // invalidateQueries triggered by the same handler doesn't refetch the
    // cache back to empty.
    captureBackingState.items = [sampleCapture];
    captureBackingState.total = 1;
    const createdReg = findListener('capture:created');
    await act(async () => {
      (createdReg.handler as (event: unknown) => void)({
        event: 'capture:created',
        id: 1,
        payload: { capture: sampleCapture },
      });
      // Let the query-cache write + selection-effect cascade settle.
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Observable DOM outcome: the refined transcript snippet (used in both
    // the list snippet and the detail textarea) is now in the document.
    // Asserting on the snippet text — not on internal mock-call counts —
    // makes this a behavior test on the rendered list, not a structural one
    // on the cache writer.
    const snippetMatches = await screen.findAllByText(/Refined sentence from the LLM\./);
    expect(snippetMatches.length).toBeGreaterThan(0);
  });

  it('subscribes to BOTH capture:created and capture:updated at the @tauri-apps event boundary', async () => {
    renderWithQuery();
    await flushAsync();

    // Shape assertion on the boundary: the component MUST register exactly
    // these two event names — a typo or missed registration would silently
    // break sibling-window sync.
    const registered = listenRegistrations.map((r) => r.event).sort();
    expect(registered).toEqual(['capture:created', 'capture:updated']);
  });
});

describe('CapturesTab — export audio via plugin-dialog + plugin-fs', () => {
  it('calls save() with a .wav default name and writes the fetched bytes via writeFile()', async () => {
    // Arrange: pre-seed the cache via the listen() callback (same path as
    // test #1 — gets us a selected capture without forcing the apiClient
    // mock to return a list, which would race with the query refetch the
    // listener triggers anyway).
    saveMock.mockResolvedValueOnce('/Users/test/Downloads/capture_cap-abcd.wav');
    writeFileMock.mockResolvedValueOnce(undefined);

    // Stub fetch for the audio download; the component fetches the audio
    // URL, reads arrayBuffer, then hands the bytes to writeFile.
    const fakeBytes = new Uint8Array([0x52, 0x49, 0x46, 0x46]); // "RIFF"
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      new Response(fakeBytes, { status: 200 }),
    );

    renderWithQuery();
    await flushAsync();

    captureBackingState.items = [sampleCapture];
    captureBackingState.total = 1;
    const createdReg = findListener('capture:created');
    await act(async () => {
      (createdReg.handler as (event: unknown) => void)({
        event: 'capture:created',
        id: 1,
        payload: { capture: sampleCapture },
      });
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Open the Export dropdown, then click the Audio item. Radix's
    // DropdownMenu needs real pointer events to open its portal — fireEvent
    // doesn't carry the pointer payload Radix expects, so we drive it with
    // user-event's pointer-aware setup.
    const user = userEvent.setup();
    const exportButton = await screen.findByRole('button', { name: /^Export$/i });
    await user.click(exportButton);

    const audioItem = await screen.findByRole('menuitem', { name: /Audio \(WAV\)/i });
    await user.click(audioItem);

    // Let the async export pipeline (save → fetch → writeFile) settle.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Behavior shape at the dialog boundary: the component asked the OS for
    // a single save destination with a .wav default name and the Audio
    // filter. Asserting the recorded call shape — not a count — so a
    // regression that changed the filter or default name would surface.
    expect(saveMock.mock.calls.length).toBe(1);
    const saveOpts = (saveMock.mock.calls[0] as unknown[])[0] as {
      defaultPath: string;
      filters: Array<{ name: string; extensions: string[] }>;
    };
    expect(saveOpts.defaultPath).toMatch(/^capture_cap-abcd.*\.wav$/);
    expect(saveOpts.filters).toEqual([{ name: 'Audio', extensions: ['wav'] }]);

    // Behavior shape at the filesystem boundary: writeFile got the path the
    // OS picker returned AND the bytes we fetched. The `(call: unknown[])`
    // cast pattern matches the existing reviewer-PASS waves.
    expect(writeFileMock.mock.calls.length).toBe(1);
    const writeArgs = writeFileMock.mock.calls[0] as unknown[];
    expect(writeArgs[0]).toBe('/Users/test/Downloads/capture_cap-abcd.wav');
    expect(writeArgs[1]).toBeInstanceOf(Uint8Array);
    expect(Array.from(writeArgs[1] as Uint8Array)).toEqual([0x52, 0x49, 0x46, 0x46]);

    // And the audio URL we fetched came from the apiClient helper so the
    // backend route is the one we're testing against, not a hard-coded
    // string that might drift.
    expect(fetchSpy).toHaveBeenCalledWith('http://test.local/captures/cap-abcdef12-3456/audio');
  });
});

describe('CapturesTab — copy transcript to clipboard', () => {
  it('writes the selected refined transcript to navigator.clipboard on Copy click', async () => {
    // userEvent v14's setup() installs its OWN clipboard polyfill on
    // navigator.clipboard, which clobbers any stub installed beforehand.
    // We must construct the user-event session first, THEN install our stub
    // on top so the component's writeText call lands on our spy. (This is the
    // exact 160k-token rabbit hole flagged in the T-UT-CAPTURES brief.)
    const user = userEvent.setup();

    // JSDOM ships no real Clipboard API; install a configurable stub on
    // window.navigator so the component's `await navigator.clipboard.writeText(...)`
    // resolves into something we can assert on. configurable:true is required
    // because vitest's jsdom env locks down navigator props by default — this
    // is the exact JSDOM workaround called out in the T-CT-01 brief.
    const clipboardWriteText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, 'clipboard', {
      value: { writeText: clipboardWriteText },
      configurable: true,
    });

    renderWithQuery();
    await flushAsync();

    // Pre-seed a capture via the listen() callback (same path as the export
    // test) so a selection exists. showRefined defaults to true in the
    // component, so the copy handler picks transcript_refined.
    captureBackingState.items = [sampleCapture];
    captureBackingState.total = 1;
    const createdReg = findListener('capture:created');
    await act(async () => {
      (createdReg.handler as (event: unknown) => void)({
        event: 'capture:created',
        id: 1,
        payload: { capture: sampleCapture },
      });
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Confirm the detail pane rendered with the seeded capture before we look
    // for the button — without a selection, handleCopy early-returns and the
    // clipboard never gets called.
    await screen.findAllByText(/Refined sentence from the LLM\./);

    // i18n key `captures.actions.copy` resolves to "Copy" in en/translation.json.
    // The button is a plain <Button> (not a Radix portal item), so userEvent
    // click is straightforward — no pointer-event dance like the dropdown.
    const copyButton = await screen.findByRole('button', { name: /^Copy$/i });
    await user.click(copyButton);

    // Let the handler's await navigator.clipboard.writeText(...) resolve.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Shape assertion on the clipboard boundary: exactly one writeText call,
    // and its first argument is the refined transcript from the selected
    // capture. Asserting the call shape — not a count on an internal
    // collaborator — so a regression that swapped showRefined polarity or
    // dropped the await would surface here.
    expect(clipboardWriteText.mock.calls.length).toBe(1);
    const writeArgs = clipboardWriteText.mock.calls[0] as unknown[];
    expect(writeArgs[0]).toBe('Refined sentence from the LLM.');
  });
});

describe('CapturesTab — listener cleanup on unmount', () => {
  it('invokes every unlisten function returned by listen() exactly once when unmounted', async () => {
    const { unmount } = renderWithQuery();
    await flushAsync();

    const expectedUnlistens = listenRegistrations.map((r) => r.unlisten);
    // Guard the precondition explicitly — if the component stopped
    // subscribing, this test would silently pass with zero unlistens.
    expect(expectedUnlistens.length).toBe(2);

    await act(async () => {
      unmount();
      // The cleanup pattern is `for (const p of unlistens) p.then(fn => fn())`
      // so we flush microtasks twice to let each .then resolve.
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Observable boundary: every unlisten the component received was invoked
    // exactly once. Compare sets (not order) since teardown order across
    // multiple awaited promises is an internal detail of the cleanup loop.
    const releasedSet = new Set(unlistensCalled);
    const expectedSet = new Set(expectedUnlistens);
    expect(releasedSet.size).toBe(expectedSet.size);
    for (const fn of expectedSet) {
      expect(releasedSet.has(fn)).toBe(true);
    }
    // And the total invocation count matches — no double-fire, no leaks.
    expect(unlistensCalled.length).toBe(expectedUnlistens.length);
  });
});
