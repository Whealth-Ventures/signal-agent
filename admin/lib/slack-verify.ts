import crypto from "node:crypto";

const VERSION = "v0";
const MAX_AGE_SECONDS = 60 * 5;

export function verifySlackSignature(args: {
  body: string;
  timestamp: string | null;
  signature: string | null;
  signingSecret: string;
  now?: number;
}): boolean {
  const { body, timestamp, signature, signingSecret } = args;
  if (!timestamp || !signature) return false;

  const ts = Number(timestamp);
  if (!Number.isFinite(ts)) return false;
  const now = args.now ?? Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > MAX_AGE_SECONDS) return false;

  const base = `${VERSION}:${timestamp}:${body}`;
  const expected = `${VERSION}=${crypto
    .createHmac("sha256", signingSecret)
    .update(base)
    .digest("hex")}`;

  const a = Buffer.from(expected);
  const b = Buffer.from(signature);
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}
