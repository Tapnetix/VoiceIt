/// <reference types="@testing-library/jest-dom/vitest" />
/**
 * InputMonitoringGate unit tests (T-UT-IM).
 *
 * Same shape as the macOS-API gap covered by S19 (AccessibilityGate): the
 * production component wraps the Tauri command surface for the macOS
 * Input-Monitoring permission. We mock the `@tauri-apps/api/core` boundary
 * (the runtime/OS edge) and drive the real component + real PlatformProvider
 * + real i18n through it.
 *
 * Assertions follow the LQ-improved pattern used elsewhere in this codebase:
 *  - We never use `toHaveBeenCalled*`.
 *  - We inspect `mock.calls` *shape* via the `(call: unknown[]) =>` cast so
 *    a stray extra argument or extra invocation would surface as a diff.
 *  - We pair every boundary assertion with a DOM-observable outcome.
 */
import '@/i18n';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InputMonitoringNotice } from '@/components/InputMonitoringGate/InputMonitoringGate';
import { PlatformProvider } from '@/platform/PlatformContext';
import type { Platform } from '@/platform/types';

// ── @tauri-apps/api/core mock (OS/runtime boundary — allowed) ────────────────
//
// `invoke` is the single edge the component crosses to talk to the macOS
// permission APIs. We route every command name through a typed dispatcher so
// each test can configure the trusted/untrusted state for
// `check_input_monitoring_permission` and observe what `openSettings` sent
// over the boundary.
const invokeMock = vi.fn();
vi.mock('@tauri-apps/api/core', () => ({
  invoke: (cmd: string, args?: Record<string, unknown>) => invokeMock(cmd, args),
}));

// ─────────────────────────────────────────────────────────────────────────────

/**
 * Build a real Platform value for PlatformProvider. We toggle `isTauri` per
 * test rather than mocking `@/platform/PlatformContext` (a first-party module).
 *
 * The non-metadata members are unused by `InputMonitoringNotice` itself but
 * the type contract demands them; we provide no-op fakes so the provider's
 * runtime invariants hold.
 */
function makePlatform(isTauri: boolean): Platform {
  const noop = async () => undefined;
  return {
    filesystem: {
      saveFile: noop,
      openPath: noop,
      pickDirectory: async () => null,
    },
    updater: {
      checkForUpdates: noop,
      downloadAndInstall: noop,
      restartAndInstall: noop,
      getStatus: () => ({
        checking: false,
        available: false,
        downloading: false,
        installing: false,
        readyToInstall: false,
      }),
      subscribe: () => () => undefined,
    },
    audio: {
      isSystemAudioSupported: async () => false,
      startSystemAudioCapture: noop,
      stopSystemAudioCapture: async () => new Blob(),
      listOutputDevices: async () => [],
      playToDevices: noop,
      stopPlayback: () => undefined,
    },
    lifecycle: {
      startServer: async () => '',
      stopServer: noop,
      restartServer: async () => '',
      setKeepServerRunning: noop,
      setupWindowCloseHandler: noop,
      subscribeToServerLogs: () => () => undefined,
    },
    metadata: {
      getVersion: async () => '0.0.0-test',
      isTauri,
    },
  };
}

interface RenderOptions {
  enabled?: boolean;
  isTauri?: boolean;
}

function renderNotice({ enabled = true, isTauri = true }: RenderOptions = {}) {
  return render(
    <PlatformProvider platform={makePlatform(isTauri)}>
      <InputMonitoringNotice enabled={enabled} />
    </PlatformProvider>,
  );
}

/** English strings the real i18n catalogue produces for this gate. */
const GATE_TITLE = /Grant Input Monitoring to enable the global shortcut/i;
const STILL_MISSING = /Still not detected\. macOS usually requires quitting/i;
const RECHECKING = /^Checking…$/;

beforeEach(() => {
  invokeMock.mockReset();
  // Default to the "granted" outcome so tests that don't care about the
  // permission API still get a well-defined return value.
  invokeMock.mockResolvedValue(true);
});

afterEach(() => {
  // Drop any focus listeners the hook attached so a later test's focus
  // dispatch doesn't fire into a stale render's recheck closure.
  window.dispatchEvent(new Event('blur'));
});

// ─────────────────────────────────────────────────────────────────────────────
// (1) Ungranted → gate UI is rendered
// ─────────────────────────────────────────────────────────────────────────────
describe('InputMonitoringNotice — permission missing', () => {
  it('renders the gate title, body, and action buttons when the permission check returns false', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_input_monitoring_permission') return false;
      return undefined;
    });

    renderNotice({ enabled: true });

    // Observable: the title surfaces in the DOM once the async check resolves.
    expect(await screen.findByText(GATE_TITLE)).toBeInTheDocument();

    // Body copy carries an inline <path> Trans component — assert the
    // non-Trans portion the user actually reads.
    expect(
      screen.getByText(/VoiceIt needs/i, { exact: false }),
    ).toBeInTheDocument();

    // Both action buttons are present and not in their loading state.
    expect(
      screen.getByRole('button', { name: /^Open Settings$/i }),
    ).toBeEnabled();
    const recheckBtn = screen.getByRole('button', { name: /I've enabled it/i });
    expect(recheckBtn).toBeEnabled();
    expect(recheckBtn).not.toHaveTextContent(RECHECKING);

    // Boundary shape: exactly one permission-check call was issued at mount,
    // and it carried no arguments. This subsumes "the API was called" while
    // also catching a regression that would forward stray params.
    const checkCalls = (invokeMock.mock.calls as unknown[][])
      .filter((call: unknown[]) => call[0] === 'check_input_monitoring_permission')
      .map((call: unknown[]) => call);
    expect(checkCalls).toEqual([['check_input_monitoring_permission', undefined]]);
  });

  it('invokes open_input_monitoring_settings when the user clicks "Open Settings"', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_input_monitoring_permission') return false;
      return undefined;
    });

    const user = userEvent.setup();
    renderNotice({ enabled: true });

    const openBtn = await screen.findByRole('button', {
      name: /^Open Settings$/i,
    });
    await user.click(openBtn);

    // Boundary shape: exactly one open-settings call, no args.
    const openCalls = (invokeMock.mock.calls as unknown[][])
      .filter((call: unknown[]) => call[0] === 'open_input_monitoring_settings')
      .map((call: unknown[]) => call);
    expect(openCalls).toEqual([['open_input_monitoring_settings', undefined]]);

    // Observable DOM outcome: the gate is still rendered (clicking Open
    // Settings does not optimistically dismiss the notice — that only
    // happens after a successful recheck).
    expect(screen.getByText(GATE_TITLE)).toBeInTheDocument();
  });

  it('surfaces the "stillMissing" warning after a recheck that still reports untrusted', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_input_monitoring_permission') return false;
      return undefined;
    });

    const user = userEvent.setup();
    renderNotice({ enabled: true });

    // Wait for the initial mount-check to land the gate.
    await screen.findByText(GATE_TITLE);

    // The warning is only created post-recheck.
    expect(screen.queryByText(STILL_MISSING)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /I've enabled it/i }));

    expect(await screen.findByText(STILL_MISSING)).toBeInTheDocument();

    // Boundary shape: two permission checks total (mount + manual recheck),
    // both no-arg.
    const checkCalls = (invokeMock.mock.calls as unknown[][])
      .filter((call: unknown[]) => call[0] === 'check_input_monitoring_permission')
      .map((call: unknown[]) => call);
    expect(checkCalls).toEqual([
      ['check_input_monitoring_permission', undefined],
      ['check_input_monitoring_permission', undefined],
    ]);
  });

  it('dismisses the gate when a recheck flips the permission to granted', async () => {
    // First call (mount): untrusted → gate shows. Second call (recheck):
    // trusted → gate must unmount entirely.
    invokeMock
      .mockImplementationOnce(async () => false)
      .mockImplementationOnce(async () => true);

    const user = userEvent.setup();
    renderNotice({ enabled: true });

    await screen.findByText(GATE_TITLE);
    await user.click(screen.getByRole('button', { name: /I've enabled it/i }));

    await waitFor(() => {
      expect(screen.queryByText(GATE_TITLE)).not.toBeInTheDocument();
    });
    // And the stillMissing warning is NOT shown — the success branch must
    // suppress it even on the same render tick that hides the gate.
    expect(screen.queryByText(STILL_MISSING)).not.toBeInTheDocument();
  });

  it('re-runs the permission check when the window receives focus', async () => {
    invokeMock.mockResolvedValue(false);

    renderNotice({ enabled: true });

    // Wait for the mount check to resolve.
    await screen.findByText(GATE_TITLE);

    // One mount-time check so far.
    const before = (invokeMock.mock.calls as unknown[][])
      .filter((call: unknown[]) => call[0] === 'check_input_monitoring_permission')
      .map((call: unknown[]) => call);
    expect(before).toEqual([['check_input_monitoring_permission', undefined]]);

    await act(async () => {
      window.dispatchEvent(new Event('focus'));
    });

    await waitFor(() => {
      const after = (invokeMock.mock.calls as unknown[][])
        .filter((call: unknown[]) => call[0] === 'check_input_monitoring_permission')
        .map((call: unknown[]) => call);
      expect(after).toEqual([
        ['check_input_monitoring_permission', undefined],
        ['check_input_monitoring_permission', undefined],
      ]);
    });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// (2) Granted → gate is hidden
// ─────────────────────────────────────────────────────────────────────────────
describe('InputMonitoringNotice — permission granted or gate suppressed', () => {
  it('renders nothing when the permission check returns true', async () => {
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_input_monitoring_permission') return true;
      return undefined;
    });

    const { container } = renderNotice({ enabled: true });

    // Wait until the check has resolved at least once so we're observing the
    // post-check state, not the initial pre-check render.
    await waitFor(() => {
      const calls = (invokeMock.mock.calls as unknown[][])
        .filter((call: unknown[]) => call[0] === 'check_input_monitoring_permission')
        .map((call: unknown[]) => call);
      expect(calls).toEqual([['check_input_monitoring_permission', undefined]]);
    });

    // DOM-observable: no gate title, no action buttons, the whole subtree
    // collapses to null.
    expect(screen.queryByText(GATE_TITLE)).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /^Open Settings$/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /I've enabled it/i }),
    ).not.toBeInTheDocument();
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when enabled=false even if the permission would be missing', async () => {
    // Even if the OS would report untrusted, the consumer-side "global
    // shortcut" toggle being off means the notice must stay silent.
    invokeMock.mockImplementation(async (cmd: string) => {
      if (cmd === 'check_input_monitoring_permission') return false;
      return undefined;
    });

    const { container } = renderNotice({ enabled: false });

    // Give any async check a chance to resolve so we know we're past the
    // post-check render, not before it.
    await waitFor(() => {
      const calls = (invokeMock.mock.calls as unknown[][])
        .filter((call: unknown[]) => call[0] === 'check_input_monitoring_permission')
        .map((call: unknown[]) => call);
      expect(calls).toEqual([['check_input_monitoring_permission', undefined]]);
    });

    expect(container.firstChild).toBeNull();
    expect(screen.queryByText(GATE_TITLE)).not.toBeInTheDocument();
  });

  it('skips the Tauri permission check entirely on a non-Tauri platform and hides the gate', async () => {
    // Outside Tauri, the hook short-circuits to "trusted" without touching
    // the invoke boundary at all — there is no macOS API to talk to.
    invokeMock.mockImplementation(async () => {
      throw new Error('invoke should not be called outside Tauri');
    });

    const { container } = renderNotice({ enabled: true, isTauri: false });

    // Confirm no invoke calls were issued — this is the load-bearing
    // behaviour for the web/dev build.
    expect(invokeMock.mock.calls as unknown[][]).toEqual([]);

    expect(container.firstChild).toBeNull();
    expect(screen.queryByText(GATE_TITLE)).not.toBeInTheDocument();
  });
});
