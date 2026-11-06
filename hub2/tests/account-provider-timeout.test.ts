import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AccountDO } from '../src/account-do';

function subject(fetcher: typeof fetch): any {
  const value = Object.create(AccountDO.prototype);
  value.env = { HUB2_PROVIDER_FETCH: fetcher };
  return value;
}

test('provider buffers a bounded non-OK body and clears its timeout after consumption', async () => {
  let signal: AbortSignal | undefined;
  const account = subject(async (_input: RequestInfo | URL, init?: RequestInit) => {
    signal = init?.signal as AbortSignal;
    return Response.json({ error: 'invalid_grant' }, { status: 401, headers: { 'x-provider': 'fixture' } });
  });
  const response = await account.provider('https://provider.test/token', {}, 20);
  assert.equal(response.status, 401);
  assert.equal(response.headers.get('x-provider'), 'fixture');
  assert.deepEqual(await account.boundedJson(response), { error: 'invalid_grant' });
  await new Promise(resolve => setTimeout(resolve, 35));
  assert.equal(signal?.aborted, false, 'completed response must not leave a timeout that aborts later');
});

test('provider timeout aborts while a response body is still streaming', async () => {
  let signal: AbortSignal | undefined;
  const account = subject(async (_input: RequestInfo | URL, init?: RequestInit) => {
    signal = init?.signal as AbortSignal;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        signal!.addEventListener('abort', () => controller.error(new DOMException('aborted', 'AbortError')), { once: true });
      },
    });
    return new Response(body);
  });
  await assert.rejects(account.provider('https://provider.test/slow', {}, 20));
  assert.equal(signal?.aborted, true);
});

test('provider cancels an oversized body and preserves the fail-closed size refusal', async () => {
  let cancelled = false;
  const account = subject(async () => new Response(new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new Uint8Array(131_073)); },
    cancel() { cancelled = true; },
  })));
  await assert.rejects(account.provider('https://provider.test/large', {}, 1000), /oversized_response/);
  assert.equal(cancelled, true);
});
