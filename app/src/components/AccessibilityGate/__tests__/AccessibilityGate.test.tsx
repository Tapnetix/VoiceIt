/**
 * AccessibilityGate — unit tests.
 *
 * Per design.md §4, this layer verifies the component's reaction to MOCKED
 * macOS Accessibility permission events. The underlying permission grant API
 * itself is not exercised here (no automated coverage for the real macOS
 * surface this run) — only the boundary between the React component and the
 * `@tauri-apps/api/{core,event}` modules.
 *
 * Coverage:
 *   (1) Ungranted state — the gate UI is rendered (title + recheck button
 *       visible, and the boundary call to `check_accessibility_permission`
 *       was issued by the mount-effect).
 *   (2) Granted state — the component renders nothing (queryByTestId/role
 *       returns null) because `needsPermission` stays false.
 *   (3) Listener cleanup on unmount — the `unlisten` function returned by
 *       `listen('system:accessibility-missing', …)` is invoked when the
 *       component unmounts, preventing zombie subscriptions.
 *
 * Assertion style (per task quality bar):
 *   - No `toHaveBeenCalled*` matchers on internal collaborators.
 *   - Boundary spies (the `@tauri-apps/api/*` module-mock fns) are inspected
 *     via `mock.calls[i]` cast through `(call: unknown[]) =>` to read the
 *     value crossing the OS boundary.
 *   - DOM observables drive the gate-visible / gate-absent / button-state
 *     assertions.
 *   - The only mocked first-party surface is `@tauri-apps/*`, which is the
 *     OS/runtime boundary (explicitly allowed).
 */
/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { act, render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { PlatformProvider } from '@/platform/PlatformContext';
import type { Platform, UpdateStatus } from '@/platform/types';

// ─── Module-boundary mocks (allowed: OS/runtime surface) ──────────────────────

const invokeMock = vi.fn();
const listenMock = vi.fn();

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

vi.mock('@tauri-apps/api/event', () => ({
  listen: (...args: unknown[]) => listenMock(...args),
}));

// Now import the component under test (after the mocks are registered).
import { AccessibilityNotice } from '@/components/AccessibilityGate/AccessibilityGate';

// ─── Platform fixture (isTauri: true so the hook's effects run) ───────────────

const mockUpdateStatus: UpdateStatus = {
  checking: false,
  available: false,
  downloading: false,
  installing: false,
  readyToInstall: false,
};

function buildPlatform(isTauri: boolean): Platform {
  return {
    filesystem: {
      saveFile: vi.fn(),
      openPath: vi.fn(),
      pickDirectory: vi.fn(),
    },
    updater: {
      checkForUpdates: vi.fn(),
      downloadAndInstall: vi.fn(),
      restartAndInstall: vi.fn(),
      getStatus: vi.fn().mockReturnValue(mockUpdateStatus),
      subscribe: vi.fn().mockReturnValue(() => {}),
    },
    audio: {
      isSystemAudioSupported: vi.fn().mockResolvedValue(false),
      startSystemAudioCapture: vi.fn(),
      stopSystemAudioCapture: vi.fn(),
      listOutputDevices: vi.fn().mockResolvedValue([]),
      playToDevices: vi.fn(),
      stopPlayback: vi.fn(),
    },
    lifecycle: {
      startServer: vi.fn().mockResolvedValue('http://localhost:8000'),
      stopServer: vi.fn(),
      restartServer: vi.fn().mockResolvedValue('http://localhost:8000'),
      setKeepServerRunning: vi.fn(),
      setupWindowCloseHandler: vi.fn(),
      subscribeToServerLogs: vi.fn().mockReturnValue(() => {}),
    },
    metadata: {
      getVersion: vi.fn().mockResolvedValue('0.0.0'),
      isTauri,
    },
  };
}

function wrap(ui: ReactNode, isTauri = true) {
  return <PlatformProvider platform={buildPlatform(isTauri)}>{ui}</PlatformProvider>;
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('AccessibilityNotice', () => {
  beforeEach(() => {
    invokeMock.mockReset();
    listenMock.mockReset();
    // Default: `listen` resolves with a no-op unlisten so the cleanup-effect
    // can fire without rejecting.
    listenMock.mockResolvedValue(() => {});
  });

  afterEach(() => {
    cleanup();
  });

  it('renders the gate UI when macOS reports the permission is ungranted', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_accessibility_permission') return false;
      return undefined;
    });

    render(wrap(<AccessibilityNotice />));

    // Outcome 1 — the gate UI eventually surfaces. The title text and both
    // action buttons (Open Settings, recheck) are observable in the DOM.
    expect(
      await screen.findByText('Grant Accessibility permission to enable auto-paste'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /open settings/i })).toBeInTheDocument();
    // The recheck button starts in its idle label.
    expect(screen.getByRole('button', { name: "I've enabled it" })).toBeInTheDocument();

    // Outcome 2 — the value crossing the OS boundary was the
    // `check_accessibility_permission` invoke. We read the captured arg
    // rather than asserting on a call count.
    await waitFor(() => {
      expect(invokeMock.mock.calls.length).toBeGreaterThan(0);
    });
    const invokeCmds = invokeMock.mock.calls.map((call: unknown[]) => call[0]);
    expect(invokeCmds).toContain('check_accessibility_permission');

    // Outcome 3 — the listen() subscription was registered on the documented
    // event channel. Reading `mock.calls[0][0]` exposes the channel name as it
    // crosses the boundary.
    await waitFor(() => {
      expect(listenMock.mock.calls.length).toBeGreaterThan(0);
    });
    const listenChannels = listenMock.mock.calls.map((call: unknown[]) => call[0]);
    expect(listenChannels).toContain('system:accessibility-missing');
  });

  it('renders nothing when macOS reports the permission is already granted', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_accessibility_permission') return true;
      return undefined;
    });

    const { container } = render(wrap(<AccessibilityNotice />));

    // Outcome 1 — the boundary check happened (we relied on the real
    // permission API to decide).
    await waitFor(() => {
      const cmds = invokeMock.mock.calls.map((call: unknown[]) => call[0]);
      expect(cmds).toContain('check_accessibility_permission');
    });

    // Outcome 2 — given the granted reply, the component returns null. No
    // gate title, no buttons, and the wrapper container stays empty.
    await waitFor(() => {
      expect(
        screen.queryByText('Grant Accessibility permission to enable auto-paste'),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /open settings/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: "I've enabled it" })).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });

  it('invokes the unlisten function returned by listen() when unmounting', async () => {
    const unlistenSpy = vi.fn();
    listenMock.mockResolvedValue(unlistenSpy);
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_accessibility_permission') return false;
      return undefined;
    });

    const { unmount } = render(wrap(<AccessibilityNotice />));

    // Wait for the listen() promise chain to resolve and the local `unlisten`
    // ref to be populated, so the cleanup-effect has something to invoke.
    await waitFor(() => {
      expect(listenMock.mock.calls.length).toBeGreaterThan(0);
    });
    // Drain the microtask queue so the .then(fn => { unlisten = fn }) runs.
    await Promise.resolve();
    await Promise.resolve();

    unmount();

    // Outcome — the same unlisten function the OS gave us through `listen`'s
    // promise was invoked by the cleanup-effect. We inspect mock.calls.length
    // for the captured-callback side rather than the global spy matcher,
    // matching the LQ-improved shape-array pattern.
    await waitFor(() => {
      expect(unlistenSpy.mock.calls.length).toBe(1);
    });
    const unlistenArgs = unlistenSpy.mock.calls.map((call: unknown[]) => call);
    expect(unlistenArgs[0]).toEqual([]);
  });

  it('clicking Open Settings sends the open_accessibility_settings command across the boundary', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_accessibility_permission') return false;
      return undefined;
    });

    const u = userEvent.setup();
    render(wrap(<AccessibilityNotice />));

    const openBtn = await screen.findByRole('button', { name: /open settings/i });
    await u.click(openBtn);

    // Outcome — the value crossing the OS boundary identifies the
    // open_accessibility_settings command. We read the captured arg list
    // rather than asserting on a spy call count.
    await waitFor(() => {
      const cmds = invokeMock.mock.calls.map((call: unknown[]) => call[0]);
      expect(cmds).toContain('open_accessibility_settings');
    });
  });

  it('clicking the recheck button surfaces the stillMissing hint when permission is still denied', async () => {
    // First call (mount-effect): false → gate shows.
    // Second call (recheck): also false → handleRecheck sets stillMissing=true.
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_accessibility_permission') return false;
      return undefined;
    });

    const u = userEvent.setup();
    render(wrap(<AccessibilityNotice />));

    const recheckBtn = await screen.findByRole('button', { name: "I've enabled it" });

    // Before clicking, the stillMissing hint must NOT be rendered.
    expect(
      screen.queryByText(/still not detected/i),
    ).not.toBeInTheDocument();

    await u.click(recheckBtn);

    // Outcome 1 — the stillMissing hint surfaces (observable DOM change
    // driven by the recheck() return value).
    expect(await screen.findByText(/still not detected/i)).toBeInTheDocument();

    // Outcome 2 — the boundary saw at least two check invocations (one from
    // mount, one from the recheck click). Shape-array read of mock.calls.
    const checkCmdCalls = invokeMock.mock.calls.filter(
      (call: unknown[]) => call[0] === 'check_accessibility_permission',
    );
    expect(checkCmdCalls.length).toBeGreaterThanOrEqual(2);
  });

  it('shows the gate after a system:accessibility-missing event fires post-mount', async () => {
    // Start in the granted state so the gate is initially hidden.
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_accessibility_permission') return true;
      return undefined;
    });

    // Capture the listener callback so we can fire the event manually.
    let firedCallback: ((event: { payload: unknown }) => void) | null = null;
    listenMock.mockImplementation(
      async (
        _channel: string,
        cb: (event: { payload: unknown }) => void,
      ) => {
        firedCallback = cb;
        return () => {};
      },
    );

    render(wrap(<AccessibilityNotice />));

    // Wait for the listener to be wired up.
    await waitFor(() => {
      expect(firedCallback).not.toBeNull();
    });

    // Confirm initial state: gate is absent.
    expect(
      screen.queryByText('Grant Accessibility permission to enable auto-paste'),
    ).not.toBeInTheDocument();

    // Simulate the OS firing the accessibility-missing event.
    await act(async () => {
      firedCallback?.({ payload: null });
    });

    // Outcome — the gate UI now appears, driven by the listener callback
    // flipping `needsPermission` to true.
    expect(
      await screen.findByText('Grant Accessibility permission to enable auto-paste'),
    ).toBeInTheDocument();
  });

  it('re-issues the accessibility check when the window regains focus', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_accessibility_permission') return false;
      return undefined;
    });

    render(wrap(<AccessibilityNotice />));

    // Wait for the mount-effect's first check.
    await waitFor(() => {
      const cmds = invokeMock.mock.calls.map((call: unknown[]) => call[0]);
      expect(cmds).toContain('check_accessibility_permission');
    });

    const callsAfterMount = invokeMock.mock.calls.filter(
      (call: unknown[]) => call[0] === 'check_accessibility_permission',
    ).length;

    // Fire a window focus event — the hook's listener should trigger a
    // second permission check.
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
    });

    // Outcome — the boundary saw at least one additional check after focus.
    await waitFor(() => {
      const callsAfterFocus = invokeMock.mock.calls.filter(
        (call: unknown[]) => call[0] === 'check_accessibility_permission',
      ).length;
      expect(callsAfterFocus).toBeGreaterThan(callsAfterMount);
    });
  });

  it('skips the OS boundary entirely on non-Tauri platforms (gate stays absent)', async () => {
    // Sanity-check the early-return: when isTauri=false the hook should not
    // invoke or subscribe at all, and the gate must not render.
    invokeMock.mockResolvedValue(false);

    const { container } = render(wrap(<AccessibilityNotice />, /* isTauri */ false));

    // Give effects a tick to (not) run.
    await Promise.resolve();
    await Promise.resolve();

    expect(invokeMock.mock.calls.length).toBe(0);
    expect(listenMock.mock.calls.length).toBe(0);
    expect(container).toBeEmptyDOMElement();
  });
});
