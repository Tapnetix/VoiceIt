import '@/i18n';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

// --- mocks ---
const fakeBuffer = (durationSec: number) => ({
  numberOfChannels: 1,
  sampleRate: 24000,
  length: durationSec * 24000,
  duration: durationSec,
  getChannelData: () => new Float32Array(durationSec * 24000),
});

vi.mock('@/lib/utils/audio', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/utils/audio')>();
  return {
    ...actual,
    decodeAudioFile: vi.fn(),
    sliceToWav: vi.fn(() => new Blob(['x'], { type: 'audio/wav' })),
    audioBufferToWav: vi.fn(() => new Blob(['x'], { type: 'audio/wav' })),
    suggestWindow: vi.fn((_buf, len) => ({ start: 42, end: 42 + Math.min(len, 45) })),
  };
});

// Shared container so the vi.mock factory (which is hoisted) can write the instance
// and tests (which run later) can read it. Using an object avoids the TDZ issue.
//
// The mocked WaveSurfer behaves like a tiny audio engine: setTime advances a
// tracked playhead, play/pause emit the matching events so the component's
// `isPlaying` state reflects engine state. Tests can then assert on *observable
// outcomes* — the playhead value, the rendered Play/Pause icon, and the
// selection text — rather than spying on individual method calls.
const mockWs = {
  instance: null as null | {
    play: ReturnType<typeof vi.fn>;
    setTime: ReturnType<typeof vi.fn>;
    pause: ReturnType<typeof vi.fn>;
    seekTo: ReturnType<typeof vi.fn>;
    currentTime: number;
    isPlaying: () => boolean;
    [key: string]: any;
  },
  // Invoke recorded wavesurfer event handlers (e.g. 'interaction') from tests.
  fire: undefined as undefined | ((event: string, ...args: any[]) => void),
};

// Minimal wavesurfer + regions mock — record handlers so tests can fire region updates.
vi.mock('wavesurfer.js', () => {
  let handlers: Record<string, ((...a: any[]) => void)[]> = {};
  const fire = (event: string, ...args: any[]) => {
    for (const cb of handlers[event] ?? []) cb(...args);
  };
  const ws: any = {
    registerPlugin: (p: any) => p,
    on: (e: string, cb: any) => {
      handlers[e] = handlers[e] ?? [];
      handlers[e].push(cb);
    },
    currentTime: 0,
    _playing: false,
    play: vi.fn(function (this: any) {
      ws._playing = true;
      fire('play');
    }),
    pause: vi.fn(function (this: any) {
      ws._playing = false;
      fire('pause');
    }),
    setTime: vi.fn(function (this: any, t: number) {
      ws.currentTime = t;
    }),
    getCurrentTime: () => ws.currentTime,
    getDuration: () => 192,
    destroy: vi.fn(() => {
      // Clear handlers on destroy so handlers from a previous mount don't fire
      // through the shared engine in a later test.
      handlers = {};
    }),
    seekTo: vi.fn(),
    load: vi.fn().mockResolvedValue(undefined),
    isPlaying: () => ws._playing,
  };
  return {
    default: {
      create: vi.fn(() => {
        ws.currentTime = 0;
        ws._playing = false;
        handlers = {};
        mockWs.instance = ws;
        mockWs.fire = fire;
        return ws;
      }),
    },
  };
});
vi.mock('wavesurfer.js/dist/plugins/regions.js', () => {
  const region = { start: 42, end: 62, setOptions: vi.fn(), on: vi.fn(), remove: vi.fn() };
  const plugin = { on: vi.fn(), addRegion: vi.fn(() => region), getRegions: () => [region], clearRegions: vi.fn() };
  return { default: { create: vi.fn(() => plugin) } };
});

import { decodeAudioFile } from '@/lib/utils/audio';
import { createRef } from 'react';
import { AudioTrimmer, placeWindow, type AudioTrimmerHandle } from '../AudioTrimmer';

describe('placeWindow (pure selection geometry)', () => {
  it('anchors a window at the given start, clamped to the clip end', () => {
    expect(placeWindow(0, 20, 192)).toEqual({ start: 0, end: 20 });
    expect(placeWindow(134.4, 20, 192)).toEqual({ start: 134.4, end: 154.4 });
    // Past the end → shifts back so the window still fits.
    expect(placeWindow(190, 20, 192)).toEqual({ start: 172, end: 192 });
    // Length clamped to [15,45].
    expect(placeWindow(0, 5, 192)).toEqual({ start: 0, end: 15 });
    expect(placeWindow(0, 60, 192)).toEqual({ start: 0, end: 45 });
  });
});

const makeFile = (name = 'interview.wav') => new File(['data'], name, { type: 'audio/wav' });

beforeEach(() => vi.clearAllMocks());

describe('AudioTrimmer', () => {
  it('S1: long source auto-expands with a ~20s window and "ideal" chip', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192)); // 3:12
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const root = await screen.findByTestId('audio-trimmer');
    expect(root).toHaveAttribute('data-state', 'expanded');
    expect(screen.getByTestId('trimmer-length-chip')).toHaveTextContent(/20s.*ideal/i);
    expect(screen.getByTestId('trimmer-region')).toBeInTheDocument();
  });

  it('S2: changing the length control updates selection + chip, clamped to 15-45', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const slider = await screen.findByTestId('trimmer-length');
    fireEvent.change(slider, { target: { value: '25' } });
    expect(screen.getByTestId('trimmer-length-chip')).toHaveTextContent(/25s/);
    expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/selection/i);
  });

  it('S3: a >30s window shows the warning and keeps confirm enabled', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const slider = await screen.findByTestId('trimmer-length');
    fireEvent.change(slider, { target: { value: '38' } });
    expect(screen.getByTestId('trimmer-warning')).toBeInTheDocument();
    expect(screen.getByTestId('trimmer-length-chip')).toHaveTextContent(/longer than recommended/i);
    // Confirm button still enabled (find by role/name "Use this clip").
    expect(screen.getByRole('button', { name: /use this clip/i })).not.toBeDisabled();
  });

  it('S4: play scopes audition to the region (defaults to start; auto-suggest jumps to the energy window)', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const playBtn = await screen.findByTestId('trimmer-play');
    // Long source anchors the window at the START of the clip → selection reads 0:00–0:20.
    expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/0:00\s*–\s*0:20/);
    // Outcome: pressing Play moves the engine playhead to the region start (0s)
    // and the transport flips into the "playing" UI (Pause icon, "Pause" aria-label).
    fireEvent.click(playBtn);
    expect(mockWs.instance!.currentTime).toBe(0);
    expect(mockWs.instance!.isPlaying()).toBe(true);
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-play')).toHaveAccessibleName(/pause/i),
    );

    // Auto-suggest opt-in jumps the window to the highest-energy span
    // (mocked suggestWindow returns start=42). The observable outcome is
    // the visible selection text shifting to start at 0:42.
    fireEvent.click(screen.getByTestId('trimmer-autosuggest'));
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/0:42\s*–\s*1:02/),
    );
    // And re-pressing Play (toggle pause → play) re-scopes the engine to the new start (42s).
    fireEvent.click(screen.getByTestId('trimmer-play')); // pause
    fireEvent.click(screen.getByTestId('trimmer-play')); // play from new region start
    expect(mockWs.instance!.currentTime).toBe(42);
    expect(mockWs.instance!.isPlaying()).toBe(true);
  });

  it('S4b: clicking the waveform moves the selection window to start at the clicked time', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192)); // 3:12
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const track = await screen.findByTestId('trimmer-waveform');
    // jsdom has no layout — give the track a real rect so px↔time maths work.
    track.getBoundingClientRect = () =>
      ({ left: 0, top: 0, right: 300, bottom: 80, width: 300, height: 80, x: 0, y: 0, toJSON() {} }) as DOMRect;
    // Click at 70% width → 0.7 * 192s ≈ 134s; a 20s window lands at ~2:14–2:34.
    fireEvent.click(track, { clientX: 210, clientY: 40 });
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/2:14\s*–\s*2:34/),
    );
    // And the on-screen region BOX is positioned from the same state (left ≈ 70%).
    const box = screen.getByTestId('trimmer-region');
    const left = parseFloat((box as HTMLElement).style.left);
    expect(left).toBeGreaterThan(65);
    expect(left).toBeLessThan(75);
    // Outcome of Play after the click: engine playhead is scoped to the new window's
    // start (~134.4s) and the transport is in the "playing" state.
    fireEvent.click(screen.getByTestId('trimmer-play'));
    expect(mockWs.instance!.currentTime).toBeGreaterThan(133.4);
    expect(mockWs.instance!.currentTime).toBeLessThan(135.4);
    expect(mockWs.instance!.isPlaying()).toBe(true);
  });

  it('exposes the current selection via the getClip imperative handle', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    const ref = createRef<AudioTrimmerHandle>();
    render(<AudioTrimmer ref={ref} file={makeFile()} onConfirm={vi.fn()} />);
    await screen.findByTestId('trimmer-region');
    const clip = ref.current!.getClip();
    expect(clip).not.toBeNull();
    expect(clip!.file).toBeInstanceOf(File);
    expect(clip!.file.name).toMatch(/reference-\d+\.wav/);
    // Default window is 20s.
    expect(clip!.durationSec).toBe(20);
  });

  it('S5: an in-range clip rests collapsed and expands on demand', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(18)); // 0:18
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const root = await screen.findByTestId('audio-trimmer');
    expect(root).toHaveAttribute('data-state', 'collapsed');
    expect(screen.getByTestId('trimmer-collapsed-note')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('trimmer-expand'));
    await waitFor(() =>
      expect(screen.getByTestId('audio-trimmer')).toHaveAttribute('data-state', 'expanded'),
    );
  });

  it('S6: confirm sends only the sliced span', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    // Capture the emitted clip directly so we can assert on its shape rather
    // than on call-count metadata.
    let emitted: { file: File; dur: number } | null = null;
    const onConfirm = (file: File, dur: number) => {
      emitted = { file, dur };
    };
    render(<AudioTrimmer file={makeFile()} onConfirm={onConfirm} />);
    await screen.findByTestId('audio-trimmer');
    fireEvent.click(screen.getByRole('button', { name: /use this clip/i }));
    // Outcome: confirm produced a wav File of the selected slice, with a duration
    // inside the allowed window range.
    expect(emitted).not.toBeNull();
    expect(emitted!.file).toBeInstanceOf(File);
    expect(emitted!.file.type).toBe('audio/wav');
    expect(emitted!.file.name).toMatch(/reference-\d+\.wav/);
    const dur = emitted!.dur;
    expect(dur).toBeGreaterThanOrEqual(15);
    expect(dur).toBeLessThanOrEqual(45);
  });

  it('S7: a <15s source uses the whole clip with no region picker', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(9)); // 0:09
    render(<AudioTrimmer file={makeFile('memo.m4a')} onConfirm={vi.fn()} />);
    const root = await screen.findByTestId('audio-trimmer');
    expect(root).toHaveAttribute('data-state', 'whole-clip');
    expect(screen.getByTestId('trimmer-shortnote')).toBeInTheDocument();
    expect(screen.queryByTestId('trimmer-region')).not.toBeInTheDocument();
    expect(screen.getByTestId('trimmer-length-chip')).toHaveTextContent(/whole clip/i);
  });

  it('S8: confirming a whole-clip emits the full buffer (no slice)', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(9));
    let emitted: { file: File; dur: number } | null = null;
    render(
      <AudioTrimmer
        file={makeFile('memo.m4a')}
        onConfirm={(file, dur) => {
          emitted = { file, dur };
        }}
      />,
    );
    await screen.findByTestId('trimmer-shortnote');
    fireEvent.click(screen.getByRole('button', { name: /use this clip/i }));
    expect(emitted).not.toBeNull();
    expect(emitted!.file.type).toBe('audio/wav');
    // Whole-clip duration matches the source buffer length, not a 15-45 window.
    expect(emitted!.dur).toBe(9);
  });

  it('S9: decode failure falls back to whole-clip state', async () => {
    (decodeAudioFile as any).mockRejectedValue(new Error('bad file'));
    render(<AudioTrimmer file={makeFile('broken.wav')} onConfirm={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByTestId('audio-trimmer')).toHaveAttribute('data-state', 'whole-clip'),
    );
  });

  it('S10: in-range clip respects expandedByDefault=true', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(25));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} expandedByDefault />);
    const root = await screen.findByTestId('audio-trimmer');
    expect(root).toHaveAttribute('data-state', 'expanded');
    expect(screen.getByTestId('trimmer-region')).toBeInTheDocument();
  });

  it('S11: rewind button moves the engine playhead back to the selection start', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    await screen.findByTestId('trimmer-region');

    // Jump selection start to ~134s by clicking the track at 70%.
    const track = screen.getByTestId('trimmer-waveform');
    track.getBoundingClientRect = () =>
      ({ left: 0, top: 0, right: 300, bottom: 80, width: 300, height: 80, x: 0, y: 0, toJSON() {} }) as DOMRect;
    fireEvent.click(track, { clientX: 210, clientY: 40 });
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/2:14/),
    );

    // Move the engine playhead somewhere else.
    mockWs.instance!.setTime(50);
    expect(mockWs.instance!.currentTime).toBe(50);

    // Rewind snaps it back to the (new) region start, which is ~134.4s.
    fireEvent.click(screen.getByTestId('trimmer-rewind'));
    expect(mockWs.instance!.currentTime).toBeGreaterThan(133.4);
    expect(mockWs.instance!.currentTime).toBeLessThan(135.4);
  });

  it('S12: loop toggle reflects in the play button accessible name', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const loopBtn = await screen.findByTestId('trimmer-loop');
    expect(loopBtn).toHaveAccessibleName(/loop selection/i);
    fireEvent.click(loopBtn);
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-loop')).toHaveAccessibleName(/stop loop/i),
    );
    // Toggle back off.
    fireEvent.click(screen.getByTestId('trimmer-loop'));
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-loop')).toHaveAccessibleName(/loop selection/i),
    );
  });

  it('S13: playback past the region end pauses when not looping', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    await screen.findByTestId('trimmer-region');

    // Start playing — engine flips to playing, transport shows Pause.
    fireEvent.click(screen.getByTestId('trimmer-play'));
    expect(mockWs.instance!.isPlaying()).toBe(true);

    // Simulate the engine emitting a timeupdate that has passed the region end (default 0–20s).
    mockWs.fire!('timeupdate', 25);

    // Outcome: the engine is paused and the transport label is back to Play.
    expect(mockWs.instance!.isPlaying()).toBe(false);
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-play')).toHaveAccessibleName(/play selection/i),
    );
  });

  it('S14: when looping, playback past the region end seeks back to the start', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    await screen.findByTestId('trimmer-region');

    // Turn loop on, then start playback.
    fireEvent.click(screen.getByTestId('trimmer-loop'));
    fireEvent.click(screen.getByTestId('trimmer-play'));
    expect(mockWs.instance!.isPlaying()).toBe(true);
    mockWs.instance!.setTime(10);

    // Crossing the end while looping → seeks back to region.start (0) and stays playing.
    mockWs.fire!('timeupdate', 25);
    expect(mockWs.instance!.currentTime).toBe(0);
    expect(mockWs.instance!.isPlaying()).toBe(true);
  });

  it('S15: ArrowRight / ArrowLeft / Home / End move the window with state-driven box', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const track = await screen.findByTestId('trimmer-waveform');
    track.getBoundingClientRect = () =>
      ({ left: 0, top: 0, right: 300, bottom: 80, width: 300, height: 80, x: 0, y: 0, toJSON() {} }) as DOMRect;

    // Default selection starts at 0:00 for a long source.
    expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/0:00\s*–\s*0:20/);

    // ArrowRight → start moves to 0:01.
    fireEvent.keyDown(track, { key: 'ArrowRight' });
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/0:01\s*–\s*0:21/),
    );

    // Shift+ArrowRight → start moves 5s further.
    fireEvent.keyDown(track, { key: 'ArrowRight', shiftKey: true });
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/0:06\s*–\s*0:26/),
    );

    // ArrowLeft → back by 1s.
    fireEvent.keyDown(track, { key: 'ArrowLeft' });
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/0:05\s*–\s*0:25/),
    );

    // Home → clamp to the start.
    fireEvent.keyDown(track, { key: 'Home' });
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/0:00\s*–\s*0:20/),
    );

    // End → clamp to the end (window slides to fit at the tail).
    fireEvent.keyDown(track, { key: 'End' });
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/2:52\s*–\s*3:12/),
    );

    // Unknown key → no change.
    fireEvent.keyDown(track, { key: 'Tab' });
    expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/2:52\s*–\s*3:12/);
  });

  it('S16: dragging the region body moves the selection window', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const track = await screen.findByTestId('trimmer-waveform');
    track.getBoundingClientRect = () =>
      ({ left: 0, top: 0, right: 300, bottom: 80, width: 300, height: 80, x: 0, y: 0, toJSON() {} }) as DOMRect;

    const region = screen.getByTestId('trimmer-region');
    // Pointer down on the body starts a "move" drag.
    dispatchPointerOn(region, 'pointerdown', 0);
    // Drag 75 px right → 0.25 * 192s = 48s. Window slides from 0–20 to 48–68.
    dispatchPointer('pointermove', 75);
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/0:48\s*–\s*1:08/),
    );
    dispatchPointer('pointerup', 75);
  });

  it('S17: dragging the end handle resizes the window from its right edge', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const track = await screen.findByTestId('trimmer-waveform');
    track.getBoundingClientRect = () =>
      ({ left: 0, top: 0, right: 300, bottom: 80, width: 300, height: 80, x: 0, y: 0, toJSON() {} }) as DOMRect;

    const endHandle = screen.getByTestId('trimmer-handle-end');
    dispatchPointerOn(endHandle, 'pointerdown', 0);
    // Drag 25 px right → 0.0833 * 192 = 16s. End grows 20 + 16 → 36.
    dispatchPointer('pointermove', 25);
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-length-chip')).toHaveTextContent(/36s/),
    );
    dispatchPointer('pointerup', 25);
  });

  it('S18: dragging the start handle resizes the window from its left edge', async () => {
    (decodeAudioFile as any).mockResolvedValue(fakeBuffer(192));
    render(<AudioTrimmer file={makeFile()} onConfirm={vi.fn()} />);
    const track = await screen.findByTestId('trimmer-waveform');
    track.getBoundingClientRect = () =>
      ({ left: 0, top: 0, right: 300, bottom: 80, width: 300, height: 80, x: 0, y: 0, toJSON() {} }) as DOMRect;

    // First, click to put the window at ~70% so we have room on the left to grow.
    fireEvent.click(track, { clientX: 210, clientY: 40 });
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-selection')).toHaveTextContent(/2:14/),
    );

    const startHandle = screen.getByTestId('trimmer-handle-start');
    dispatchPointerOn(startHandle, 'pointerdown', 0);
    // Drag 15 px right → 0.05 * 192 ≈ 9.6s. Start grows from 134.4 by +9.6 → 144 ish,
    // shrinking the window from 20s to ~10.4s, but clamped by WINDOW_MIN=15 → end-15.
    dispatchPointer('pointermove', 15);
    await waitFor(() =>
      expect(screen.getByTestId('trimmer-length-chip')).toHaveTextContent(/15s/),
    );
    dispatchPointer('pointerup', 15);
  });
});

// React's synthetic PointerEvent in jsdom doesn't always carry clientX through
// fireEvent helpers (jsdom lacks the PointerEvent constructor). Dispatch a real
// MouseEvent — React's SyntheticEvent will see clientX, and our handler reads it.
function dispatchPointerOn(el: HTMLElement, type: 'pointerdown', clientX: number) {
  const ev = new MouseEvent(type, { bubbles: true, clientX });
  el.dispatchEvent(ev);
}

// jsdom lacks PointerEvent; the component listens via addEventListener('pointermove', …)
// on window. MouseEvent carries clientX natively, so dispatch one with the right type.
function dispatchPointer(type: 'pointermove' | 'pointerup', clientX: number) {
  const ev = new MouseEvent(type, { bubbles: true, clientX });
  window.dispatchEvent(ev);
}
