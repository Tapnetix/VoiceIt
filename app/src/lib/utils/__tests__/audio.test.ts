import { describe, expect, it, vi } from 'vitest';
import {
  WINDOW_MIN,
  WINDOW_MAX,
  WINDOW_DEFAULT,
  WINDOW_WARN,
  IDEAL_MIN,
  IDEAL_MAX,
  decodeAudioFile,
  suggestWindow,
  sliceToWav,
  classifyWindowLength,
  audioBufferToWav,
  createAudioUrl,
  downloadAudio,
  formatAudioDuration,
  getAudioDuration,
} from '../audio';

// Minimal AudioBuffer stand-in. getChannelData returns the backing Float32Array.
function fakeBuffer(samples: Float32Array, sampleRate = 24000): AudioBuffer {
  return {
    numberOfChannels: 1,
    sampleRate,
    length: samples.length,
    duration: samples.length / sampleRate,
    getChannelData: () => samples,
  } as unknown as AudioBuffer;
}

// 60s buffer: silence everywhere except a loud band at 30s-50s.
function buildBuffer(): AudioBuffer {
  const sr = 24000;
  const total = 60 * sr;
  const s = new Float32Array(total);
  for (let i = 30 * sr; i < 50 * sr; i++) s[i] = 0.8;
  return fakeBuffer(s, sr);
}

describe('window constants', () => {
  it('are the documented values', () => {
    expect(WINDOW_MIN).toBe(15);
    expect(WINDOW_MAX).toBe(45);
    expect(WINDOW_DEFAULT).toBe(20);
    expect(WINDOW_WARN).toBe(30);
    expect(IDEAL_MIN).toBe(15);
    expect(IDEAL_MAX).toBe(20);
  });
});

describe('suggestWindow', () => {
  it('picks the highest-energy contiguous window', () => {
    const { start, end } = suggestWindow(buildBuffer(), WINDOW_DEFAULT);
    expect(end - start).toBeCloseTo(WINDOW_DEFAULT, 1);
    // The 20s default window should sit inside the loud 30-50s band.
    expect(start).toBeGreaterThanOrEqual(30 - 1);
    expect(end).toBeLessThanOrEqual(50 + 1);
  });

  it('clamps the requested length into 15-45', () => {
    const big = suggestWindow(buildBuffer(), 999);
    expect(big.end - big.start).toBeCloseTo(WINDOW_MAX, 1);
    const small = suggestWindow(buildBuffer(), 1);
    expect(small.end - small.start).toBeCloseTo(WINDOW_MIN, 1);
  });

  it('returns the whole clip when the source is shorter than WINDOW_MIN', () => {
    const sr = 24000;
    const short = fakeBuffer(new Float32Array(9 * sr).fill(0.5), sr); // 9s
    const { start, end } = suggestWindow(short, WINDOW_DEFAULT);
    expect(start).toBe(0);
    expect(end).toBeCloseTo(9, 2);
  });
});

describe('classifyWindowLength', () => {
  it('labels ideal / neutral / warn bands', () => {
    expect(classifyWindowLength(15)).toBe('ideal');
    expect(classifyWindowLength(20)).toBe('ideal'); // inclusive top of ideal band
    expect(classifyWindowLength(25)).toBe('neutral');
    expect(classifyWindowLength(31)).toBe('warn');
  });
});

describe('sliceToWav', () => {
  it('encodes ~N seconds of the buffer into a WAV blob', () => {
    const sr = 24000;
    const buf = fakeBuffer(new Float32Array(60 * sr).fill(0.3), sr);
    const wav = sliceToWav(buf, 30, 50); // 20s slice
    expect(wav.type).toBe('audio/wav');
    // 20s mono 16-bit @24k = 20*24000*2 bytes + 44 header.
    const expectedData = 20 * sr * 2;
    expect(wav.size).toBeCloseTo(44 + expectedData, -2);
  });
});

describe('audioBufferToWav (now exported)', () => {
  it('produces a RIFF/WAVE header', async () => {
    const sr = 24000;
    const buf = fakeBuffer(new Float32Array(sr).fill(0.1), sr);
    const blob = audioBufferToWav(buf);
    // jsdom's Blob does not expose .arrayBuffer() directly; use FileReader.
    const ab = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(blob);
    });
    const head = new TextDecoder().decode(ab.slice(0, 4));
    expect(head).toBe('RIFF');
  });
});

describe('decodeAudioFile', () => {
  it('returns the decoded AudioBuffer for the file bytes', async () => {
    const fake = { numberOfChannels: 1, sampleRate: 24000, length: 3, duration: 3 / 24000 } as unknown as AudioBuffer;
    const decodedFor = new Map<ArrayBuffer, AudioBuffer>();
    const bytes = new ArrayBuffer(8);
    decodedFor.set(bytes, fake);
    vi.stubGlobal(
      'AudioContext',
      class {
        closed = false;
        async decodeAudioData(ab: ArrayBuffer) {
          return decodedFor.get(ab)!;
        }
        async close() {
          this.closed = true;
        }
      },
    );
    const file = { arrayBuffer: async () => bytes } as unknown as File;

    const result = await decodeAudioFile(file);

    expect(result).toBe(fake);
    vi.unstubAllGlobals();
  });

  it('releases the AudioContext even when decoding throws', async () => {
    const closedContexts: boolean[] = [];
    vi.stubGlobal(
      'AudioContext',
      class {
        constructor() {
          closedContexts.push(false);
        }
        async decodeAudioData(): Promise<AudioBuffer> {
          throw new Error('bad bytes');
        }
        async close() {
          closedContexts[closedContexts.length - 1] = true;
        }
      },
    );
    const file = { arrayBuffer: async () => new ArrayBuffer(4) } as unknown as File;

    await expect(decodeAudioFile(file)).rejects.toThrow('bad bytes');
    expect(closedContexts).toEqual([true]);
    vi.unstubAllGlobals();
  });
});

describe('createAudioUrl', () => {
  it('joins the server URL and audio id at /audio/<id>', () => {
    expect(createAudioUrl('abc-123', 'http://localhost:8000')).toBe(
      'http://localhost:8000/audio/abc-123',
    );
  });
});

describe('formatAudioDuration', () => {
  it('renders seconds as M:SS with zero-padded seconds', () => {
    expect(formatAudioDuration(0)).toBe('0:00');
    expect(formatAudioDuration(5)).toBe('0:05');
    expect(formatAudioDuration(65)).toBe('1:05');
    expect(formatAudioDuration(125.9)).toBe('2:05'); // floors seconds
    expect(formatAudioDuration(3600)).toBe('60:00');
  });
});

describe('downloadAudio', () => {
  it('attaches and detaches the download link cleanly (no DOM residue)', () => {
    const wav = 'blob:fake-url';
    const filename = 'chapter-1.wav';
    const before = document.body.childNodes.length;

    // Suppress jsdom's "Not implemented: navigation" by intercepting click on
    // any anchor created during this call.
    const realCreate = document.createElement.bind(document);
    const spy = vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = realCreate(tag) as HTMLAnchorElement;
      if (tag === 'a') el.click = () => {};
      return el;
    });

    downloadAudio(wav, filename);
    spy.mockRestore();

    expect(document.body.childNodes.length).toBe(before);
    expect(document.querySelectorAll(`a[href="${wav}"]`).length).toBe(0);
  });

  it('configures the anchor href and download attributes from its arguments', () => {
    const wav = 'blob:capture-url';
    const filename = 'sample.wav';
    let captured: { href: string; download: string } | null = null;

    const realCreate = document.createElement.bind(document);
    const spy = vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = realCreate(tag) as HTMLAnchorElement;
      if (tag === 'a') {
        // Capture the values set by downloadAudio. Replace click with a no-op
        // so jsdom does not warn about navigation.
        el.click = () => {
          captured = { href: el.href, download: el.download };
        };
      }
      return el;
    });

    downloadAudio(wav, filename);
    spy.mockRestore();

    expect(captured).not.toBeNull();
    expect(captured!.href).toContain(wav);
    expect(captured!.download).toBe(filename);
  });
});

describe('getAudioDuration', () => {
  it('returns the recordedDuration shortcut when set', async () => {
    const file = Object.assign(new Blob(['x']), { name: 'rec.webm' }) as unknown as File & {
      recordedDuration?: number;
    };
    file.recordedDuration = 12.5;
    await expect(getAudioDuration(file)).resolves.toBe(12.5);
  });

  it('ignores non-finite recordedDuration and falls through to decoding', async () => {
    const decoded = { duration: 7.25 } as AudioBuffer;
    vi.stubGlobal(
      'AudioContext',
      class {
        async decodeAudioData() {
          return decoded;
        }
        async close() {}
      },
    );
    const file = Object.assign(
      { arrayBuffer: async () => new ArrayBuffer(2) } as unknown as File,
      { recordedDuration: Number.NaN },
    ) as File & { recordedDuration?: number };

    await expect(getAudioDuration(file)).resolves.toBe(7.25);
    vi.unstubAllGlobals();
  });

  it('returns the decoded AudioBuffer duration for files without a recordedDuration', async () => {
    const decoded = { duration: 3.5 } as AudioBuffer;
    vi.stubGlobal(
      'AudioContext',
      class {
        async decodeAudioData() {
          return decoded;
        }
        async close() {}
      },
    );
    const file = { arrayBuffer: async () => new ArrayBuffer(8) } as unknown as File;

    await expect(getAudioDuration(file)).resolves.toBe(3.5);
    vi.unstubAllGlobals();
  });
});
