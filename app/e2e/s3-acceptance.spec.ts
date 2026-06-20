/**
 * S3 E2E spec — acceptance scenarios for SegmentRegenerateControl behaviour
 * surfaced through the Books / ChapterEditor flow.
 *
 * S3 acceptance (live-backend web build — runs at phase-end gate):
 *   1. A segment in a TERMINAL `failed` + non-retryable audio state
 *      (audio_status="error" with retryable=false) renders the regenerate
 *      button DISABLED — present and visible in the DOM, but `disabled` so
 *      the user cannot fire another doomed request. The brief is explicit:
 *      the button must NOT be hidden or removed; it must be rendered as a
 *      disabled affordance so the failed state is visible to the user.
 *   2. Double-clicking the regenerate button on a segment in a pending state
 *      submits at most one POST /segments/{id}/regenerate request — the
 *      pending-state disabled affordance must suppress the second click.
 *
 * Live-backend prerequisites:
 *   - Backend running with the "Silo 42" fixture (seeded by global-setup /
 *     fixtures.ts).
 *   - For test 1: the fixture must contain at least one segment whose
 *     audio.status is "error" (terminal failure) with retryable=false so
 *     SegmentRegenerateControl is mounted and rendered disabled. If today's
 *     fixture lacks such a segment OR the data model lacks the `retryable`
 *     field entirely, the test is skipped at the live gate (see
 *     bugs_escalated against SegmentRegenerateControl.tsx in the audit
 *     report — the implementation does not currently disable on terminal
 *     `error` state).
 *   - For test 2: the fixture must contain at least one segment whose
 *     audio.status is set (not "none") so the regenerate button is mounted.
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

/**
 * Query the backend directly for the first segment in a TERMINAL failed +
 * non-retryable state (audio_status="error" with retryable=false).
 *
 * Returns the segmentId + chapter/book context if such a segment exists in
 * the live fixture. Returns null if none is present (in which case the
 * caller skips at the live gate).
 *
 * This avoids walking every paragraph's ⋯ menu just to find a failure case —
 * once we know the seg/chapter/book ids, we can navigate directly.
 */
async function findTerminalFailedSegmentViaApi(
  request: import('@playwright/test').APIRequestContext,
): Promise<{ segId: string; chapterId: string; bookId: string } | null> {
  const booksRes = await request.get('/books');
  if (!booksRes.ok()) return null;
  const books = (await booksRes.json()) as Array<{ id: string; name: string }>;
  const silo = books.find((b) => b.name === 'Silo 42');
  if (!silo) return null;

  const chaptersRes = await request.get(`/books/${silo.id}/chapters`);
  if (!chaptersRes.ok()) return null;
  const chapters = (await chaptersRes.json()) as Array<{ id: string }>;

  for (const ch of chapters) {
    const segsRes = await request.get(`/chapters/${ch.id}/segments`);
    if (!segsRes.ok()) continue;
    const segs = (await segsRes.json()) as Array<{
      id: string;
      audio?: { status?: string; retryable?: boolean };
    }>;
    const target = segs.find(
      (s) => s.audio?.status === 'error' && s.audio?.retryable === false,
    );
    if (target) {
      return { segId: target.id, chapterId: ch.id, bookId: silo.id };
    }
  }
  return null;
}

// ─── S3: terminal failed + non-retryable renders DISABLED, not hidden ────────

test('S3: a segment in terminal audio_status="error" with retryable=false renders the regenerate button disabled (not hidden)', async ({
  page,
  request,
}) => {
  /**
   * The brief's explicit acceptance: when a segment reaches a TERMINAL
   * failure state that the user cannot recover from by retrying
   * (audio_status="error" + retryable=false), the regenerate button must be
   * rendered DISABLED — visible in the DOM, but `disabled` so a click is a
   * no-op. This is contrasted with "hidden" (removed from the DOM): the
   * user must still SEE the failure context.
   *
   * We locate such a segment via the backend API (so we get a known
   * seg/chapter/book id), navigate directly to the chapter editor, open
   * the ⋯ menu for that paragraph, and assert:
   *   - the regenerate button is attached + visible
   *   - the regenerate button is disabled
   *
   * If the live fixture has no such segment, we skip — the live gate will
   * surface the gap. See the audit's bugs_escalated entry against
   * SegmentRegenerateControl.tsx:45-48: the current implementation only
   * disables on isPending || audio_status==='pending'|'generating' — it
   * does NOT disable on terminal audio_status='error' with retryable=false,
   * and the SegmentResponse contract does not expose a `retryable` field.
   * Until the component AND the contract are updated, this test fails the
   * spec on any fixture that does seed a terminal-failed segment.
   */
  const target = await findTerminalFailedSegmentViaApi(request);
  if (target === null) {
    test.skip(
      true,
      'No segment with audio_status="error" and retryable=false in fixture — terminal-failed branch cannot be exercised against the live backend until the fixture seeds one (and until SegmentResponse exposes retryable). See bugs_escalated.',
    );
    return;
  }

  await page.goto('/books');

  const bookCard = page.getByText('Silo 42');
  await expect(bookCard).toBeVisible({ timeout: 10_000 });
  await bookCard.click();

  const editBtn = page
    .getByTestId('chapter-list')
    .locator(`[data-testid="edit-chapter-${target.chapterId}"]`);
  await expect(editBtn).toBeVisible({ timeout: 5_000 });
  await editBtn.click();

  const chapterText = page.getByTestId('chapter-text');
  await expect(chapterText).toBeVisible({ timeout: 8_000 });
  await expect(chapterText.locator('[data-testid^="seg-"]').first()).toBeVisible({
    timeout: 5_000,
  });

  // Find the paragraph containing our terminal-failed segment by walking
  // paragraphs and opening each ⋯ menu until we see the matching
  // regenerate-btn-{segId}. (Paragraph index for a specific segment is
  // not surfaced in the DOM directly; the segId in the dialog is the
  // ground truth.)
  const allParas = chapterText.locator('p');
  const paraCount = await allParas.count();

  let opened = false;
  for (let i = 0; i < paraCount; i++) {
    const para = allParas.nth(i);
    const menuBtn = para.getByRole('button', { name: '⋯' });
    if (!(await menuBtn.isVisible())) continue;

    await menuBtn.click();
    const dialog = page.getByTestId('selection-dialog');
    if (!(await dialog.isVisible())) continue;

    const targetBtn = dialog.getByTestId(`regenerate-btn-${target.segId}`);
    if (await targetBtn.isVisible().catch(() => false)) {
      opened = true;
      break;
    }
    await dialog.getByTestId('cancel-btn').click();
  }

  expect(opened, 'expected to find the terminal-failed segment in the chapter editor').toBe(
    true,
  );

  const dialog = page.getByTestId('selection-dialog');
  const regenBtn = dialog.getByTestId(`regenerate-btn-${target.segId}`);

  // Core brief acceptance — present + visible (NOT hidden / removed) …
  await expect(regenBtn).toBeAttached();
  await expect(regenBtn).toBeVisible();
  // … AND disabled, because the segment is in a terminal non-retryable
  // failure. A click here must be a no-op.
  await expect(regenBtn).toBeDisabled();
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
