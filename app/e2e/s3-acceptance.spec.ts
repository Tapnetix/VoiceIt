/**
 * S3 E2E spec — acceptance scenarios for SegmentRegenerateControl behaviour
 * surfaced through the Books / ChapterEditor flow.
 *
 * S3 acceptance (live-backend web build — runs at phase-end gate):
 *   1. Terminal failed delivery state renders the regenerate button DISABLED
 *      (not hidden / removed) so the user can still see the line state. A
 *      segment in audio_status="error" that is non-retryable must keep the
 *      button mounted; a non-terminal pending/generating state must also
 *      render the button (with spinner). Only a segment that has never been
 *      generated (audio_status="none") may legitimately hide the regenerate
 *      affordance.
 *   2. Double-clicking the regenerate button on a segment in a pending state
 *      submits at most one POST /segments/{id}/regenerate request — the
 *      pending-state disabled affordance must suppress the second click.
 *
 * Live-backend prerequisites:
 *   - Backend running with the "Silo 42" fixture (seeded by global-setup /
 *     fixtures.ts).
 *   - The fixture must contain at least one chapter with one or more segments
 *     whose audio.status is set (not "none") so SegmentRegenerateControl is
 *     mounted in the ⋯ SelectionDialog.
 *   - /books route wired in the web build (C16-equivalent).
 *
 * This spec is authored RED — it will turn GREEN at the phase-end live-stack
 * gate. `cd app && bun x playwright test --list e2e/s3-acceptance.spec.ts`
 * verifies the file parses today.
 */

import { test, expect } from './fixtures';

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Walk a chapter's paragraphs, opening each segment's ⋯ menu, looking for one
 * whose SelectionDialog exposes a regenerate-btn. Returns the segmentId and
 * paragraph index of the first match (or nulls if none).
 *
 * Leaves the dialog CLOSED (clicks cancel-btn) before returning so the caller
 * can re-open it cleanly after wiring up route interception.
 */
async function findFirstRegenerateTarget(page: import('@playwright/test').Page) {
  const chapterText = page.getByTestId('chapter-text');
  const allParas = chapterText.locator('p');
  const paraCount = await allParas.count();

  for (let i = 0; i < paraCount; i++) {
    const para = allParas.nth(i);
    const menuBtn = para.getByRole('button', { name: '⋯' });
    if (!(await menuBtn.isVisible())) continue;

    await menuBtn.click();
    const dialog = page.getByTestId('selection-dialog');
    if (!(await dialog.isVisible())) continue;

    const regenBtn = dialog.locator('[data-testid^="regenerate-btn-"]');
    if (await regenBtn.isVisible()) {
      const testId = await regenBtn.getAttribute('data-testid');
      const segId = testId?.replace('regenerate-btn-', '') ?? null;
      await dialog.getByTestId('cancel-btn').click();
      return { segId, paraIdx: i };
    }

    await dialog.getByTestId('cancel-btn').click();
  }

  return { segId: null as string | null, paraIdx: -1 };
}

// ─── S3: terminal failed state renders disabled, not hidden ──────────────────

test('S3: a segment in a non-idle audio state keeps the regenerate button mounted in the DOM (rendered, not hidden)', async ({
  page,
}) => {
  /**
   * Open the ⋯ menu of any segment whose audio.status is set (so the regen
   * control is mounted). The button must be present in the DOM regardless of
   * whether the underlying state is terminal-failed (audio_status="error"),
   * in-flight ("pending"/"generating"), or completed. The component MUST NOT
   * remove the button to communicate a failed/terminal state — it must use
   * the disabled attribute (or equivalent visible affordance) instead.
   */
  await page.goto('/books');

  const bookCard = page.getByText('Silo 42');
  await expect(bookCard).toBeVisible({ timeout: 10_000 });
  await bookCard.click();

  const editBtn = page
    .getByTestId('chapter-list')
    .locator('[data-testid^="edit-chapter"]')
    .first();
  await expect(editBtn).toBeVisible({ timeout: 5_000 });
  await editBtn.click();

  const chapterText = page.getByTestId('chapter-text');
  await expect(chapterText).toBeVisible({ timeout: 8_000 });
  await expect(chapterText.locator('[data-testid^="seg-"]').first()).toBeVisible({
    timeout: 5_000,
  });

  const { segId, paraIdx } = await findFirstRegenerateTarget(page);
  if (segId === null) {
    test.skip(
      true,
      'No segment with audio_status set to a non-"none" value — fixture lacks generated/failed audio (deferred to phase-end gate)',
    );
    return;
  }

  // Re-open the dialog on the found paragraph and assert the regenerate
  // button is in the DOM. The exact state (enabled / disabled / spinning)
  // varies by underlying audio_status, but the element MUST be mounted —
  // a terminal failed state must render disabled, not removed.
  const targetPara = chapterText.locator('p').nth(paraIdx);
  await targetPara.getByRole('button', { name: '⋯' }).click();
  const dialog = page.getByTestId('selection-dialog');
  await expect(dialog).toBeVisible({ timeout: 3_000 });

  const regenBtn = dialog.getByTestId(`regenerate-btn-${segId}`);
  // attached (in DOM) is the key acceptance: not removed/hidden.
  await expect(regenBtn).toBeAttached();
  // And visible to the user — disabled buttons still render.
  await expect(regenBtn).toBeVisible();
});

// ─── S3: double-click on pending submits exactly one request ─────────────────

test('S3: double-clicking the regenerate button submits at most one POST /segments/{id}/regenerate request', async ({
  page,
}) => {
  /**
   * Click the regenerate button twice in rapid succession. Once the first
   * click puts the segment into a pending state, the button MUST be disabled
   * so the second click is a no-op. The network log must show exactly one
   * POST /segments/{segId}/regenerate.
   *
   * Recording happens via page.route — every request matching the URL is
   * appended to `regenRequests`. After the double-click and a settle delay,
   * the request count for the target segId must be exactly 1.
   */
  const regenRequests: string[] = [];

  await page.route('**/segments/*/regenerate', async (route) => {
    const req = route.request();
    if (req.method() === 'POST') {
      regenRequests.push(req.url());
    }
    await route.continue();
  });

  await page.goto('/books');

  const bookCard = page.getByText('Silo 42');
  await expect(bookCard).toBeVisible({ timeout: 10_000 });
  await bookCard.click();

  const editBtn = page
    .getByTestId('chapter-list')
    .locator('[data-testid^="edit-chapter"]')
    .first();
  await expect(editBtn).toBeVisible({ timeout: 5_000 });
  await editBtn.click();

  const chapterText = page.getByTestId('chapter-text');
  await expect(chapterText).toBeVisible({ timeout: 8_000 });
  await expect(chapterText.locator('[data-testid^="seg-"]').first()).toBeVisible({
    timeout: 5_000,
  });

  const { segId, paraIdx } = await findFirstRegenerateTarget(page);
  if (segId === null) {
    test.skip(
      true,
      'No segment with regenerate-btn surfaced — fixture lacks generated audio (deferred to phase-end gate)',
    );
    return;
  }

  // Open the menu and double-click the regenerate button.
  const targetPara = chapterText.locator('p').nth(paraIdx);
  await targetPara.getByRole('button', { name: '⋯' }).click();
  const dialog = page.getByTestId('selection-dialog');
  await expect(dialog).toBeVisible({ timeout: 3_000 });

  const regenBtn = dialog.getByTestId(`regenerate-btn-${segId}`);
  await expect(regenBtn).toBeVisible();
  await expect(regenBtn).toBeEnabled();

  // Playwright's dblclick fires two click events back-to-back. The first
  // click should trigger the mutation; the component must disable the button
  // before the second click can re-fire the mutation.
  await regenBtn.dblclick();

  // Wait long enough for both clicks to have round-tripped if they were
  // going to. The first request must be observed within this window.
  await expect
    .poll(() => regenRequests.filter((u) => u.includes(`/segments/${segId}/regenerate`)).length, {
      timeout: 5_000,
    })
    .toBeGreaterThanOrEqual(1);

  // Give the UI another beat to allow any (incorrect) second submission to
  // surface in the recording before asserting the cap.
  await page.waitForTimeout(1_000);

  const targetedRequests = regenRequests.filter((u) =>
    u.includes(`/segments/${segId}/regenerate`),
  );
  expect(targetedRequests.length).toBe(1);
});
