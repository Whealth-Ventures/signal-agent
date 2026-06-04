import { SignJWT, jwtVerify } from "jose";
import { cookies } from "next/headers";

const SESSION_COOKIE = "signal_admin_session";
const SESSION_TTL_SECONDS = 60 * 60 * 24; // 24h

function secret(): Uint8Array {
  const s = process.env.AUTH_SECRET;
  if (!s || s.length < 32) {
    throw new Error("AUTH_SECRET env var must be at least 32 chars");
  }
  return new TextEncoder().encode(s);
}

// Constant-time string comparison so failed logins don't leak the secret's
// contents via response timing. (Length is allowed to leak — that's standard.)
function safeEqual(a: string, b: string): boolean {
  const aBuf = new TextEncoder().encode(a);
  const bBuf = new TextEncoder().encode(b);
  if (aBuf.length !== bBuf.length) return false;
  let diff = 0;
  for (let i = 0; i < aBuf.length; i++) diff |= aBuf[i] ^ bBuf[i];
  return diff === 0;
}

// Single shared login. Credentials live only in Vercel env vars; anyone who
// knows them can sign in. Replaces the old per-email magic-link allowlist.
export function verifyCredentials(username: string, password: string): boolean {
  const u = process.env.ADMIN_USERNAME;
  const p = process.env.ADMIN_PASSWORD;
  if (!u || !p) {
    throw new Error("ADMIN_USERNAME / ADMIN_PASSWORD env vars not set");
  }
  // Evaluate both halves before AND-ing so timing doesn't reveal whether the
  // username alone matched.
  const userOk = safeEqual(username, u);
  const passOk = safeEqual(password, p);
  return userOk && passOk;
}

export async function setSession(identifier: string): Promise<void> {
  const token = await new SignJWT({ email: identifier.toLowerCase(), kind: "session" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime(`${SESSION_TTL_SECONDS}s`)
    .sign(secret());

  cookies().set(SESSION_COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_TTL_SECONDS,
  });
}

export async function getSession(): Promise<{ email: string } | null> {
  const token = cookies().get(SESSION_COOKIE)?.value;
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, secret());
    if (payload.kind !== "session" || typeof payload.email !== "string") return null;
    return { email: payload.email };
  } catch {
    return null;
  }
}

export async function getSessionFromCookieString(
  cookieHeader: string | null,
): Promise<{ email: string } | null> {
  if (!cookieHeader) return null;
  const match = cookieHeader
    .split(/;\s*/)
    .find((c) => c.startsWith(`${SESSION_COOKIE}=`));
  if (!match) return null;
  const token = decodeURIComponent(match.slice(SESSION_COOKIE.length + 1));
  try {
    const { payload } = await jwtVerify(token, secret());
    if (payload.kind !== "session" || typeof payload.email !== "string") return null;
    return { email: payload.email };
  } catch {
    return null;
  }
}

export function clearSession(): void {
  cookies().delete(SESSION_COOKIE);
}
