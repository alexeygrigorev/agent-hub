import { describe, expect, it } from 'vitest';
import { mcpConsentHeaders, UI_HEADERS } from '../src/ui-pages';

describe('mcpConsentHeaders', () => {
  it('uses same-origin Referrer-Policy (not no-referrer)', () => {
    expect(UI_HEADERS['Referrer-Policy']).toBe('same-origin');
    expect(mcpConsentHeaders('http://127.0.0.1:8787/callback')['Referrer-Policy']).toBe('same-origin');
  });
  it('adds custom callback schemes to form-action for Cursor-style redirects', () => {
    const csp = mcpConsentHeaders('cursor://anysphere.cursor-mcp/oauth/callback')['Content-Security-Policy'];
    expect(csp).toContain("form-action 'self' https: http: cursor:");
  });
  it('keeps http(s) form-action for network redirects', () => {
    expect(mcpConsentHeaders('https://example.com/cb')['Content-Security-Policy']).toContain("form-action 'self' https: http:");
    expect(mcpConsentHeaders('https://example.com/cb')['Content-Security-Policy']).not.toContain('cursor:');
  });
});
