import { defineConfig } from 'vitest/config';
export default defineConfig({
  cacheDir: './.local/lifecycle-r2/vite-cache',
  test: {
    include: ['test/session-lifecycle.test.ts', 'test/session-lifecycle-r2.test.ts'],
    fileParallelism: false,
    maxWorkers: 1,
  },
});
