import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useBookProgress, type BookProgressHandlers } from '@/lib/hooks/useBookProgress';
import { apiClient } from '@/lib/api/client';
import type { BookProgressEvent } from '@/lib/api/types';

/**
 * Tier-1 behaviour rewrite of useBookProgress.
 *
 * What this hook promises (its observable contract):
 *  1. It opens exactly one EventSource at apiClient.getBookEventsUrl(bookId).
 *  2. Each contract-04 SSE payload is parsed and delivered to the handler
 *     keyed by its `type` discriminator — and the payload is delivered
 *     intact (no field rewriting).
 *  3. Unknown payload shapes (ready/ping heartbeats, malformed JSON) are
 *     swallowed silently — they never propagate to consumer handlers and
 *     never throw.
 *  4. A transient `onerror` from the EventSource keeps the stream OPEN
 *     (so the browser can auto-reconnect) and synthesises a connection
 *     error event with stage='connection'.
 *  5. Unmount or bookId change closes the underlying EventSource so the
 *     browser tears down the network connection.
 *
 * Test style: we capture the observable outcome (the payloads each consumer
 * receives, and the EventSource's open/closed state) into plain arrays/flags,
 * then assert on those captured outcomes. No vi.fn() call-count assertions,
 * no first-party module mocks — only the browser-global `EventSource` is
 * stubbed (jsdom does not implement it).
 */

interface CapturedEvent {
  channel: keyof BookProgressHandlers;
  payload: BookProgressEvent;
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  readonly url: string;
  // `closed` reflects whether the consumer of EventSource has called .close().
  // The real EventSource has a readyState; we expose `closed` for direct assertion.
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  // Test helpers — these mimic what a real SSE server would push down the wire.
  pushMessage(data: unknown): void {
    const raw = typeof data === 'string' ? data : JSON.stringify(data);
    this.onmessage?.({ data: raw } as MessageEvent);
  }

  pushTransportError(): void {
    this.onerror?.();
  }
}

// Capture-all handlers that record every delivered event into a shared sink.
// This mirrors how a real consumer would react — by reducing events into state —
// instead of asserting on call counts.
function makeCapturingHandlers(sink: CapturedEvent[]): BookProgressHandlers {
  return {
    onAnalysisProgress: (e) => sink.push({ channel: 'onAnalysisProgress', payload: e }),
    onCharacterDetected: (e) => sink.push({ channel: 'onCharacterDetected', payload: e }),
    onAnalysisComplete: (e) => sink.push({ channel: 'onAnalysisComplete', payload: e }),
    onGenerationProgress: (e) => sink.push({ channel: 'onGenerationProgress', payload: e }),
    onGenerationComplete: (e) => sink.push({ channel: 'onGenerationComplete', payload: e }),
    onExportProgress: (e) => sink.push({ channel: 'onExportProgress', payload: e }),
    onExportComplete: (e) => sink.push({ channel: 'onExportComplete', payload: e }),
    onError: (e) => sink.push({ channel: 'onError', payload: e }),
  };
}

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as unknown as { EventSource: typeof FakeEventSource }).EventSource = FakeEventSource;
});

afterEach(() => {
  delete (globalThis as unknown as { EventSource?: typeof FakeEventSource }).EventSource;
});

describe('useBookProgress', () => {
  it('subscribes to the per-book SSE endpoint exposed by apiClient', () => {
    renderHook(() => useBookProgress('book-42', {}));

    const expectedUrl = apiClient.getBookEventsUrl('book-42');
    const opened = FakeEventSource.instances.map((s) => s.url);
    expect(opened).toEqual([expectedUrl]);
  });

  it('does not open a connection when bookId is empty', () => {
    renderHook(() => useBookProgress('', {}));
    expect(FakeEventSource.instances).toEqual([]);
  });

  it('routes each contract-04 event to its matching handler with the payload intact', () => {
    const captured: CapturedEvent[] = [];
    renderHook(() => useBookProgress('b1', makeCapturingHandlers(captured)));
    const source = FakeEventSource.instances[0];

    const events: BookProgressEvent[] = [
      { type: 'analysis_progress', stage: 'detect', progress: 50, message: 'scanning' },
      { type: 'character_detected', character: { id: 'c1', name: 'Mira' }, total: 1 },
      { type: 'analysis_complete', character_count: 4, chapter_count: 12 },
      { type: 'generation_progress', chapter_id: 'ch1', completed: 3, errors: 0, total: 10, overall_progress: 30 },
      { type: 'generation_complete', chapter_id: 'ch1' },
      { type: 'export_progress', progress: 75, stage: 'mux' },
      { type: 'export_complete', download_path: '/tmp/book.m4b', filename: 'book.m4b' },
      { type: 'error', stage: 'cast', message: 'Voice assignment failed' },
    ];
    for (const evt of events) source.pushMessage(evt);

    // Each event lands on exactly its matching channel, in send-order,
    // and the payload is forwarded byte-for-byte (no shape rewriting).
    expect(captured).toEqual([
      { channel: 'onAnalysisProgress', payload: events[0] },
      { channel: 'onCharacterDetected', payload: events[1] },
      { channel: 'onAnalysisComplete', payload: events[2] },
      { channel: 'onGenerationProgress', payload: events[3] },
      { channel: 'onGenerationComplete', payload: events[4] },
      { channel: 'onExportProgress', payload: events[5] },
      { channel: 'onExportComplete', payload: events[6] },
      { channel: 'onError', payload: events[7] },
    ]);
  });

  it('does not invoke optional handlers that the consumer omitted', () => {
    const captured: CapturedEvent[] = [];
    // Subscribe to character_detected only. analysis_progress must be a no-op,
    // not throw — because not every consumer cares about every channel.
    renderHook(() =>
      useBookProgress('b1', {
        onCharacterDetected: (e) => captured.push({ channel: 'onCharacterDetected', payload: e }),
      }),
    );
    const source = FakeEventSource.instances[0];

    source.pushMessage({ type: 'analysis_progress', stage: 'detect', progress: 10 });
    source.pushMessage({ type: 'character_detected', character: { id: 'c1', name: 'Mira' }, total: 1 });

    // Only the registered channel observed an event.
    expect(captured.map((c) => c.channel)).toEqual(['onCharacterDetected']);
  });

  it('uses the most recently rendered handlers, even after rerenders, without resubscribing', () => {
    // The hook's docstring says "Tears down on unmount or bookId change" —
    // i.e. handler-only changes must NOT recreate the EventSource (that
    // would drop in-flight events and thrash the network). And whichever
    // handler is current at the moment the event arrives is the one called.
    const firstCaptured: BookProgressEvent[] = [];
    const secondCaptured: BookProgressEvent[] = [];

    const handlersA: BookProgressHandlers = { onAnalysisComplete: (e) => firstCaptured.push(e) };
    const handlersB: BookProgressHandlers = { onAnalysisComplete: (e) => secondCaptured.push(e) };

    const { rerender } = renderHook(
      ({ h }: { h: BookProgressHandlers }) => useBookProgress('b1', h),
      { initialProps: { h: handlersA } },
    );

    expect(FakeEventSource.instances).toHaveLength(1);
    const source = FakeEventSource.instances[0];

    rerender({ h: handlersB });

    // Still the same single EventSource — handler swap did not resubscribe.
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(source.closed).toBe(false);

    source.pushMessage({ type: 'analysis_complete', character_count: 1, chapter_count: 2 });

    // The event reached the *new* handler, not the stale one.
    expect(firstCaptured).toEqual([]);
    expect(secondCaptured).toEqual([
      { type: 'analysis_complete', character_count: 1, chapter_count: 2 },
    ]);
  });

  it('closes the previous stream and opens a new one when bookId changes', () => {
    const { rerender } = renderHook(
      ({ id }: { id: string }) => useBookProgress(id, {}),
      { initialProps: { id: 'b1' } },
    );

    const first = FakeEventSource.instances[0];
    expect(first.closed).toBe(false);
    expect(first.url).toBe(apiClient.getBookEventsUrl('b1'));

    rerender({ id: 'b2' });

    expect(FakeEventSource.instances).toHaveLength(2);
    const second = FakeEventSource.instances[1];
    expect(first.closed).toBe(true); // previous connection torn down
    expect(second.closed).toBe(false); // new connection live
    expect(second.url).toBe(apiClient.getBookEventsUrl('b2'));
  });

  it('closes the underlying EventSource on unmount', () => {
    const { unmount } = renderHook(() => useBookProgress('b1', {}));
    const source = FakeEventSource.instances[0];
    expect(source.closed).toBe(false);

    unmount();

    expect(source.closed).toBe(true);
  });

  it('silently ignores non-JSON heartbeat frames without crashing or notifying consumers', () => {
    const captured: CapturedEvent[] = [];
    renderHook(() => useBookProgress('b1', makeCapturingHandlers(captured)));
    const source = FakeEventSource.instances[0];

    // ready/ping heartbeats arrive as opaque non-JSON strings.
    expect(() => source.pushMessage('ready')).not.toThrow();
    expect(() => source.pushMessage('ping')).not.toThrow();

    expect(captured).toEqual([]);
    expect(source.closed).toBe(false);
  });

  it('silently ignores JSON payloads with unknown type discriminators', () => {
    const captured: CapturedEvent[] = [];
    renderHook(() => useBookProgress('b1', makeCapturingHandlers(captured)));
    const source = FakeEventSource.instances[0];

    source.pushMessage({ type: 'something_we_do_not_know_yet', payload: 1 });

    expect(captured).toEqual([]);
    expect(source.closed).toBe(false);
  });

  it('keeps the stream open on transport error and synthesises a connection-stage error event', () => {
    // A transient onerror must NOT close the EventSource — the browser
    // will auto-reconnect — and the consumer must be told about it via
    // the onError channel with stage='connection' so the UI can show a
    // "reconnecting…" indicator.
    const captured: CapturedEvent[] = [];
    renderHook(() => useBookProgress('b1', makeCapturingHandlers(captured)));
    const source = FakeEventSource.instances[0];

    source.pushTransportError();

    expect(source.closed).toBe(false);
    expect(captured).toHaveLength(1);
    expect(captured[0].channel).toBe('onError');
    expect(captured[0].payload).toEqual({
      type: 'error',
      stage: 'connection',
      message: 'SSE connection error',
    });
  });

  it('absorbs transport errors safely when the consumer registered no onError handler', () => {
    renderHook(() => useBookProgress('b1', {}));
    const source = FakeEventSource.instances[0];

    expect(() => source.pushTransportError()).not.toThrow();
    expect(source.closed).toBe(false);
  });
});
