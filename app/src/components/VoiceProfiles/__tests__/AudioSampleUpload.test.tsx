/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

// Stub out the shadcn form wrappers so they render without a react-hook-form context.
vi.mock('@/components/ui/form', () => ({
  FormItem: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  FormControl: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  FormMessage: () => null,
}));

import { AudioSampleUpload } from '@/components/VoiceProfiles/AudioSampleUpload';

const makeAudioFile = (name = 'clip.wav') =>
  new File(['x'], name, { type: 'audio/wav' });

const makeTextFile = (name = 'notes.txt') =>
  new File(['hello'], name, { type: 'text/plain' });

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
    throw new Error(
      `expected exactly one native <button> matching ${name}, got ${matches.length}`,
    );
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

  it('clicking Choose File opens the hidden file picker', async () => {
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
    const clickSpy = vi.spyOn(input, 'click');

    await user.click(getNativeButton(/choose file/i));

    expect(clickSpy).toHaveBeenCalledTimes(1);
  });
});

describe('AudioSampleUpload — file input change', () => {
  it('emits the selected file via onFileChange when a file is picked', () => {
    const onFileChange = vi.fn();
    const { container } = render(
      <AudioSampleUpload
        file={null}
        onFileChange={onFileChange}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const picked = makeAudioFile('picked.wav');

    fireEvent.change(input, { target: { files: [picked] } });

    expect(onFileChange).toHaveBeenCalledWith(picked);
  });

  it('emits undefined when the file picker is cleared (no file selected)', () => {
    const onFileChange = vi.fn();
    const { container } = render(
      <AudioSampleUpload
        file={null}
        onFileChange={onFileChange}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    fireEvent.change(input, { target: { files: [] } });

    expect(onFileChange).toHaveBeenCalledWith(undefined);
  });
});

describe('AudioSampleUpload — drag and drop', () => {
  it('accepts a dropped audio file and emits it via onFileChange', () => {
    const onFileChange = vi.fn();
    render(
      <AudioSampleUpload
        file={null}
        onFileChange={onFileChange}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const dropzone = getDropzone();
    const dropped = makeAudioFile('dropped.wav');

    fireEvent.dragOver(dropzone);
    fireEvent.drop(dropzone, { dataTransfer: { files: [dropped] } });

    expect(onFileChange).toHaveBeenCalledWith(dropped);
  });

  it('rejects a dropped non-audio file (does NOT call onFileChange)', () => {
    const onFileChange = vi.fn();
    render(
      <AudioSampleUpload
        file={null}
        onFileChange={onFileChange}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const dropzone = getDropzone();
    const dropped = makeTextFile();

    fireEvent.drop(dropzone, { dataTransfer: { files: [dropped] } });

    expect(onFileChange).not.toHaveBeenCalled();
  });

  it('does not call onFileChange when dropping with no files', () => {
    const onFileChange = vi.fn();
    render(
      <AudioSampleUpload
        file={null}
        onFileChange={onFileChange}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const dropzone = getDropzone();

    fireEvent.drop(dropzone, { dataTransfer: { files: [] } });

    expect(onFileChange).not.toHaveBeenCalled();
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
  it('opens the file picker when Enter is pressed on the dropzone', () => {
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
    const clickSpy = vi.spyOn(input, 'click');
    const dropzone = getDropzone();

    fireEvent.keyDown(dropzone, { key: 'Enter' });

    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it('opens the file picker when Space is pressed on the dropzone', () => {
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
    const clickSpy = vi.spyOn(input, 'click');
    const dropzone = getDropzone();

    fireEvent.keyDown(dropzone, { key: ' ' });

    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it('ignores keys other than Enter/Space on the dropzone', () => {
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
    const clickSpy = vi.spyOn(input, 'click');
    const dropzone = getDropzone();

    fireEvent.keyDown(dropzone, { key: 'Tab' });
    fireEvent.keyDown(dropzone, { key: 'a' });

    expect(clickSpy).not.toHaveBeenCalled();
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

  it('invokes onPlayPause when the play/pause control is clicked', async () => {
    const onPlayPause = vi.fn();
    const user = userEvent.setup();
    render(
      <AudioSampleUpload
        file={makeAudioFile()}
        onFileChange={vi.fn()}
        onPlayPause={onPlayPause}
        isPlaying={false}
        fieldName="sample"
      />,
    );

    await user.click(getNativeButton(/^play$/i));

    expect(onPlayPause).toHaveBeenCalledTimes(1);
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

  it('clicking Remove emits undefined and clears the underlying file input value', async () => {
    const onFileChange = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <AudioSampleUpload
        file={makeAudioFile()}
        onFileChange={onFileChange}
        onPlayPause={vi.fn()}
        isPlaying={false}
        fieldName="sample"
      />,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    // Simulate that a prior selection left a value on the input.
    Object.defineProperty(input, 'value', {
      configurable: true,
      writable: true,
      value: 'C:\\fakepath\\old.wav',
    });

    await user.click(getNativeButton(/remove/i));

    expect(onFileChange).toHaveBeenCalledWith(undefined);
    expect(input.value).toBe('');
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
