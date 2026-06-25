import { defineConfig } from 'vitest/config';
import path from 'node:path';

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
      // Stub out the virtual changelog module that requires a Vite plugin
      'virtual:changelog': path.resolve(__dirname, 'src/__mocks__/virtual-changelog.ts'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    // The integration-shaped tests (booksRoute, acceptance specs) cross
    // the 5s default under istanbul instrumentation now that coverage
    // measures all of src/**. Without the widened scope these completed
    // in ~1s. Bump the cap so coverage runs don't flake while still
    // catching genuine hangs.
    testTimeout: 15_000,
    coverage: {
      provider: 'istanbul',
      reporter: ['text', 'html', 'json-summary'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/**/__tests__/**',
        'src/**/__mocks__/**',
        'src/main.tsx',
        // Tauri-IPC-using components: verified by tauri-driver E2E (S18-S21)
        // and unit tests with @tauri-apps mocked; excluded from line-coverage
        // gate so the headline number doesn't conflate "tested via real
        // packaged app" with "tested via mocked unit layer". See
        // design.md §2.
        'src/components/DictateWindow/**',
        'src/components/AccessibilityGate/**',
        'src/components/InputMonitoringGate/**',
        'src/components/CapturesTab/**',
        'src/lib/hooks/useChordSync.ts',
        'src/lib/hooks/useCaptureRecordingSession.ts',
      ],
    },
  },
});
