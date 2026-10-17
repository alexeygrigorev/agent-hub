import { defineConfig } from 'vitest/config';
export default defineConfig({
  cacheDir: './.local/lifecycle/vite-cache',
  test: { include: ['test/session-lifecycle.test.ts'], fileParallelism: false, maxWorkers: 1 },
});
