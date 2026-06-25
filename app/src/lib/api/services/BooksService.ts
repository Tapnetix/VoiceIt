/* generated using openapi-typescript-codegen -- do not edit */
/* istanbul ignore file */
/* tslint:disable */
/* eslint-disable */
import type { AnalyzeRequest } from '../models/AnalyzeRequest';
import type { AnalyzeResponse } from '../models/AnalyzeResponse';
import type { Body_import_book_books_import_post } from '../models/Body_import_book_books_import_post';
import type { BookDetailResponse } from '../models/BookDetailResponse';
import type { BookResponse } from '../models/BookResponse';
import type { ExportRequest } from '../models/ExportRequest';
import type { ExportResponse } from '../models/ExportResponse';
import type { GenerateRequest } from '../models/GenerateRequest';
import type { GenerateResponse } from '../models/GenerateResponse';
import type { GenerationStatusResponse } from '../models/GenerationStatusResponse';
import type { PreviewResponse } from '../models/PreviewResponse';
import type { RegenerateRequest } from '../models/RegenerateRequest';
import type { RegenerateResponse } from '../models/RegenerateResponse';
import type { CancelablePromise } from '../core/CancelablePromise';
import { OpenAPI } from '../core/OpenAPI';
import { request as __request } from '../core/request';
export class BooksService {
    /**
     * Analyze Book
     * Enqueue literary analysis + voice casting for a book.
     *
     * - Returns **202** immediately with ``status="analyzing"`` and a ``task_id``.
     * - Returns **404** if the book does not exist.
     * - Returns **409** if the book is already being analyzed or generating audio.
     * @returns AnalyzeResponse Successful Response
     * @throws ApiError
     */
    public static analyzeBookBooksBookIdAnalyzePost({
        bookId,
        requestBody,
    }: {
        bookId: string,
        requestBody?: AnalyzeRequest,
    }): CancelablePromise<AnalyzeResponse> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/books/{book_id}/analyze',
            path: {
                'book_id': bookId,
            },
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Generate Chapter
     * Lazily materialise and enqueue audio for all unrendered segments in a chapter.
     *
     * - Returns **202** immediately with ``queued_segments``.
     * - Returns **404** if the book or chapter does not exist.
     * - Returns **409** if the book is already generating.
     *
     * The 409 guard, status flip, and drain-reset are all handled inside
     * ``enqueue_chapter_generation`` so the lifecycle lives in one place.
     * @returns GenerateResponse Successful Response
     * @throws ApiError
     */
    public static generateChapterBooksBookIdChaptersChapterIdGeneratePost({
        bookId,
        chapterId,
        requestBody,
    }: {
        bookId: string,
        chapterId: string,
        requestBody?: GenerateRequest,
    }): CancelablePromise<GenerateResponse> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/books/{book_id}/chapters/{chapter_id}/generate',
            path: {
                'book_id': bookId,
                'chapter_id': chapterId,
            },
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Generate Book
     * Render the whole book (all chapters).
     *
     * - Returns **202** immediately with ``queued_segments`` (sum across all chapters).
     * - Returns **404** if the book does not exist.
     * - Returns **409** if the book is already generating.
     *
     * The 409 guard, status flip, and drain-reset are all handled inside
     * ``enqueue_book_generation`` so the lifecycle lives in one place.
     * @returns GenerateResponse Successful Response
     * @throws ApiError
     */
    public static generateBookBooksBookIdGeneratePost({
        bookId,
        requestBody,
    }: {
        bookId: string,
        requestBody?: GenerateRequest,
    }): CancelablePromise<GenerateResponse> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/books/{book_id}/generate',
            path: {
                'book_id': bookId,
            },
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Get Generation Status
     * Return per-chapter generation counts and overall progress.
     *
     * Uses B5's ``chapter_generation_state`` rollup for the ``state`` field.
     * @returns GenerationStatusResponse Successful Response
     * @throws ApiError
     */
    public static getGenerationStatusBooksBookIdGenerationStatusGet({
        bookId,
    }: {
        bookId: string,
    }): CancelablePromise<GenerationStatusResponse> {
        return __request(OpenAPI, {
            method: 'GET',
            url: '/books/{book_id}/generation-status',
            path: {
                'book_id': bookId,
            },
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Regenerate Segment
     * Re-render a single BookSegment as a new GenerationVersion.
     *
     * Creates a new ``GenerationVersion`` on the segment's existing ``Generation``
     * — does NOT create a new Generation, StoryItem, or touch any sibling segment.
     *
     * - Returns **202** immediately with ``{segment_id, generation_id, version_id, status}``.
     * - Returns **404** if the segment does not exist.
     * - Returns **409** if the book is currently generating.
     *
     * The optional body may override ``emotion``, ``instruct``, and ``seed`` for
     * this take; when omitted the segment's current settings are recomposed via
     * ``compose_instruct(segment)``.
     * @returns RegenerateResponse Successful Response
     * @throws ApiError
     */
    public static regenerateSegmentSegmentsSegmentIdRegeneratePost({
        segmentId,
        requestBody,
    }: {
        segmentId: string,
        requestBody?: RegenerateRequest,
    }): CancelablePromise<RegenerateResponse> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/segments/{segment_id}/regenerate',
            path: {
                'segment_id': segmentId,
            },
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Preview Segment
     * Synthesize a short preview clip for a segment — non-destructive.
     *
     * Unlike ``/regenerate``, this endpoint does **not** create a new
     * ``GenerationVersion``, does **not** promote any version to default, and
     * does **not** change ``BookSegment.audio_status``.  It is safe to call
     * during emotion-preview UX flows (D4) without disturbing the stored take.
     *
     * - Returns **202** with ``{generation_id, audio_path}`` pointing at the
     * temporary preview audio.
     * - Returns **404** if the segment does not exist.
     * - Returns **409** if the book is currently generating.
     *
     * The optional body accepts ``emotion`` and ``instruct`` overrides.
     * ``seed`` is ignored for previews (variation is intentional).
     * @returns PreviewResponse Successful Response
     * @throws ApiError
     */
    public static previewSegmentSegmentsSegmentIdPreviewPost({
        segmentId,
        requestBody,
    }: {
        segmentId: string,
        requestBody?: RegenerateRequest,
    }): CancelablePromise<PreviewResponse> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/segments/{segment_id}/preview',
            path: {
                'segment_id': segmentId,
            },
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Start Export
     * Enqueue audiobook export for a book.
     *
     * - Returns **202** immediately with ``status="exporting"`` and a ``task_id``.
     * - Returns **404** if the book does not exist.
     * - Returns **409** if the book is currently ``generating``.
     * - Returns **422** if no audio has been rendered yet (zero completed segments),
     * or if the format is invalid.
     * @returns ExportResponse Successful Response
     * @throws ApiError
     */
    public static startExportBooksBookIdExportPost({
        bookId,
        requestBody,
    }: {
        bookId: string,
        requestBody: ExportRequest,
    }): CancelablePromise<ExportResponse> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/books/{book_id}/export',
            path: {
                'book_id': bookId,
            },
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Download Export
     * Download the exported audiobook file.
     *
     * - Returns the file with correct ``Content-Type`` and ``Content-Disposition`` headers.
     * - Returns **404** if the book does not exist or if export has not completed yet.
     * @returns any Successful Response
     * @throws ApiError
     */
    public static downloadExportBooksBookIdExportDownloadGet({
        bookId,
    }: {
        bookId: string,
    }): CancelablePromise<any> {
        return __request(OpenAPI, {
            method: 'GET',
            url: '/books/{book_id}/export/download',
            path: {
                'book_id': bookId,
            },
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Import Book
     * Upload an ebook, parse it, create Book + Chapters, return BookDetailResponse.
     * @returns BookDetailResponse Successful Response
     * @throws ApiError
     */
    public static importBookBooksImportPost({
        formData,
    }: {
        formData: Body_import_book_books_import_post,
    }): CancelablePromise<BookDetailResponse> {
        return __request(OpenAPI, {
            method: 'POST',
            url: '/books/import',
            formData: formData,
            mediaType: 'multipart/form-data',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * List Books
     * List all books, newest first.
     * @returns BookResponse Successful Response
     * @throws ApiError
     */
    public static listBooksBooksGet(): CancelablePromise<Array<BookResponse>> {
        return __request(OpenAPI, {
            method: 'GET',
            url: '/books',
        });
    }
    /**
     * Get Book
     * Return book detail (metadata + chapters).
     * @returns BookDetailResponse Successful Response
     * @throws ApiError
     */
    public static getBookBooksBookIdGet({
        bookId,
    }: {
        bookId: string,
    }): CancelablePromise<BookDetailResponse> {
        return __request(OpenAPI, {
            method: 'GET',
            url: '/books/{book_id}',
            path: {
                'book_id': bookId,
            },
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Patch Book
     * Update book metadata (title, author, cover_path).
     * @returns BookDetailResponse Successful Response
     * @throws ApiError
     */
    public static patchBookBooksBookIdPatch({
        bookId,
        requestBody,
    }: {
        bookId: string,
        requestBody: Record<string, any>,
    }): CancelablePromise<BookDetailResponse> {
        return __request(OpenAPI, {
            method: 'PATCH',
            url: '/books/{book_id}',
            path: {
                'book_id': bookId,
            },
            body: requestBody,
            mediaType: 'application/json',
            errors: {
                422: `Validation Error`,
            },
        });
    }
    /**
     * Delete Book
     * Delete a book and all its related rows (cascade).
     * @returns string Successful Response
     * @throws ApiError
     */
    public static deleteBookBooksBookIdDelete({
        bookId,
    }: {
        bookId: string,
    }): CancelablePromise<Record<string, string>> {
        return __request(OpenAPI, {
            method: 'DELETE',
            url: '/books/{book_id}',
            path: {
                'book_id': bookId,
            },
            errors: {
                422: `Validation Error`,
            },
        });
    }
}
