/**
 * S4 E2E spec — BookLibrary acceptance: empty-state copy + cover-placeholder fallback.
 *
 * Owner: audit-coverage task S4 (source: app/src/components/BooksTab/BookLibrary.tsx).
 *
 * PREREQUISITES:
 *   - The web dev server is started by `just dev-web` (playwright.config.ts webServer).
 *   - The /books route is wired in the web build (post-C16).
 *
 * Scope: these specs exercise BookLibrary in isolation by intercepting GET /books
 * at the network boundary so the rendered state is deterministic (independent of
 * the seeded fixture). No first-party module mocking — Playwright's `page.route`
 * intercepts the real HTTP boundary the React Query hook calls into.
 *
 * Note on the "cover 404 fallback" scenario: BookLibrary renders a fixed-size
 * placeholder element for the cover slot rather than fetching a remote image
 * URL. The acceptance criterion ("falls back to placeholder image without
 * layout break") is therefore expressed as: no broken <img> element appears in
 * the rendered library, and the placeholder slot is rendered at its declared
 * size for every book (regardless of whether the book payload carries a
 * cover_path). That is exactly the "no layout break" guarantee.
 */
import { expect, test } from './fixtures';

// Backend default port is 17493 (uvicorn backend.main:app; see `just dev-web`).
// Scope the intercept to the API origin so we DO NOT intercept the SPA
// navigation to the page itself (http://localhost:5173/books) — that route is
// served by the Vite dev server / Tauri router, not the backend, and
// intercepting it would return the stub JSON in place of the HTML shell.
const BOOKS_LIST_URL = /^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):17493\/books\/?(?:\?.*)?$/;

// ─── S4-a: empty library shows empty-state copy + import CTA ─────────────────

test('S4: empty library shows the empty-state copy and an import CTA', async ({
  page,
}) => {
  // Intercept GET /books and force an empty list. The fixture re-seeds a "Silo 42"
  // book before each test, so without this intercept the library is never empty.
  await page.route(BOOKS_LIST_URL, async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
      return;
    }
    await route.continue();
  });

  await page.goto('/books');

  // Empty-state container (Card) carries the same data-testid="book-grid" as
  // the populated grid — assert it is visible so the page rendered.
  const bookGrid = page.getByTestId('book-grid');
  await expect(bookGrid).toBeVisible({ timeout: 10_000 });

  // Empty-state copy (i18n: books.empty)
  await expect(bookGrid).toContainText(
    /Import an EPUB, FB2, TXT, or PDF to create your first audiobook/i,
  );

  // Import CTA is rendered inside the empty-state card (i18n: books.import.btn)
  const importBtn = bookGrid.getByTestId('import-book-btn');
  await expect(importBtn).toBeVisible();
  await expect(importBtn).toBeEnabled();
  await expect(importBtn).toHaveText(/import book/i);

  // No book cards are rendered when the library is empty.
  await expect(page.locator('[data-book-id]')).toHaveCount(0);

  // Clicking the import CTA transitions the BooksTab into the import view —
  // the dropzone surfaced by BookImport becomes visible.
  await importBtn.click();
  await expect(page.getByTestId('book-dropzone')).toBeVisible({ timeout: 5_000 });
});

// ─── S4-b: populated library renders cover placeholder without layout break ──

test('S4: populated library renders a fixed-size cover placeholder for every book without broken images', async ({
  page,
}) => {
  // Provide a deterministic list of two books — one with a cover_path that
  // would 404 if naively dereferenced as an <img src>, one without.
  // The library must render both without surfacing any broken-image element
  // and without collapsing the cover slot's reserved layout space.
  const stubbedBooks = [
    {
      id: 'book-with-broken-cover',
      title: 'Book With Broken Cover',
      author: 'Acceptance Author',
      source_format: 'epub',
      cover_path: '/covers/does-not-exist-404.jpg',
      status: 'analyzed',
      chapter_count: 3,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    {
      id: 'book-without-cover',
      title: 'Book Without Cover',
      source_format: 'txt',
      status: 'imported',
      chapter_count: 1,
      created_at: '2026-01-02T00:00:00Z',
      updated_at: '2026-01-02T00:00:00Z',
    },
  ];

  await page.route(BOOKS_LIST_URL, async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(stubbedBooks),
      });
      return;
    }
    await route.continue();
  });

  // Track failed image loads on the page — a broken cover <img> would surface
  // here and the test would fail.
  const failedImageUrls: string[] = [];
  page.on('requestfailed', (request) => {
    if (request.resourceType() === 'image') {
      failedImageUrls.push(request.url());
    }
  });
  page.on('response', (response) => {
    const req = response.request();
    if (req.resourceType() === 'image' && response.status() >= 400) {
      failedImageUrls.push(req.url());
    }
  });

  await page.goto('/books');

  // Both books appear in the grid.
  const bookGrid = page.getByTestId('book-grid');
  await expect(bookGrid).toBeVisible({ timeout: 10_000 });
  await expect(page.locator('[data-book-id="book-with-broken-cover"]')).toBeVisible();
  await expect(page.locator('[data-book-id="book-without-cover"]')).toBeVisible();

  // Top-bar import CTA appears when the library is non-empty (separate from
  // the empty-state CTA tested above).
  await expect(page.getByTestId('import-book-btn')).toBeVisible();

  // Each book card reserves a fixed-size cover slot (54x78). Both cards' slots
  // must occupy that reserved space — no layout collapse on the "broken cover"
  // book. We measure both via bounding boxes and assert dimensions are stable.
  for (const id of ['book-with-broken-cover', 'book-without-cover']) {
    const card = page.locator(`[data-book-id="${id}"]`);
    const coverSlot = card.locator('div[aria-hidden]').first();
    await expect(coverSlot).toBeVisible();
    const box = await coverSlot.boundingBox();
    expect(box, `cover slot for ${id} must have a layout box`).not.toBeNull();
    if (!box) throw new Error('unreachable');
    // Tailwind w-[54px] h-[78px] — assert the reserved dimensions are intact.
    expect(Math.round(box.width)).toBe(54);
    expect(Math.round(box.height)).toBe(78);
  }

  // The placeholder fallback is a div, not an <img>; the library renders no
  // <img> elements for covers. This is the "no layout break from a 404 cover
  // URL" guarantee: there is no image network request that could 404.
  // (BookCard intentionally renders an aria-hidden div, not <img src=cover_path>.)
  const cardCoverImages = page
    .locator('[data-book-id]')
    .locator('img');
  await expect(cardCoverImages).toHaveCount(0);

  // No broken-image network failures surfaced for cover URLs while rendering.
  expect(
    failedImageUrls.filter((u) => u.includes('/covers/')),
  ).toHaveLength(0);
});
