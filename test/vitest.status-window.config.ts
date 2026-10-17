import { defineConfig } from 'vitest/config';
export default defineConfig({
  cacheDir: './.local/status-window/vite-cache',
  test: { include: ['test/status-window.test.ts'], fileParallelism: false, maxWorkers: 1, environment: 'node' },
});
