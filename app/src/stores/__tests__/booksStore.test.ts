import { beforeEach, describe, expect, it } from 'vitest';
import { useBooksStore } from '@/stores/booksStore';

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
});
