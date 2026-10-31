/**
 * How a person is stored: a keyed hash of their Telegram id, never the id itself.
 *
 * Same recipe as `user_key` in quiz_bot.py (HMAC-SHA256 of the decimal id, first 16 hex
 * characters, prefixed with "u"), so a person has the same key in both systems.
 * tests/fixtures/user_key_vectors.json is checked by Python and TypeScript alike.
 */
const encoder = new TextEncoder();

export async function userKey(secret: string, userId: number): Promise<string> {
  if (!secret) throw new Error("SCORES_SALT is not set");
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(String(userId))));
  const hex = Array.from(signature, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `u${hex.slice(0, 16)}`;
}

/** Compare two secrets without leaking, through timing, how many characters matched. */
export function safeEqual(a: string, b: string): boolean {
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  let diff = left.length ^ right.length;
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    diff |= (left[i] ?? 0) ^ (right[i] ?? 0);
  }
  return diff === 0;
}
