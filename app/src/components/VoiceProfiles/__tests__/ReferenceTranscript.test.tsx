/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import {
  ReferenceTranscript,
  type ReferenceTranscriptProps,
} from '@/components/VoiceProfiles/ReferenceTranscript';

const noop = () => {};

const baseProps: ReferenceTranscriptProps = {
  value: '',
  onChange: noop,
  status: 'idle',
  isTranscribing: false,
  regeneratePrompt: false,
  onRetranscribe: noop,
  onAcceptRegenerate: noop,
  onKeepEdits: noop,
};

// ── Controlled host ────────────────────────────────────────────────────────────
// A minimal stand-in for the parent that owns ReferenceTranscript's state. By
// routing the callbacks through useState we can assert observable outcomes
// (rendered text, banner visibility, retry counters) instead of inspecting
// internal spy invocations. This mirrors how the real ProfileForm wires the
// component up via useReferenceTranscript.
interface HostProps {
  initialValue?: string;
  initialStatus?: ReferenceTranscriptProps['status'];
  initialRegeneratePrompt?: boolean;
  initialHasClip?: boolean;
}

function Host({
  initialValue = '',
  initialStatus = 'idle',
  initialRegeneratePrompt = false,
  initialHasClip = true,
}: HostProps) {
  const [value, setValue] = useState(initialValue);
  const [status, setStatus] =
    useState<ReferenceTranscriptProps['status']>(initialStatus);
  const [isTranscribing, setIsTranscribing] = useState(
    initialStatus === 'transcribing' || initialStatus === 'downloading',
  );
  const [regeneratePrompt, setRegeneratePrompt] = useState(initialRegeneratePrompt);
  const [retryCount, setRetryCount] = useState(0);
  // The "last decision" the host made in response to a regenerate prompt;
  // surfaced as visible text so a test can assert the click triggered the
  // documented behaviour without inspecting the spy directly.
  const [regenerateDecision, setRegenerateDecision] = useState<'none' | 'accept' | 'keep'>(
    'none',
  );

  return (
    <div>
      <ReferenceTranscript
        value={value}
        onChange={setValue}
        status={status}
        isTranscribing={isTranscribing}
        regeneratePrompt={regeneratePrompt}
        hasClip={initialHasClip}
        onRetranscribe={() => {
          // Simulate the parent's reaction: kick off transcription. The
          // observable outcome is the transcribing indicator + the retry tally.
          setRetryCount((n) => n + 1);
          setStatus('transcribing');
          setIsTranscribing(true);
        }}
        onAcceptRegenerate={() => {
          // Accepting regenerate dismisses the banner and clears the user's
          // edits so a fresh transcription can fill the box.
          setRegeneratePrompt(false);
          setRegenerateDecision('accept');
          setValue('');
        }}
        onKeepEdits={() => {
          // Keeping edits dismisses the banner without touching the text.
          setRegeneratePrompt(false);
          setRegenerateDecision('keep');
        }}
      />
      <span data-testid="host-retry-count">{retryCount}</span>
      <span data-testid="host-regenerate-decision">{regenerateDecision}</span>
    </div>
  );
}

describe('ReferenceTranscript', () => {
  it('shows the auto-filled hint and the text when filled (S1)', () => {
    render(
      <ReferenceTranscript {...baseProps} status="filled" value="detected words" />,
    );
    expect(screen.getByTestId('transcript-autofilled-hint')).toBeInTheDocument();
    expect(screen.getByTestId('transcript-input')).toHaveValue('detected words');
  });

  it('shows the transcribing indicator, disables Re-transcribe, but keeps the input typeable (S2)', async () => {
    render(<Host initialStatus="transcribing" />);

    expect(screen.getByTestId('transcript-transcribing')).toBeInTheDocument();
    expect(screen.getByTestId('transcript-retranscribe')).toBeDisabled();

    const input = screen.getByTestId('transcript-input') as HTMLTextAreaElement;
    expect(input).not.toBeDisabled();

    await userEvent.type(input, 'hello');
    // Observable outcome: the textarea reflects what the user typed because
    // the component forwarded each onChange into the parent's state.
    expect(input).toHaveValue('hello');
  });

  it('shows an error note with an empty editable field and an enabled retry on failure (S4)', () => {
    render(<ReferenceTranscript {...baseProps} status="failed" value="" />);
    expect(screen.getByTestId('transcript-error')).toBeInTheDocument();
    expect(screen.getByTestId('transcript-input')).not.toBeDisabled();
    expect(screen.getByTestId('transcript-retranscribe')).not.toBeDisabled();
  });

  it('triggers a transcription run when Re-transcribe is clicked (S7)', async () => {
    render(<Host initialStatus="filled" initialValue="words" />);

    // Pre-click: idle from the host's perspective — no retries logged, no
    // transcribing indicator.
    expect(screen.getByTestId('host-retry-count')).toHaveTextContent('0');
    expect(screen.queryByTestId('transcript-transcribing')).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId('transcript-retranscribe'));

    // Observable outcome: the host advanced into the transcribing state and
    // recorded exactly one retry — the click reached the parent's handler.
    expect(screen.getByTestId('host-retry-count')).toHaveTextContent('1');
    expect(screen.getByTestId('transcript-transcribing')).toBeInTheDocument();
    expect(screen.getByTestId('transcript-retranscribe')).toBeDisabled();
  });

  it('confirms regenerate clears edits and dismisses the banner', async () => {
    render(
      <Host
        initialStatus="filled"
        initialRegeneratePrompt
        initialValue="my hand edits"
      />,
    );

    expect(screen.getByTestId('transcript-regenerate-prompt')).toBeInTheDocument();
    expect(screen.getByTestId('transcript-input')).toHaveValue('my hand edits');

    await userEvent.click(screen.getByTestId('transcript-regenerate-confirm'));

    // Observable outcome: banner is gone, edits cleared, host recorded "accept".
    expect(screen.queryByTestId('transcript-regenerate-prompt')).not.toBeInTheDocument();
    expect(screen.getByTestId('transcript-input')).toHaveValue('');
    expect(screen.getByTestId('host-regenerate-decision')).toHaveTextContent('accept');
  });

  it('keeping edits dismisses the banner without touching the text', async () => {
    render(
      <Host
        initialStatus="filled"
        initialRegeneratePrompt
        initialValue="my hand edits"
      />,
    );

    expect(screen.getByTestId('transcript-regenerate-prompt')).toBeInTheDocument();

    await userEvent.click(screen.getByTestId('transcript-regenerate-keep'));

    // Observable outcome: banner gone, text preserved, host recorded "keep".
    expect(screen.queryByTestId('transcript-regenerate-prompt')).not.toBeInTheDocument();
    expect(screen.getByTestId('transcript-input')).toHaveValue('my hand edits');
    expect(screen.getByTestId('host-regenerate-decision')).toHaveTextContent('keep');
  });

  it('shows the model-download state with Re-transcribe disabled but the input still editable', () => {
    render(<ReferenceTranscript {...baseProps} status="downloading" isTranscribing />);
    expect(screen.getByTestId('transcript-downloading')).toBeInTheDocument();
    expect(screen.queryByTestId('transcript-transcribing')).not.toBeInTheDocument();
    expect(screen.getByTestId('transcript-retranscribe')).toBeDisabled();
    expect(screen.getByTestId('transcript-input')).not.toBeDisabled();
  });

  it('shows the backend error detail in the failed state', () => {
    render(<ReferenceTranscript {...baseProps} status="failed" value="" error="HTTP 500: boom" />);
    expect(screen.getByTestId('transcript-error')).toHaveTextContent('HTTP 500: boom');
  });

  it('disables Re-transcribe and shows a hint until a clip is confirmed (hasClip=false)', () => {
    render(<ReferenceTranscript {...baseProps} status="idle" hasClip={false} />);
    expect(screen.getByTestId('transcript-retranscribe')).toBeDisabled();
    expect(screen.getByTestId('transcript-need-clip')).toBeInTheDocument();
  });

  it('enables Re-transcribe once a clip is confirmed (hasClip=true)', () => {
    render(<ReferenceTranscript {...baseProps} status="filled" value="words" hasClip />);
    expect(screen.getByTestId('transcript-retranscribe')).not.toBeDisabled();
    expect(screen.queryByTestId('transcript-need-clip')).not.toBeInTheDocument();
  });
});
