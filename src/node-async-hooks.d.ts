// Minimal typing for the nodejs_compat AsyncLocalStorage used by rows-meter.ts
// (no @types/node in this project).
declare module 'node:async_hooks' {
  export class AsyncLocalStorage<T> {
    getStore(): T | undefined;
    run<R>(store: T, callback: () => R): R;
  }
}
