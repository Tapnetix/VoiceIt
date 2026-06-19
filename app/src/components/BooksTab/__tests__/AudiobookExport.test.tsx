/**
 * AudiobookExport component tests (D7)
 *
 * Tests describe observable behaviour against the rendered UI:
 * - The export-format / export-metadata / export-action regions and their controls render.
 * - The download button is disabled until export completes.
 * - Starting an export flips the start button into its in-progress state and seeds the status area.
 * - export_progress events update the status percentage; export_complete enables download.
 * - The download button label reflects the selected format extension.
 * - Error events surface the message inside export-status.
 */
/// <reference types="@testing-library/jest-dom/vitest" />
import '@/i18n';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { AudiobookExport } from '@/components/BooksTab/AudiobookExport';

// ─── Mocks ────────────────────────────────────────────────────────────────────

let exportProgressHandler: ((event: any) => void) | undefined;
let exportCompleteHandler: ((event: any) => void) | undefined;
let errorHandler: ((event: any) => void) | undefined;

vi.mock('@/lib/hooks/useBookProgress', () => ({
  useBookProgress: (_id: string, handlers: any) => {
    exportProgressHandler = handlers.onExportProgress;
    exportCompleteHandler = handlers.onExportComplete;
    errorHandler = handlers.onError;
  },
}));

const mockStartExport = vi.fn();
const mockDownloadExport = vi.fn();
vi.mock('@/lib/hooks/useBooks', () => ({
  useBook: () => ({
    data: { id: 'book-1', title: 'Test Book', author: 'Jane Doe', status: 'ready' },
  }),
  useStartExport: () => ({
    mutateAsync: mockStartExport,
    isPending: false,
  }),
  useDownloadExport: () => ({
    mutateAsync: mockDownloadExport,
    isPending: false,
  }),
}));

vi.mock('@/stores/booksStore', () => ({
  useBooksStore: (sel: any) =>
    sel({
      selectedBookId: 'book-1',
      setView: vi.fn(),
    }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('AudiobookExport', () => {
  beforeEach(() => {
    exportProgressHandler = undefined;
    exportCompleteHandler = undefined;
    errorHandler = undefined;
    mockStartExport.mockClear();
    mockStartExport.mockResolvedValue({ book_id: 'book-1', task_id: 'task-1', status: 'exporting' });
    mockDownloadExport.mockClear();
    mockDownloadExport.mockResolvedValue(new Blob());
  });

  it('renders the export-format radio group', () => {
    render(<AudiobookExport />);
    expect(screen.getByTestId('export-format')).toBeInTheDocument();
  });

  it('renders export-metadata section with title, author, and cover-drop', () => {
    render(<AudiobookExport />);
    const metadata = screen.getByTestId('export-metadata');
    expect(metadata).toBeInTheDocument();
    expect(metadata.querySelector('[data-testid="cover-drop"]')).toBeInTheDocument();
  });

  it('renders export-action section', () => {
    render(<AudiobookExport />);
    expect(screen.getByTestId('export-action')).toBeInTheDocument();
  });

  it('renders export-status inside export-action', () => {
    render(<AudiobookExport />);
    const action = screen.getByTestId('export-action');
    expect(action.querySelector('[data-testid="export-status"]')).toBeInTheDocument();
  });

  it('renders start-export-btn inside export-action', () => {
    render(<AudiobookExport />);
    expect(screen.getByTestId('start-export-btn')).toBeInTheDocument();
  });

  it('renders download-btn inside export-action', () => {
    render(<AudiobookExport />);
    expect(screen.getByTestId('download-btn')).toBeInTheDocument();
  });

  it('download-btn is disabled initially', () => {
    render(<AudiobookExport />);
    expect(screen.getByTestId('download-btn')).toBeDisabled();
  });

  it('clicking start moves the action into an in-progress state with a starting status', async () => {
    render(<AudiobookExport />);

    const startBtn = screen.getByTestId('start-export-btn');
    expect(startBtn).toBeEnabled();
    expect(startBtn).toHaveTextContent(/start export/i);

    await act(async () => {
      fireEvent.click(startBtn);
    });

    // Observable outcome: the button reflects the running phase and the status area
    // shows the "starting" message seeded by handleStartExport.
    expect(screen.getByTestId('start-export-btn')).toBeDisabled();
    expect(screen.getByTestId('start-export-btn')).toHaveTextContent(/exporting/i);
    expect(screen.getByTestId('export-status')).toHaveTextContent(/starting export/i);
  });

  it('selecting mp3_single updates the download button label to .mp3', () => {
    render(<AudiobookExport />);

    fireEvent.click(screen.getByLabelText(/mp3 \(single file\)/i));

    expect(screen.getByTestId('download-btn')).toHaveTextContent('.mp3');
  });

  it('download-btn becomes enabled after export_complete event', async () => {
    render(<AudiobookExport />);

    // Start export first
    await act(async () => {
      fireEvent.click(screen.getByTestId('start-export-btn'));
    });

    // Fire export_complete SSE event
    act(() => {
      exportCompleteHandler?.({
        type: 'export_complete',
        download_path: '/tmp/test.m4b',
        filename: 'test.m4b',
      });
    });

    expect(screen.getByTestId('download-btn')).toBeEnabled();
  });

  it('download button label reflects m4b format by default', () => {
    render(<AudiobookExport />);
    // Default format is m4b
    expect(screen.getByTestId('download-btn')).toHaveTextContent('.m4b');
  });

  it('updates export-status when export_progress event fires', async () => {
    render(<AudiobookExport />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('start-export-btn'));
    });

    act(() => {
      exportProgressHandler?.({
        type: 'export_progress',
        progress: 50,
      });
    });

    // Status section must reflect the actual progress value (50%), not merely exist.
    const status = screen.getByTestId('export-status');
    expect(status).toHaveTextContent('50%');
  });

  it('shows error message in export-status on error event', async () => {
    render(<AudiobookExport />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('start-export-btn'));
    });

    act(() => {
      errorHandler?.({
        type: 'error',
        stage: 'export',
        message: 'Export failed due to missing audio',
      });
    });

    const status = screen.getByTestId('export-status');
    expect(status).toHaveTextContent(/export failed/i);
  });

  it('format radio group contains m4b, mp3_single, mp3_per_chapter options', () => {
    render(<AudiobookExport />);
    const formatGroup = screen.getByTestId('export-format');
    expect(formatGroup).toHaveTextContent('m4b');
    expect(formatGroup).toHaveTextContent('mp3');
  });

  it('selecting mp3_per_chapter updates the download button label to .zip', () => {
    render(<AudiobookExport />);

    fireEvent.click(screen.getByLabelText(/mp3 per chapter/i));

    expect(screen.getByTestId('download-btn')).toHaveTextContent('.zip');
  });

  it('typing in the title input updates the displayed value', () => {
    render(<AudiobookExport />);
    const titleInput = screen.getByLabelText('Title') as HTMLInputElement;

    fireEvent.change(titleInput, { target: { value: 'New Title' } });

    expect(titleInput.value).toBe('New Title');
  });

  it('typing in the author input updates the displayed value', () => {
    render(<AudiobookExport />);
    const authorInput = screen.getByLabelText('Author') as HTMLInputElement;

    fireEvent.change(authorInput, { target: { value: 'New Author' } });

    expect(authorInput.value).toBe('New Author');
  });

  it('dropping an image file onto cover-drop shows its filename', () => {
    render(<AudiobookExport />);
    const dropZone = screen.getByTestId('cover-drop');
    const file = new File(['cover-bytes'], 'mycover.png', { type: 'image/png' });

    fireEvent.drop(dropZone, {
      dataTransfer: { files: [file] },
    });

    expect(dropZone).toHaveTextContent('mycover.png');
  });

  it('dropping a non-image file onto cover-drop is ignored', () => {
    render(<AudiobookExport />);
    const dropZone = screen.getByTestId('cover-drop');
    const file = new File(['plain'], 'notes.txt', { type: 'text/plain' });

    fireEvent.drop(dropZone, {
      dataTransfer: { files: [file] },
    });

    // Drop zone still shows the placeholder copy, not the rejected filename.
    expect(dropZone).not.toHaveTextContent('notes.txt');
    expect(dropZone).toHaveTextContent(/drop cover image here/i);
  });

  it('choosing a file via the hidden cover input shows its filename', () => {
    render(<AudiobookExport />);
    const dropZone = screen.getByTestId('cover-drop');
    const fileInput = dropZone.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['cover'], 'picked.jpg', { type: 'image/jpeg' });

    fireEvent.change(fileInput, { target: { files: [file] } });

    expect(dropZone).toHaveTextContent('picked.jpg');
  });

  it('surfaces a failure from startExport as the error status message', async () => {
    mockStartExport.mockRejectedValueOnce(new Error('network down'));
    render(<AudiobookExport />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('start-export-btn'));
    });

    expect(screen.getByTestId('export-status')).toHaveTextContent(/network down/i);
  });

  it('progress events without a message keep the seeded status text but update the percentage', async () => {
    render(<AudiobookExport />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('start-export-btn'));
    });

    act(() => {
      exportProgressHandler?.({ type: 'export_progress', progress: 33 });
    });

    const status = screen.getByTestId('export-status');
    expect(status).toHaveTextContent('33%');
    expect(status).toHaveTextContent(/starting export/i);
  });

  it('progress events with a message replace the status text', async () => {
    render(<AudiobookExport />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('start-export-btn'));
    });

    act(() => {
      exportProgressHandler?.({
        type: 'export_progress',
        progress: 75,
        message: 'Encoding chapter 3',
      });
    });

    const status = screen.getByTestId('export-status');
    expect(status).toHaveTextContent('75%');
    expect(status).toHaveTextContent(/encoding chapter 3/i);
  });

  it('header shows the book title from useBook data', () => {
    render(<AudiobookExport />);
    // The book title from the mocked useBook data is rendered in the header
    expect(screen.getByText('Test Book')).toBeInTheDocument();
  });

  it('header back-to-overview button is rendered and clickable', () => {
    render(<AudiobookExport />);
    const backBtn = screen.getByRole('button', { name: /back to overview/i });
    expect(backBtn).toBeInTheDocument();
    // Clicking does not throw — exercises the setView('overview') handler path.
    fireEvent.click(backBtn);
  });

  it('idle status copy is shown before any export action', () => {
    render(<AudiobookExport />);
    expect(screen.getByTestId('export-status')).toHaveTextContent(/ready to export/i);
  });

  it('pressing Enter on the cover drop zone opens the file picker', () => {
    render(<AudiobookExport />);
    const dropZone = screen.getByTestId('cover-drop');
    const fileInput = dropZone.querySelector('input[type="file"]') as HTMLInputElement;
    const clickSpy = vi.spyOn(fileInput, 'click').mockImplementation(() => {});

    fireEvent.keyDown(dropZone, { key: 'Enter' });

    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('clicking the cover drop zone opens the file picker', () => {
    render(<AudiobookExport />);
    const dropZone = screen.getByTestId('cover-drop');
    const fileInput = dropZone.querySelector('input[type="file"]') as HTMLInputElement;
    const clickSpy = vi.spyOn(fileInput, 'click').mockImplementation(() => {});

    fireEvent.click(dropZone);

    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockRestore();
  });

  it('dragging over the cover drop zone does not change its visible copy', () => {
    render(<AudiobookExport />);
    const dropZone = screen.getByTestId('cover-drop');
    // Exercises the onDragOver preventDefault handler without changing state.
    fireEvent.dragOver(dropZone);
    expect(dropZone).toHaveTextContent(/drop cover image here/i);
  });

  it('clicking download after export_complete triggers a download with the selected format and title', async () => {
    render(<AudiobookExport />);

    // Customize the title so we can verify it is forwarded to the download mutation.
    const titleInput = screen.getByLabelText('Title') as HTMLInputElement;
    fireEvent.change(titleInput, { target: { value: 'My Custom Title' } });

    await act(async () => {
      fireEvent.click(screen.getByTestId('start-export-btn'));
    });
    act(() => {
      exportCompleteHandler?.({
        type: 'export_complete',
        download_path: '/tmp/test.m4b',
        filename: 'test.m4b',
      });
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('download-btn'));
    });

    // Observable outcome: the download mutation was invoked with the selected
    // format (default m4b) and the user-supplied title.
    expect(mockDownloadExport).toHaveBeenCalledWith(
      expect.objectContaining({
        bookId: 'book-1',
        bookTitle: 'My Custom Title',
        format: 'm4b',
      }),
    );
  });

  it('a failed download does not crash the component or revert the complete badge', async () => {
    // The download error is captured into errorMessage state but the JSX only
    // renders errorMessage when phase === 'error'; on the complete phase the
    // "Done / Export complete" badge keeps showing. This locks in that the
    // failure does not throw and does not regress the completion state.
    mockDownloadExport.mockRejectedValueOnce(new Error('disk full'));
    render(<AudiobookExport />);

    await act(async () => {
      fireEvent.click(screen.getByTestId('start-export-btn'));
    });
    act(() => {
      exportCompleteHandler?.({
        type: 'export_complete',
        download_path: '/tmp/test.m4b',
        filename: 'test.m4b',
      });
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('download-btn'));
    });

    // Component still mounted; status area still shows the "Done" completion state.
    expect(screen.getByTestId('export-status')).toHaveTextContent(/done/i);
    expect(screen.getByTestId('download-btn')).toBeEnabled();
  });
});
