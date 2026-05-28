import { SignJWT, jwtVerify } from "jose";
import { cookies } from "next/headers";

const SESSION_COOKIE = "signal_admin_session";
const SESSION_TTL_SECONDS = 60 * 60 * 24; // 24h
const MAGIC_LINK_TTL_SECONDS = 60 * 15;   // 15m

function secret(): Uint8Array {
  const s = process.env.AUTH_SECRET;
  if (!s || s.length < 32) {
    throw new Error("AUTH_SECRET env var must be at least 32 chars");
  }
  return new TextEncoder().encode(s);
}

export function allowedEmails(): string[] {
  return (process.env.ALLOWED_EMAILS || "")
    .split(",")
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean);
}

export function isAllowed(email: string): boolean {
  return allowedEmails().includes(email.trim().toLowerCase());
}

export async function signMagicToken(email: string): Promise<string> {
  return new SignJWT({ email: email.toLowerCase(), kind: "magic" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime(`${MAGIC_LINK_TTL_SECONDS}s`)
    .sign(secret());
}

export async function verifyMagicToken(token: string): Promise<string | null> {
  try {
    const { payload } = await jwtVerify(token, secret());
    if (payload.kind !== "magic" || typeof payload.email !== "string") return null;
    return payload.email;
  } catch {
    return null;
  }
}

export async function setSession(email: string): Promise<void> {
  const token = await new SignJWT({ email: email.toLowerCase(), kind: "session" })
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
