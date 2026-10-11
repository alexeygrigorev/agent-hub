export function base64url(buffer: ArrayBuffer | Uint8Array): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function decodeBase64url(str: string): Uint8Array {
  const base64 = str.replace(/-/g, '+').replace(/_/g, '/');
  const pad = base64.length % 4;
  const padded = pad ? base64 + '='.repeat(4 - pad) : base64;
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

export async function sha256(value: string | Uint8Array): Promise<string> {
  const data = typeof value === 'string' ? new TextEncoder().encode(value) : value;
  const hash = await crypto.subtle.digest('SHA-256', data);
  return base64url(hash);
}

export function randomBytes(count = 32): Uint8Array {
  const arr = new Uint8Array(count);
  crypto.getRandomValues(arr);
  return arr;
}

export function randomString(count = 32): string {
  return base64url(randomBytes(count));
}

// Generate 8-char human code without 0,O,1,I,L
export function generateUserCode(): string {
  const alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  let code = '';
  const random = randomBytes(8);
  for (let i = 0; i < 8; i++) {
    if (i === 4) code += '-';
    code += alphabet[random[i] % alphabet.length];
  }
  return code;
}

export async function signHmac(secret: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(data));
  return base64url(signature);
}

export async function verifyHmac(secret: string, data: string, signatureBase64url: string): Promise<boolean> {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['verify']
  );
  try {
    const signatureBytes = decodeBase64url(signatureBase64url);
    return await crypto.subtle.verify('HMAC', key, signatureBytes, new TextEncoder().encode(data));
  } catch {
    return false;
  }
}

export async function verifyRs256(jwk: JsonWebKey, payload: string, signatureBase64url: string): Promise<boolean> {
  try {
    const key = await crypto.subtle.importKey(
      'jwk',
      jwk,
      { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
      false,
      ['verify']
    );
    const signatureBytes = decodeBase64url(signatureBase64url);
    const payloadBytes = new TextEncoder().encode(payload);
    return await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, signatureBytes, payloadBytes);
  } catch (err) {
    return false;
  }
}
