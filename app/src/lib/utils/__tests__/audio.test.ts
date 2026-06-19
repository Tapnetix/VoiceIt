// Unit tests for app/src/lib/utils/audio.ts.
//
// Statement coverage target (U-js-014): >= 80%. The convertToWav, fallback
// HTMLMediaElement.duration path in getAudioDuration, and stereo
// audioBufferToWav header-field assertions below carry the coverage from
// the prior 74% baseline up past 98%.
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
  convertToWav,
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

  it('falls back to HTMLMediaElement.duration when decodeAudioData throws', async () => {
    vi.stubGlobal(
      'AudioContext',
      class {
        async decodeAudioData(): Promise<AudioBuffer> {
          throw new Error('cannot decode');
        }
        async close() {}
      },
    );
    const createdUrls: string[] = [];
    const revokedUrls: string[] = [];
    vi.stubGlobal('URL', {
      createObjectURL: (_b: Blob) => {
        const u = `blob:fallback-${createdUrls.length}`;
        createdUrls.push(u);
        return u;
      },
      revokeObjectURL: (u: string) => {
        revokedUrls.push(u);
      },
    });
    vi.stubGlobal(
      'Audio',
      class {
        duration = 4.2;
        private listeners: Record<string, Array<() => void>> = {};
        constructor() {
          Object.defineProperty(this, 'src', {
            configurable: true,
            set: (_v: string) => {
              queueMicrotask(() => {
                for (const cb of this.listeners.loadedmetadata ?? []) cb();
              });
            },
            get: () => '',
          });
        }
        addEventListener(event: string, cb: () => void) {
          (this.listeners[event] ||= []).push(cb);
        }
      },
    );

    const file = { arrayBuffer: async () => new ArrayBuffer(2) } as unknown as File;
    await expect(getAudioDuration(file)).resolves.toBe(4.2);
    expect(createdUrls.length).toBeGreaterThan(0);
    expect(revokedUrls).toEqual(createdUrls);

    vi.unstubAllGlobals();
  });

  it('rejects when the HTMLMediaElement fallback reports invalid duration', async () => {
    vi.stubGlobal(
      'AudioContext',
      class {
        async decodeAudioData(): Promise<AudioBuffer> {
          throw new Error('cannot decode');
        }
        async close() {}
      },
    );
    vi.stubGlobal('URL', {
      createObjectURL: () => 'blob:bad',
      revokeObjectURL: () => {},
    });
    vi.stubGlobal(
      'Audio',
      class {
        duration = Number.NaN;
        private listeners: Record<string, Array<() => void>> = {};
        constructor() {
          Object.defineProperty(this, 'src', {
            configurable: true,
            set: (_v: string) => {
              queueMicrotask(() => {
                for (const cb of this.listeners.loadedmetadata ?? []) cb();
              });
            },
            get: () => '',
          });
        }
        addEventListener(event: string, cb: () => void) {
          (this.listeners[event] ||= []).push(cb);
        }
      },
    );

    const file = { arrayBuffer: async () => new ArrayBuffer(2) } as unknown as File;
    await expect(getAudioDuration(file)).rejects.toThrow(/invalid duration/);

    vi.unstubAllGlobals();
  });

  it('rejects when the HTMLMediaElement fallback emits an error event', async () => {
    vi.stubGlobal(
      'AudioContext',
      class {
        async decodeAudioData(): Promise<AudioBuffer> {
          throw new Error('cannot decode');
        }
        async close() {}
      },
    );
    vi.stubGlobal('URL', {
      createObjectURL: () => 'blob:err',
      revokeObjectURL: () => {},
    });
    vi.stubGlobal(
      'Audio',
      class {
        duration = 0;
        private listeners: Record<string, Array<() => void>> = {};
        constructor() {
          Object.defineProperty(this, 'src', {
            configurable: true,
            set: (_v: string) => {
              queueMicrotask(() => {
                for (const cb of this.listeners.error ?? []) cb();
              });
            },
            get: () => '',
          });
        }
        addEventListener(event: string, cb: () => void) {
          (this.listeners[event] ||= []).push(cb);
        }
      },
    );

    const file = { arrayBuffer: async () => new ArrayBuffer(2) } as unknown as File;
    await expect(getAudioDuration(file)).rejects.toThrow(/Failed to load audio file/);

    vi.unstubAllGlobals();
  });
});

describe('convertToWav', () => {
  it('returns a WAV blob produced by decoding the input and re-encoding the AudioBuffer', async () => {
    const sr = 24000;
    const samples = new Float32Array(sr).fill(0.25); // 1s mono
    const decoded = fakeBuffer(samples, sr);
    vi.stubGlobal(
      'AudioContext',
      class {
        async decodeAudioData() {
          return decoded;
        }
        async close() {}
      },
    );
    // jsdom's Blob lacks .arrayBuffer(); pass a Blob-shaped fake so the
    // production code path is exercised end-to-end without touching jsdom internals.
    const input = {
      arrayBuffer: async () => new ArrayBuffer(4),
      type: 'audio/webm',
      size: 4,
    } as unknown as Blob;

    const wav = await convertToWav(input);

    expect(wav.type).toBe('audio/wav');
    // Verify RIFF/WAVE header bytes — proves the bytes came from audioBufferToWav.
    const ab = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(wav);
    });
    const head = new TextDecoder().decode(ab.slice(0, 4));
    const wave = new TextDecoder().decode(ab.slice(8, 12));
    expect(head).toBe('RIFF');
    expect(wave).toBe('WAVE');
    // 1s mono 16-bit @24k payload = 24000*2 bytes + 44 header.
    expect(wav.size).toBe(44 + sr * 2);
    vi.unstubAllGlobals();
  });

  it('propagates the AudioContext.decodeAudioData rejection', async () => {
    vi.stubGlobal(
      'AudioContext',
      class {
        async decodeAudioData(): Promise<AudioBuffer> {
          throw new Error('decode failed');
        }
        async close() {}
      },
    );
    const input = {
      arrayBuffer: async () => new ArrayBuffer(1),
      type: 'audio/webm',
      size: 1,
    } as unknown as Blob;
    await expect(convertToWav(input)).rejects.toThrow('decode failed');
    vi.unstubAllGlobals();
  });
});

describe('audioBufferToWav header values', () => {
  it('encodes channel count, sample rate, and byte-rate in the WAV header', async () => {
    const sr = 16000;
    const left = new Float32Array(sr).fill(0.1);
    const right = new Float32Array(sr).fill(-0.1);
    const buf: AudioBuffer = {
      numberOfChannels: 2,
      sampleRate: sr,
      length: sr,
      duration: 1,
      getChannelData: (c: number) => (c === 0 ? left : right),
    } as unknown as AudioBuffer;

    const blob = audioBufferToWav(buf);
    const ab = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(blob);
    });
    const view = new DataView(ab);
    expect(view.getUint16(22, true)).toBe(2); // numberOfChannels
    expect(view.getUint32(24, true)).toBe(sr); // sampleRate
    expect(view.getUint32(28, true)).toBe(sr * 2 * 2); // byteRate = sr * channels * bytes/sample
    expect(view.getUint16(34, true)).toBe(16); // bitDepth
    // data chunk size = samples * channels * 2 bytes
    expect(view.getUint32(40, true)).toBe(sr * 2 * 2);
  });
});

describe('suggestWindow boundary handling', () => {
  it('clamps to end at duration when the picked window would overshoot the buffer', () => {
    // Buffer of 30.5s where the loud band sits at the very end.
    // The 20s window starting near the last possible frame would push end past duration.
    const sr = 24000;
    const total = Math.floor(30.5 * sr);
    const s = new Float32Array(total);
    // Loud only in the final 5s — forces the algorithm to pick a start near the end.
    for (let i = total - 5 * sr; i < total; i++) s[i] = 0.9;
    const buf = fakeBuffer(s, sr);

    const { start, end } = suggestWindow(buf, 20);
    expect(end).toBeLessThanOrEqual(buf.duration + 1e-6);
    expect(end - start).toBeCloseTo(20, 1);
    expect(start).toBeGreaterThanOrEqual(0);
  });
});
