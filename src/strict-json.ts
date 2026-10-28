// Strict JSON request helpers shared by the metrics and quota ingestion routes.

/**
 * Validates a strict UTC-aware ISO-8601 timestamp string (ending in Z or +00:00)
 * and normalizes it to canonical ISO-8601 UTC format (.toISOString()).
 * Rejects empty strings, naive timestamps without timezone, non-UTC offsets, or invalid calendar dates.
 */
export function parseUtcIso(raw: unknown): { iso: string; ms: number } | null {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > 64) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|\+00:00)$/.exec(raw);
  if (!match) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);

  if (month < 1 || month > 12) return null;
  if (day < 1 || day > 31) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;

  const ms = Date.parse(raw);
  if (!Number.isFinite(ms)) return null;

  const dt = new Date(ms);
  if (
    dt.getUTCFullYear() !== year ||
    dt.getUTCMonth() + 1 !== month ||
    dt.getUTCDate() !== day ||
    dt.getUTCHours() !== hour ||
    dt.getUTCMinutes() !== minute ||
    dt.getUTCSeconds() !== second
  ) {
    return null;
  }

  return { iso: dt.toISOString(), ms };
}

export function isPlainObject(val: unknown): val is Record<string, any> {
  if (typeof val !== "object" || val === null || Array.isArray(val)) return false;
  const proto = Object.getPrototypeOf(val);
  return proto === Object.prototype || proto === null;
}

export async function readBoundedJsonBody(
  request: Request,
  maxBytes: number
): Promise<{ data?: Record<string, any>; errorStatus?: number; errorCode?: string }> {
  const contentLengthHeader = request.headers.get("content-length");
  if (contentLengthHeader !== null && contentLengthHeader.trim() !== "") {
    const declared = Number(contentLengthHeader);
    if (!Number.isFinite(declared) || declared < 0) {
      return { errorStatus: 400, errorCode: "invalid_content_length" };
    }
    if (declared > maxBytes) {
      return { errorStatus: 413, errorCode: "body_too_large" };
    }
  }

  if (!request.body) {
    return { errorStatus: 400, errorCode: "missing_body" };
  }

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;

  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      if (part.value) {
        totalBytes += part.value.byteLength;
        if (totalBytes > maxBytes) {
          await reader.cancel();
          return { errorStatus: 413, errorCode: "body_too_large" };
        }
        chunks.push(part.value);
      }
    }
  } catch {
    return { errorStatus: 400, errorCode: "stream_read_error" };
  } finally {
    try {
      reader.releaseLock();
    } catch {}
  }

  if (totalBytes === 0) {
    return { errorStatus: 400, errorCode: "missing_body" };
  }

  const allBytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    allBytes.set(chunk, offset);
    offset += chunk.byteLength;
  }

  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(allBytes);
  } catch {
    return { errorStatus: 400, errorCode: "invalid_utf8" };
  }

  // Guard against raw __proto__ key in JSON text before parse
  if (/"__proto__"\s*:/.test(text)) {
    return { errorStatus: 400, errorCode: "unknown_field" };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { errorStatus: 400, errorCode: "invalid_json" };
  }

  if (!isPlainObject(parsed)) {
    return { errorStatus: 400, errorCode: "invalid_json_object" };
  }

  return { data: parsed };
}
