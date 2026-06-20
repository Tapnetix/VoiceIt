/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import { ReferenceTranscript } from '@/components/VoiceProfiles/ReferenceTranscript';
import type { ReferenceTranscriptStatus } from '@/lib/hooks/useReferenceTranscript';

// ---------------------------------------------------------------------------
// Acceptance scenario S11
// ---------------------------------------------------------------------------
//
// Vitest + jsdom exercise of `ReferenceTranscript` (the presentational surface
// the user sees while a voice-clone reference clip is being transcribed). The
// brief covers three user-visible failure / edge modes:
//
//   S11.1 — Empty-string response shows the "no transcript yet" state, not
//           blank.  When the STT backend returns an empty transcription the
//           hook flips status to 'failed' and surfaces the error
//           "the transcription came back empty" (see
//           useReferenceTranscript.ts ~L94). The component must render a
//           visible, human-readable failure note instead of leaving the user
//           staring at a silent, blank textarea.
//
//   S11.2 — Over-max content scrolls / truncates without layout break.  Very
//           long pasted transcripts must not blow up the surrounding card:
//           the wrapper survives as a single contained node, the textarea
//           still carries the full value, and overflow falls on the
//           textarea's own native scrolling rather than overflowing siblings.
//
//   S11.3 — Save while a fetch is in flight is last-write-wins.  The
//           transcription is asynchronous; the user can keep typing in the
//           textarea while transcribing is still active. When the fetch
//           eventually completes and the parent re-renders with the freshly
//           detected text, that latest value MUST be what the textarea
//           presents — the controlled prop is the single source of truth.
//
// Boundary discipline: ReferenceTranscript is a controlled presentational
// component, so no first-party module is mocked. We drive its public props
// directly and assert on what the user sees (rendered text, textarea value,
// DOM structure).

type Props = React.ComponentProps<typeof ReferenceTranscript>;

function makeProps(overrides: Partial<Props> = {}): Props {
  return {
    value: '',
    onChange: vi.fn(),
    status: 'idle' as ReferenceTranscriptStatus,
    isTranscribing: false,
    regeneratePrompt: false,
    onRetranscribe: vi.fn(),
    onAcceptRegenerate: vi.fn(),
    onKeepEdits: vi.fn(),
    hasClip: true,
    error: null,
    ...overrides,
  };
}

describe('S11: ReferenceTranscript user-visible edge modes', () => {
  it('S11.1: empty STT response surfaces a visible failure note, not a silent blank', () => {
    // Mirror what useReferenceTranscript writes to the component when the
    // backend returns an empty transcription (status='failed', error set to
    // the literal sentence the hook uses).
    const emptyTranscriptError = 'the transcription came back empty';
    render(
      React.createElement(
        ReferenceTranscript,
        makeProps({
          value: '',
          status: 'failed',
          isTranscribing: false,
          error: emptyTranscriptError,
        }),
      ),
    );

    // The textarea is rendered and is empty — that part is expected, the
    // STT layer had nothing to fill it with.
    const textarea = screen.getByTestId('transcript-input') as HTMLTextAreaElement;
    expect(textarea.value).toBe('');

    // ...but the user MUST also see a non-blank explanation. The component
    // surfaces the failed state via the `transcript-error` paragraph, which
    // composes the generic error note with the specific reason in parens.
    const errorNote = screen.getByTestId('transcript-error');
    expect(errorNote).toBeInTheDocument();
    expect(errorNote.textContent ?? '').toContain(emptyTranscriptError);
    // The generic note (whatever the locale renders) is non-empty too, so
    // the user is never shown a stray "()" with no surrounding sentence.
    expect((errorNote.textContent ?? '').trim().length).toBeGreaterThan(
      emptyTranscriptError.length + 2,
    );

    // None of the success / progress affordances are showing — the user
    // is not misled into thinking the transcript was filled.
    expect(screen.queryByTestId('transcript-autofilled-hint')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcript-transcribing')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcript-downloading')).not.toBeInTheDocument();
  });

  it('S11.2: very long content stays inside the textarea without breaking the wrapper layout', () => {
    // 50k characters: well past anything realistic STT would emit, and big
    // enough to expose any naive container-sizing bug. We assert layout
    // invariants, not pixel widths — jsdom does not lay out.
    const overMaxLength = 50_000;
    const overMaxValue = 'x'.repeat(overMaxLength);
    render(
      React.createElement(
        ReferenceTranscript,
        makeProps({
          value: overMaxValue,
          status: 'filled',
        }),
      ),
    );

    // Wrapper survives as a single contained node — over-long content did
    // not cause React to bail or duplicate the surface.
    const wrappers = screen.getAllByTestId('reference-transcript');
    expect(wrappers.length).toBe(1);
    const wrapper = wrappers[0];

    // The textarea is the carrier for the long value; the parent wrapper
    // does NOT spill text directly. (If a future change accidentally
    // rendered `value` outside the textarea, the wrapper's textContent
    // would balloon past the textarea's own value length.)
    const textarea = screen.getByTestId('transcript-input') as HTMLTextAreaElement;
    expect(textarea.value).toBe(overMaxValue);
    expect(textarea.value.length).toBe(overMaxLength);
    expect(textarea.tagName).toBe('TEXTAREA');

    // The wrapper still contains the textarea as a descendant; no portal
    // escape, no detachment.
    expect(wrapper.contains(textarea)).toBe(true);

    // The wrapper itself stays a single block with the documented structural
    // class — a layout-breaking regression would typically come from a
    // changed root tag or stripped class. We assert the structural anchor.
    expect(wrapper.tagName).toBe('DIV');
    expect(wrapper.className).toContain('space-y-2');

    // The status hint area (autofilled note) is also still rendered: the
    // long value did not crowd out sibling UI.
    expect(screen.getByTestId('transcript-autofilled-hint')).toBeInTheDocument();
  });

  it('S11.3: typing while a transcription is in flight, and a later prop update wins (last-write-wins)', async () => {
    const user = userEvent.setup();

    // Parent-owned controlled value, matching how useReferenceTranscript's
    // consumers wire the component. The harness mirrors the parent's
    // useState wiring so the textarea re-renders with each keystroke (true
    // controlled-component behaviour).
    const lastTypedRef: { current: string } = { current: '' };

    function Harness({
      override,
      forceStatus,
      forceIsTranscribing,
    }: {
      override: string | null;
      forceStatus: ReferenceTranscriptStatus;
      forceIsTranscribing: boolean;
    }) {
      const [typed, setTyped] = React.useState('');
      // When the parent (the hook) has its own last-write to publish — e.g.
      // the transcription returned — it overrides the user's typed value.
      const displayed = override !== null ? override : typed;
      lastTypedRef.current = typed;
      return React.createElement(
        ReferenceTranscript,
        makeProps({
          value: displayed,
          onChange: setTyped,
          status: forceStatus,
          isTranscribing: forceIsTranscribing,
        }),
      );
    }

    const { rerender } = render(
      React.createElement(Harness, {
        override: null,
        forceStatus: 'transcribing',
        forceIsTranscribing: true,
      }),
    );

    // Progress affordance is visible — the user knows a fetch is in flight.
    expect(screen.getByTestId('transcript-transcribing')).toBeInTheDocument();

    // The textarea remains interactive during the in-flight fetch: the user
    // can still type a manual save. (Disabling here would block legitimate
    // edits while STT is running.)
    let textarea = screen.getByTestId('transcript-input') as HTMLTextAreaElement;
    expect(textarea).not.toBeDisabled();

    await user.type(textarea, 'manual');

    // Every keystroke landed through the controlled boundary; the textarea
    // shows the user's typed value mid-flight.
    textarea = screen.getByTestId('transcript-input') as HTMLTextAreaElement;
    expect(textarea.value).toBe('manual');
    expect(lastTypedRef.current).toBe('manual');

    // Now the in-flight transcription resolves with text that disagrees
    // with the user's manual edit. The hook (the parent) decides — its
    // policy here is last-write-wins on the controlled `value` prop, so
    // the textarea displays whatever the hook wrote last.
    const transcribedText = 'auto-transcribed text from the server';
    rerender(
      React.createElement(Harness, {
        override: transcribedText,
        forceStatus: 'filled',
        forceIsTranscribing: false,
      }),
    );

    // Whatever the parent's last write was, that is what the textarea now
    // shows. The progress affordance is gone; the filled affordance is up.
    expect((screen.getByTestId('transcript-input') as HTMLTextAreaElement).value).toBe(
      transcribedText,
    );
    expect(screen.queryByTestId('transcript-transcribing')).not.toBeInTheDocument();
    expect(screen.getByTestId('transcript-autofilled-hint')).toBeInTheDocument();
  });
});
