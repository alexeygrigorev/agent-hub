import { describe, expect, it } from 'vitest';
import { isRecord } from '../src/is-record';

describe('isRecord', () => {
  it('accepts non-null non-array objects regardless of prototype', () => {
    expect(isRecord({ value: 1 })).toBe(true);
    expect(isRecord(new Date())).toBe(true);
    expect(isRecord(Object.create(null))).toBe(true);
  });

  it('rejects null, arrays, and primitives', () => {
    for (const value of [null, [], 'text', 1, true, undefined]) {
      expect(isRecord(value)).toBe(false);
    }
  });
});
