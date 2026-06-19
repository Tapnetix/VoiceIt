/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { BookImport } from '@/components/BooksTab/BookImport';
import { useBooksStore } from '@/stores/booksStore';

// ── Mocks ─────────────────────────────────────────────────────────────────────
//
// Only the data-fetching boundary (`useBooks` hooks) is mocked — those wrap
// network I/O via TanStack Query. Everything else is the real component +
// real zustand store, so assertions can hit observable outcomes (rendered
// text, store transitions) instead of internal call counts.

const mockImportMutate = vi.fn();
// The analyze mutation's onSuccess callback is what flips the books-store view,
// so the mock invokes it synchronously to let tests observe that transition.
const mockAnalyzeMutate = vi.fn(
  (
    _vars: unknown,
    opts?: { onSuccess?: (data: unknown, vars: unknown, ctx: unknown) => void },
  ) => {
    opts?.onSuccess?.(undefined, _vars, undefined);
  },
);

const importedBook = {
  id: 'b1',
  title: 'Silo 42',
  author: 'Zev Paiss',
  source_format: 'epub',
  status: 'imported' as const,
  chapters: new Array(23)
    .fill(0)
    .map((_, i) => ({
      id: `c${i}`,
      number: i + 1,
      title: `Ch ${i + 1}`,
      word_count: 100,
      generation_state: 'none' as const,
    })),
  chapter_count: 23,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
};

vi.mock('@/lib/hooks/useBooks', () => ({
  useImportBook: vi.fn(() => ({
    mutate: mockImportMutate,
    data: importedBook,
    isPending: false,
  })),
  useAnalyzeBook: vi.fn(() => ({ mutate: mockAnalyzeMutate, isPending: false })),
}));

import { useImportBook, useAnalyzeBook } from '@/lib/hooks/useBooks';

const wrap = (ui: React.ReactNode) => (
  <QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>
);

beforeEach(() => {
  // Reset the real zustand store between tests so view/selection assertions
  // are not polluted by neighbours.
  useBooksStore.getState().reset();
  mockImportMutate.mockClear();
  mockAnalyzeMutate.mockClear();
  mockAnalyzeMutate.mockImplementation((_vars, opts) => {
    opts?.onSuccess?.(undefined, _vars, undefined);
  });
});

// ── Tests: imported-book state ────────────────────────────────────────────────

describe('BookImport — after successful import', () => {
  it('shows detected title/author/chapter count after parse, and the options + analyze action', () => {
    render(wrap(<BookImport />));
    expect(screen.getByTestId('meta-title')).toHaveTextContent('Silo 42');
    expect(screen.getByTestId('meta-author')).toHaveTextContent('Zev Paiss');
    expect(screen.getByTestId('meta-chapters')).toHaveTextContent('23');
    expect(screen.getByTestId('model-select')).toBeInTheDocument();
    expect(screen.getByTestId('narrator-select')).toBeInTheDocument();
    expect(screen.getByTestId('analyze-btn')).toBeInTheDocument();
  });

  it('renders the book-metadata card', () => {
    render(wrap(<BookImport />));
    expect(screen.getByTestId('book-metadata')).toBeInTheDocument();
  });

  it('shows source_format badge', () => {
    render(wrap(<BookImport />));
    expect(screen.getByTestId('book-metadata')).toHaveTextContent(/epub/i);
  });
});

// ── Tests: dropzone ───────────────────────────────────────────────────────────

describe('BookImport — dropzone', () => {
  it('renders the file input with book-dropzone testid', () => {
    render(wrap(<BookImport />));
    expect(screen.getByTestId('book-dropzone')).toBeInTheDocument();
  });

  it('shows the PDF best-effort note', () => {
    render(wrap(<BookImport />));
    expect(screen.getByText(/pdf.*best.effort/i)).toBeInTheDocument();
  });
});

// ── Tests: pre-import state ───────────────────────────────────────────────────

describe('BookImport — before import', () => {
  beforeEach(() => {
    vi.mocked(useImportBook).mockReturnValue({
      mutate: mockImportMutate,
      data: undefined,
      isPending: false,
    } as unknown as ReturnType<typeof useImportBook>);
    vi.mocked(useAnalyzeBook).mockReturnValue({
      mutate: mockAnalyzeMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useAnalyzeBook>);
  });

  it('does not show the metadata card before a file is imported', () => {
    render(wrap(<BookImport />));
    expect(screen.queryByTestId('book-metadata')).not.toBeInTheDocument();
  });

  it('shows the dropzone but not analyze-btn before import', () => {
    render(wrap(<BookImport />));
    expect(screen.getByTestId('book-dropzone')).toBeInTheDocument();
    expect(screen.queryByTestId('analyze-btn')).not.toBeInTheDocument();
  });
});

// ── Tests: extension validation ───────────────────────────────────────────────

describe('BookImport — extension validation', () => {
  beforeEach(() => {
    vi.mocked(useImportBook).mockReturnValue({
      mutate: mockImportMutate,
      data: undefined,
      isPending: false,
    } as unknown as ReturnType<typeof useImportBook>);
    vi.mocked(useAnalyzeBook).mockReturnValue({
      mutate: mockAnalyzeMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useAnalyzeBook>);
  });

  it('shows inline error for unsupported file types', async () => {
    render(wrap(<BookImport />));
    const input = screen.getByTestId('book-dropzone');
    const badFile = new File(['data'], 'book.mobi', { type: 'application/octet-stream' });
    fireEvent.change(input, { target: { files: [badFile] } });
    await waitFor(() =>
      expect(screen.getByText(/unsupported/i)).toBeInTheDocument(),
    );
  });

  it('does not show error for valid .epub file', async () => {
    render(wrap(<BookImport />));
    const input = screen.getByTestId('book-dropzone');
    const validFile = new File(['PK...'], 'book.epub', { type: 'application/epub+zip' });
    fireEvent.change(input, { target: { files: [validFile] } });
    await waitFor(() =>
      expect(screen.queryByText(/unsupported/i)).not.toBeInTheDocument(),
    );
  });

  it('does not show error for valid .pdf file', async () => {
    render(wrap(<BookImport />));
    const input = screen.getByTestId('book-dropzone');
    const validFile = new File(['%PDF...'], 'book.pdf', { type: 'application/pdf' });
    fireEvent.change(input, { target: { files: [validFile] } });
    await waitFor(() =>
      expect(screen.queryByText(/unsupported/i)).not.toBeInTheDocument(),
    );
  });

  it('does not show error for valid .txt file', async () => {
    render(wrap(<BookImport />));
    const input = screen.getByTestId('book-dropzone');
    const validFile = new File(['hello world'], 'book.txt', { type: 'text/plain' });
    fireEvent.change(input, { target: { files: [validFile] } });
    await waitFor(() =>
      expect(screen.queryByText(/unsupported/i)).not.toBeInTheDocument(),
    );
  });

  it('does not show error for valid .fb2 file', async () => {
    render(wrap(<BookImport />));
    const input = screen.getByTestId('book-dropzone');
    const validFile = new File(['<FictionBook/>'], 'book.fb2', { type: 'application/x-fictionbook+xml' });
    fireEvent.change(input, { target: { files: [validFile] } });
    await waitFor(() =>
      expect(screen.queryByText(/unsupported/i)).not.toBeInTheDocument(),
    );
  });
});

// ── Tests: analyze action ─────────────────────────────────────────────────────

describe('BookImport — analyze action', () => {
  it('navigates to the analysis view for the imported book once analyze succeeds', () => {
    vi.mocked(useImportBook).mockReturnValue({
      mutate: mockImportMutate,
      data: importedBook,
      isPending: false,
    } as unknown as ReturnType<typeof useImportBook>);
    vi.mocked(useAnalyzeBook).mockReturnValue({
      mutate: mockAnalyzeMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useAnalyzeBook>);

    // Sanity-check starting state — confirms the post-click transition is
    // a real change, not a no-op.
    expect(useBooksStore.getState().view).toBe('library');
    expect(useBooksStore.getState().selectedBookId).toBeNull();

    render(wrap(<BookImport />));
    fireEvent.click(screen.getByTestId('analyze-btn'));

    expect(useBooksStore.getState().view).toBe('analysis');
    expect(useBooksStore.getState().selectedBookId).toBe('b1');
  });

  it('keeps the user on the import view when analysis is still pending', () => {
    vi.mocked(useImportBook).mockReturnValue({
      mutate: mockImportMutate,
      data: importedBook,
      isPending: false,
    } as unknown as ReturnType<typeof useImportBook>);
    // While pending, the mutation should not deliver onSuccess — the store
    // therefore stays put even if the user manages to fire the click handler.
    const pendingMutate = vi.fn();
    vi.mocked(useAnalyzeBook).mockReturnValue({
      mutate: pendingMutate,
      isPending: true,
    } as unknown as ReturnType<typeof useAnalyzeBook>);

    render(wrap(<BookImport />));
    const btn = screen.getByTestId('analyze-btn');
    expect(btn).toBeDisabled();
    expect(btn).toHaveTextContent(/analyzing/i);
    expect(useBooksStore.getState().view).toBe('library');
    expect(useBooksStore.getState().selectedBookId).toBeNull();
  });

  it('does not change the view when the imported book is missing', () => {
    vi.mocked(useImportBook).mockReturnValue({
      mutate: mockImportMutate,
      data: undefined,
      isPending: false,
    } as unknown as ReturnType<typeof useImportBook>);
    vi.mocked(useAnalyzeBook).mockReturnValue({
      mutate: mockAnalyzeMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useAnalyzeBook>);

    render(wrap(<BookImport />));
    // No analyze button rendered without a book, and the store stays neutral.
    expect(screen.queryByTestId('analyze-btn')).not.toBeInTheDocument();
    expect(useBooksStore.getState().view).toBe('library');
    expect(useBooksStore.getState().selectedBookId).toBeNull();
  });
});
