import type { Actor, JsonRecord } from './types';

export function json(body: JsonRecord, status = 200, headers?: HeadersInit): Response {
  const out = new Headers(headers);
  out.set('Content-Type', 'application/json; charset=utf-8');
  out.set('Cache-Control', 'no-store');
  out.set('X-Content-Type-Options', 'nosniff');
  return new Response(JSON.stringify(body), { status, headers: out });
}

export function error(code: string, status: number, detail?: string): Response {
  return json(detail ? { error: code, detail } : { error: code }, status);
}

export async function bodyObject(request: Request, limit = 16_384): Promise<JsonRecord> {
  const raw = await boundedText(request, limit);
  let parsed: unknown;
  try { parsed = JSON.parse(raw); } catch { throw new HttpError('invalid_json', 400); }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new HttpError('invalid_json_object', 400);
  return parsed as JsonRecord;
}

export async function boundedText(request: Request, limit: number): Promise<string> {
  const length = Number(request.headers.get('Content-Length'));
  if (Number.isFinite(length) && length > limit) throw new HttpError('body_too_large', 413);
  const reader = request.body?.getReader();
  if (!reader) return '';
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel();
        throw new HttpError('body_too_large', 413);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
}

export class HttpError extends Error {
  constructor(readonly code: string, readonly status: number) { super(code); }
}

export function actorFrom(request: Request): Actor | undefined {
  try {
    const value = request.headers.get('x-hub2-actor');
    if (!value || value.length > 2_048) return undefined;
    const parsed = JSON.parse(value) as Actor;
    if (!parsed || typeof parsed.id !== 'string' || !['operator', 'machine', 'agent'].includes(parsed.kind)
      || !Array.isArray(parsed.scopes) || !parsed.scopes.every(scope => typeof scope === 'string')
      || (parsed.resource_id !== undefined && typeof parsed.resource_id !== 'string')
      || (parsed.generation !== undefined && typeof parsed.generation !== 'string')) return undefined;
    return parsed;
  } catch { return undefined; }
}

export function hasScope(actor: Actor | undefined, scope: string): boolean {
  return !!actor && (actor.scopes.includes('*') || actor.scopes.includes(scope));
}

export function boundedString(value: unknown, max: number): value is string {
  return typeof value === 'string' && value.trim().length > 0 && new TextEncoder().encode(value).byteLength <= max;
}
