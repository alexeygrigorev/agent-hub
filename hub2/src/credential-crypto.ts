/** Byte-level AES-GCM only. Each caller owns its wire format and contextual AAD. */
export function credentialKey(raw: string): Promise<CryptoKey> {
  if (!/^[0-9a-f]{64}$/i.test(raw)) throw new Error('invalid_credential_key');
  const bytes = new Uint8Array(raw.match(/../g)!.map(v => parseInt(v, 16)));
  return crypto.subtle.importKey('raw', bytes, 'AES-GCM', false, ['encrypt', 'decrypt']);
}
export async function sealCredential(key: CryptoKey, value: Uint8Array, aad: Uint8Array) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = await crypto.subtle.encrypt({name: 'AES-GCM', iv, additionalData: aad}, key, value);
  return {iv, data: new Uint8Array(data)};
}
export async function openCredential(
  key: CryptoKey, iv: Uint8Array, data: Uint8Array, aad: Uint8Array,
): Promise<Uint8Array> {
  const plain = await crypto.subtle.decrypt({name: 'AES-GCM', iv, additionalData: aad}, key, data);
  return new Uint8Array(plain);
}
