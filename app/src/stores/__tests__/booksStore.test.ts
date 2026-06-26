import { beforeEach, describe, expect, it } from 'vitest';
import { useBooksStore, type BooksView } from '@/stores/booksStore';

beforeEach(() => useBooksStore.getState().reset());

describe('booksStore', () => {
  it('defaults to the library view with no selection', () => {
    const s = useBooksStore.getState();
    expect(s.view).toBe('library');
    expect(s.selectedBookId).toBeNull();
  });

  it('selects a book and switches view', () => {
    useBooksStore.getState().setSelectedBookId('b1');
    useBooksStore.getState().setView('overview');
    expect(useBooksStore.getState().selectedBookId).toBe('b1');
    expect(useBooksStore.getState().view).toBe('overview');
  });

  it('tracks the current read-along line', () => {
    useBooksStore.getState().setReadAlong(true);
    useBooksStore.getState().setCurrentSpokenSegment('seg-12');
    const s = useBooksStore.getState();
    expect(s.readAlongPlaying).toBe(true);
    expect(s.currentSpokenSegmentId).toBe('seg-12');
  });

  it('selects a chapter independently of the book selection', () => {
    useBooksStore.getState().setSelectedChapterId('ch-7');
    expect(useBooksStore.getState().selectedChapterId).toBe('ch-7');
    useBooksStore.getState().setSelectedChapterId(null);
    expect(useBooksStore.getState().selectedChapterId).toBeNull();
  });

  it('selects a segment independently of chapter and book selection', () => {
    useBooksStore.getState().setSelectedSegmentId('seg-42');
    expect(useBooksStore.getState().selectedSegmentId).toBe('seg-42');
    useBooksStore.getState().setSelectedSegmentId(null);
    expect(useBooksStore.getState().selectedSegmentId).toBeNull();
  });

  it('selects a character independently of other selections', () => {
    useBooksStore.getState().setSelectedCharacterId('char-narrator');
    expect(useBooksStore.getState().selectedCharacterId).toBe('char-narrator');
    useBooksStore.getState().setSelectedCharacterId(null);
    expect(useBooksStore.getState().selectedCharacterId).toBeNull();
  });

  it('reset clears chapter, segment, character ids and read-along state', () => {
    const s = useBooksStore.getState();
    s.setSelectedBookId('b1');
    s.setSelectedChapterId('ch-1');
    s.setSelectedSegmentId('seg-1');
    s.setSelectedCharacterId('char-1');
    s.setReadAlong(true);
    s.setCurrentSpokenSegment('seg-now');
    s.setView('voice-editor');

    useBooksStore.getState().reset();

    const after = useBooksStore.getState();
    expect(after.view).toBe('library');
    expect(after.selectedBookId).toBeNull();
    expect(after.selectedChapterId).toBeNull();
    expect(after.selectedSegmentId).toBeNull();
    expect(after.selectedCharacterId).toBeNull();
    expect(after.readAlongPlaying).toBe(false);
    expect(after.currentSpokenSegmentId).toBeNull();
  });

  it('reset returns to the library with cleared selection', () => {
    useBooksStore.getState().setSelectedBookId('b1');
    useBooksStore.getState().setView('chapter-editor');
    useBooksStore.getState().reset();
    expect(useBooksStore.getState().view).toBe('library');
    expect(useBooksStore.getState().selectedBookId).toBeNull();
  });

  it('reset restores every documented default after a fully corrupted setState replace', () => {
    // Corrupt every documented data slot via partial merge (simulating a
    // malformed persisted-state rehydration that overlays garbage onto the
    // store). The reset action remains accessible because it lives on the slice.
    useBooksStore.setState({
      view: 'not-a-real-view' as unknown as BooksView,
      selectedBookId: 99 as unknown as string,
      selectedChapterId: { broken: true } as unknown as string,
      selectedSegmentId: [] as unknown as string,
      selectedCharacterId: NaN as unknown as string,
      readAlongPlaying: 'yes' as unknown as boolean,
      currentSpokenSegmentId: false as unknown as string,
    });

    useBooksStore.getState().reset();

    const after = useBooksStore.getState();
    expect(after.view).toBe('library');
    expect(after.selectedBookId).toBeNull();
    expect(after.selectedChapterId).toBeNull();
    expect(after.selectedSegmentId).toBeNull();
    expect(after.selectedCharacterId).toBeNull();
    expect(after.readAlongPlaying).toBe(false);
    expect(after.currentSpokenSegmentId).toBeNull();
  });

  it('remains queryable after a partial malformed merge with the wrong field type', () => {
    // Simulate a corrupted hydration payload merging a wrong-typed field.
    useBooksStore.setState({
      selectedBookId: 42 as unknown as string,
    });

    // getState() itself must not throw, and unrelated documented slots must be intact.
    const s = useBooksStore.getState();
    expect(s.view).toBe('library');
    expect(s.selectedChapterId).toBeNull();
    expect(s.selectedSegmentId).toBeNull();
    expect(s.selectedCharacterId).toBeNull();
    expect(s.readAlongPlaying).toBe(false);
    expect(s.currentSpokenSegmentId).toBeNull();
    // The malformed value is observable (zustand does not validate), proving the
    // store stayed queryable even with a wrong-typed slot.
    expect(s.selectedBookId).toBe(42);
  });

  it('setters operate normally after reset clears a previously malformed state', () => {
    // Corrupt the store first via partial merge (mirrors a botched rehydration).
    useBooksStore.setState({
      view: 12345 as unknown as BooksView,
      selectedBookId: { id: 'wrong' } as unknown as string,
      selectedChapterId: true as unknown as string,
      selectedSegmentId: 0 as unknown as string,
      selectedCharacterId: [] as unknown as string,
      readAlongPlaying: 'maybe' as unknown as boolean,
      currentSpokenSegmentId: 7 as unknown as string,
    });

    useBooksStore.getState().reset();

    // Every setter should still behave normally end-to-end.
    useBooksStore.getState().setView('voice-editor');
    useBooksStore.getState().setSelectedBookId('book-7');
    useBooksStore.getState().setSelectedChapterId('chap-3');
    useBooksStore.getState().setSelectedSegmentId('seg-9');
    useBooksStore.getState().setSelectedCharacterId('char-x');
    useBooksStore.getState().setReadAlong(true);
    useBooksStore.getState().setCurrentSpokenSegment('seg-now');

    const after = useBooksStore.getState();
    expect(after.view).toBe('voice-editor');
    expect(after.selectedBookId).toBe('book-7');
    expect(after.selectedChapterId).toBe('chap-3');
    expect(after.selectedSegmentId).toBe('seg-9');
    expect(after.selectedCharacterId).toBe('char-x');
    expect(after.readAlongPlaying).toBe(true);
    expect(after.currentSpokenSegmentId).toBe('seg-now');
  });
});
