/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

// Stub out the shadcn form wrappers so they render without a react-hook-form context.
vi.mock('@/components/ui/form', () => ({
  FormItem: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  FormControl: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  FormMessage: () => null,
}));

import { AudioSampleUpload } from '@/components/VoiceProfiles/AudioSampleUpload';

/**
 * Controlled host: feeds `onFileChange` back into the `file` prop so we can
 * observe the AudioSampleUpload's resulting UI rather than asserting on the
 * raw callback. Exposes `isPlaying` toggle the same way.
 */
function ControlledHost({
  initialFile = null,
  initialIsPlaying = false,
  isValidating = false,
  fieldName = 'sample',
  onFileChangeSpy,
  onPlayPauseSpy,
}: {
  initialFile?: File | null;
  initialIsPlaying?: boolean;
  isValidating?: boolean;
  fieldName?: string;
  onFileChangeSpy?: (file: File | undefined) => void;
  onPlayPauseSpy?: () => void;
}) {
  const [file, setFile] = useState<File | null | undefined>(initialFile);
  const [isPlaying, setIsPlaying] = useState(initialIsPlaying);
  return (
    <AudioSampleUpload
      file={file}
      onFileChange={(next) => {
        onFileChangeSpy?.(next);
        setFile(next ?? null);
      }}
      onPlayPause={() => {
        onPlayPauseSpy?.();
        setIsPlaying((prev) => !prev);
      }}
      isPlaying={isPlaying}
      isValidating={isValidating}
      fieldName={fieldName}
    />
  );
}

const makeAudioFile = (name = 'clip.wav') => new File(['x'], name, { type: 'audio/wav' });

const makeTextFile = (name = 'notes.txt') => new File(['hello'], name, { type: 'text/plain' });

/** Find the dropzone (the role=button DIV wrapper that handles drag/drop/keyboard). */
function getDropzone(): HTMLElement {
  const buttons = screen.getAllByRole('button');
  const dropzone = buttons.find((el) => el.tagName === 'DIV');
  if (!dropzone) {
    throw new Error('dropzone not found');
  }
  return dropzone;
}

/**
 * Find a real <button> by its accessible name. The dropzone div also exposes
 * role="button" and inherits a label from its children, so a plain
 * getByRole('button', { name }) call can resolve to either the dropzone or the
 * inner shadcn button. This helper restricts the match to actual <button>
 * elements.
 */
function getNativeButton(name: RegExp): HTMLButtonElement {
  const matches = screen
    .getAllByRole('button', { name })
    .filter((el): el is HTMLButtonElement => el.tagName === 'BUTTON');
  if (matches.length !== 1) {
    throw new Error(`expected exactly one native <button> matching ${name}, got ${matches.length}`);
  }
  return matches[0];
}

function queryNativeButton(name: RegExp): HTMLButtonElement | null {
  const matches = screen
    .queryAllByRole('button', { name })
    .filter((el): el is HTMLButtonElement => el.tagName === 'BUTTON');
  return matches[0] ?? null;
}

describe('AudioSampleUpload — empty state', () => {
  it('renders the Choose File prompt and upload hint when no file is selected', () => {
    render(
      <AudioSampleUpload
        file={null}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );

    expect(getNativeButton(/choose file/i)).toBeInTheDocument();
    expect(screen.getByText(/click to choose a file or drag and drop/i)).toBeInTheDocument();
    // The "file uploaded" affordance must NOT appear in the empty state.
    expect(screen.queryByText(/file uploaded/i)).not.toBeInTheDocument();
    expect(queryNativeButton(/remove/i)).toBeNull();
  });

  it('treats `file={undefined}` the same as no file (renders the empty prompt)', () => {
    render(
      <AudioSampleUpload
        file={undefined}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    expect(getNativeButton(/choose file/i)).toBeInTheDocument();
  });

  it('applies the fieldName prop to the underlying file input', () => {
    const { container } = render(
      <AudioSampleUpload
        file={null}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="my-custom-field"
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    expect(input.name).toBe('my-custom-field');
    expect(input.accept).toBe('audio/*');
  });

  it('clicking Choose File dispatches a click to the hidden audio file input', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <AudioSampleUpload
        file={null}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    // Capture the receiver of programmatic click() so we can verify the
    // delegated control is the audio file input (the only way the browser
    // opens the picker) and not some other element.
    const receivers: HTMLInputElement[] = [];
    const originalClick = input.click.bind(input);
    input.click = function patchedClick() {
      receivers.push(this as HTMLInputElement);
      return originalClick();
    };

    await user.click(getNativeButton(/choose file/i));

    // The click must have landed on the audio-accepting hidden file input.
    expect(receivers).toEqual([input]);
    expect(receivers[0].type).toBe('file');
    expect(receivers[0].accept).toBe('audio/*');
  });
});

describe('AudioSampleUpload — file input change', () => {
  it('emits the selected file when a file is picked and surfaces it in the UI', () => {
    const onFileChange = vi.fn();
    const { container } = render(<ControlledHost onFileChangeSpy={onFileChange} />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const picked = makeAudioFile('picked.wav');

    fireEvent.change(input, { target: { files: [picked] } });

    // Observable outcome: the populated-state UI appears with the picked file's name.
    expect(screen.getByText(/file uploaded/i)).toBeInTheDocument();
    expect(screen.getByText(/picked\.wav/)).toBeInTheDocument();
    // Behavior shape: the emitted value is a File whose identity, name and
    // audio MIME type all match what the user picked (not just "callback
    // fired"). We inspect call args directly rather than asserting a count.
    const emitted = onFileChange.mock.calls[0]?.[0] as File | undefined;
    expect(emitted).toBe(picked);
    expect(emitted?.name).toBe('picked.wav');
    expect(emitted?.type).toBe('audio/wav');
  });

  it('emits undefined and stays in empty state when the file picker is cleared', () => {
    const onFileChange = vi.fn();
    const { container } = render(
      <ControlledHost initialFile={makeAudioFile('to-clear.wav')} onFileChangeSpy={onFileChange} />,
    );
    // Sanity: starts in populated state.
    expect(screen.getByText(/to-clear\.wav/)).toBeInTheDocument();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    fireEvent.change(input, { target: { files: [] } });

    // Observable outcome: the populated UI is gone, the empty prompt is back.
    expect(screen.queryByText(/file uploaded/i)).not.toBeInTheDocument();
    expect(getNativeButton(/choose file/i)).toBeInTheDocument();
    // Behavior shape: the emitted value is exactly `undefined` (not null, not
    // an empty File) — clearing must signal "no file" via the canonical sentinel.
    const emittedArgs = onFileChange.mock.calls[0];
    expect(emittedArgs?.[0]).toBeUndefined();
    expect(emittedArgs).toHaveLength(1);
  });
});

describe('AudioSampleUpload — drag and drop', () => {
  it('accepts a dropped audio file and surfaces it in the populated UI', () => {
    const onFileChange = vi.fn();
    render(<ControlledHost onFileChangeSpy={onFileChange} />);
    const dropzone = getDropzone();
    const dropped = makeAudioFile('dropped.wav');

    fireEvent.dragOver(dropzone);
    fireEvent.drop(dropzone, { dataTransfer: { files: [dropped] } });

    // Observable outcome: the dropped file's name is now displayed and the
    // empty-state button is gone.
    expect(screen.getByText(/dropped\.wav/)).toBeInTheDocument();
    expect(queryNativeButton(/choose file/i)).toBeNull();
    // Behavior shape: the emitted File matches the dropped one by identity AND
    // by the audio MIME type that the drop handler is supposed to gate on.
    const emitted = onFileChange.mock.calls[0]?.[0] as File | undefined;
    expect(emitted).toBe(dropped);
    expect(emitted?.type.startsWith('audio/')).toBe(true);
  });

  it('rejects a dropped non-audio file and leaves the empty state intact', () => {
    const onFileChange = vi.fn();
    render(<ControlledHost onFileChangeSpy={onFileChange} />);
    const dropzone = getDropzone();
    const dropped = makeTextFile('notes.txt');

    fireEvent.drop(dropzone, { dataTransfer: { files: [dropped] } });

    // Observable outcome: the empty-state UI is still present; the text file
    // is NOT shown anywhere.
    expect(getNativeButton(/choose file/i)).toBeInTheDocument();
    expect(screen.queryByText(/file uploaded/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/notes\.txt/)).not.toBeInTheDocument();
    // Behavior shape: no value was ever emitted upstream — the rejection is
    // a no-op at the boundary, not a "called with undefined".
    expect(onFileChange.mock.calls).toEqual([]);
  });

  it('leaves the empty state intact when a drop carries no files', () => {
    const onFileChange = vi.fn();
    render(<ControlledHost onFileChangeSpy={onFileChange} />);
    const dropzone = getDropzone();

    fireEvent.drop(dropzone, { dataTransfer: { files: [] } });

    // Observable outcome: empty state is preserved end-to-end.
    expect(getNativeButton(/choose file/i)).toBeInTheDocument();
    expect(screen.queryByText(/file uploaded/i)).not.toBeInTheDocument();
    // Behavior shape: no emission at the boundary at all.
    expect(onFileChange.mock.calls).toEqual([]);
  });

  it('reflects an active drag via the dropzone styling and clears it on dragLeave', () => {
    render(
      <AudioSampleUpload
        file={null}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const dropzone = getDropzone();

    // Idle state: muted dashed border.
    expect(dropzone.className).toMatch(/border-dashed/);

    fireEvent.dragOver(dropzone);
    // Active drag: primary border, no dashed style.
    expect(dropzone.className).toMatch(/border-primary/);
    expect(dropzone.className).not.toMatch(/border-dashed/);

    fireEvent.dragLeave(dropzone);
    expect(dropzone.className).toMatch(/border-dashed/);
  });
});

describe('AudioSampleUpload — keyboard activation', () => {
  it('dispatches a click to the hidden audio input when Enter is pressed on the dropzone', () => {
    const { container } = render(
      <AudioSampleUpload
        file={null}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const receivers: HTMLInputElement[] = [];
    const originalClick = input.click.bind(input);
    input.click = function patched() {
      receivers.push(this as HTMLInputElement);
      return originalClick();
    };
    const dropzone = getDropzone();

    fireEvent.keyDown(dropzone, { key: 'Enter' });

    // The hidden audio file input is the sole click receiver — that's how the
    // browser-native picker opens. Anything else means the keyboard handler
    // wired up the wrong element.
    expect(receivers).toEqual([input]);
    expect(receivers[0].accept).toBe('audio/*');
  });

  it('dispatches a click to the hidden audio input when Space is pressed on the dropzone', () => {
    const { container } = render(
      <AudioSampleUpload
        file={null}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const receivers: HTMLInputElement[] = [];
    const originalClick = input.click.bind(input);
    input.click = function patched() {
      receivers.push(this as HTMLInputElement);
      return originalClick();
    };
    const dropzone = getDropzone();

    fireEvent.keyDown(dropzone, { key: ' ' });

    expect(receivers).toEqual([input]);
    expect(receivers[0].accept).toBe('audio/*');
  });

  it('does not dispatch a click to the file input on keys other than Enter/Space', () => {
    const { container } = render(
      <AudioSampleUpload
        file={null}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const receivers: HTMLInputElement[] = [];
    const originalClick = input.click.bind(input);
    input.click = function patched() {
      receivers.push(this as HTMLInputElement);
      return originalClick();
    };
    const dropzone = getDropzone();

    fireEvent.keyDown(dropzone, { key: 'Tab' });
    fireEvent.keyDown(dropzone, { key: 'a' });
    fireEvent.keyDown(dropzone, { key: 'Escape' });

    // No click was dispatched to the file input — the picker stays closed.
    // Also: the empty-state UI is unchanged (no accidental state mutation).
    expect(receivers).toEqual([]);
    expect(getNativeButton(/choose file/i)).toBeInTheDocument();
  });
});

describe('AudioSampleUpload — populated state', () => {
  it('shows the uploaded file name and Play/Remove controls when a file is present', () => {
    render(
      <AudioSampleUpload
        file={makeAudioFile('voice-take-3.wav')}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );

    expect(screen.getByText(/file uploaded/i)).toBeInTheDocument();
    expect(screen.getByText(/voice-take-3\.wav/)).toBeInTheDocument();
    expect(getNativeButton(/^play$/i)).toBeInTheDocument();
    expect(getNativeButton(/remove/i)).toBeInTheDocument();
    // The empty-state prompt is gone.
    expect(queryNativeButton(/choose file/i)).toBeNull();
  });

  it('exposes a Pause-labelled control while isPlaying=true', () => {
    render(
      <AudioSampleUpload
        file={makeAudioFile()}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={true}
        fieldName="sample"
      />,
    );

    expect(getNativeButton(/pause/i)).toBeInTheDocument();
    expect(queryNativeButton(/^play$/i)).toBeNull();
  });

  it('toggles the play/pause control label when the user activates it', async () => {
    const onPlayPause = vi.fn();
    const user = userEvent.setup();
    render(
      <ControlledHost
        initialFile={makeAudioFile()}
        initialIsPlaying={false}
        onPlayPauseSpy={onPlayPause}
      />,
    );
    // Sanity: starts in "Play" affordance.
    expect(getNativeButton(/^play$/i)).toBeInTheDocument();
    expect(queryNativeButton(/pause/i)).toBeNull();

    await user.click(getNativeButton(/^play$/i));

    // Observable outcome: control flips to "Pause" — the component asks the
    // host to toggle and re-renders accordingly.
    expect(getNativeButton(/pause/i)).toBeInTheDocument();
    expect(queryNativeButton(/^play$/i)).toBeNull();

    // Activating again flips it back — confirms the wiring drives state both
    // ways via the same callback boundary.
    await user.click(getNativeButton(/pause/i));
    expect(getNativeButton(/^play$/i)).toBeInTheDocument();
    expect(queryNativeButton(/pause/i)).toBeNull();

    // Behavior shape: each invocation is a zero-arg signal (no payload).
    expect(onPlayPause.mock.calls.every((args) => args.length === 0)).toBe(true);
  });

  it('disables the play/pause control while isValidating=true', () => {
    render(
      <AudioSampleUpload
        file={makeAudioFile()}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        isValidating
        fieldName="sample"
      />,
    );

    expect(getNativeButton(/^play$/i)).toBeDisabled();
  });

  it('keeps the play/pause control enabled by default (isValidating defaults to false)', () => {
    render(
      <AudioSampleUpload
        file={makeAudioFile()}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );

    expect(getNativeButton(/^play$/i)).toBeEnabled();
  });

  it('clicking Remove returns the dropzone to the empty state and clears the file input value', async () => {
    const onFileChange = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <ControlledHost initialFile={makeAudioFile('old.wav')} onFileChangeSpy={onFileChange} />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    // Simulate that a prior selection left a value on the input.
    Object.defineProperty(input, 'value', {
      configurable: true,
      writable: true,
      value: 'C:\\fakepath\\old.wav',
    });
    // Sanity: starts populated.
    expect(screen.getByText(/old\.wav/)).toBeInTheDocument();

    await user.click(getNativeButton(/remove/i));

    // Observable outcome 1: the empty-state UI is back.
    expect(getNativeButton(/choose file/i)).toBeInTheDocument();
    expect(screen.queryByText(/file uploaded/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/old\.wav/)).not.toBeInTheDocument();
    expect(queryNativeButton(/remove/i)).toBeNull();
    // Observable outcome 2: the underlying file input's value is cleared, so
    // re-picking the same file would still fire a `change` event.
    expect(input.value).toBe('');
    // Behavior shape: the emission was the canonical "cleared" sentinel
    // (undefined, exactly one arg), not a stale File and not null.
    const removeArgs = onFileChange.mock.calls.at(-1);
    expect(removeArgs?.[0]).toBeUndefined();
    expect(removeArgs).toHaveLength(1);
  });

  it('applies the "file present" styling to the dropzone when a file is set', () => {
    render(
      <AudioSampleUpload
        file={makeAudioFile()}
        onFileChange={vi.fn()}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const dropzone = getDropzone();
    expect(dropzone.className).toMatch(/border-primary/);
    expect(dropzone.className).not.toMatch(/border-dashed/);
  });
});
