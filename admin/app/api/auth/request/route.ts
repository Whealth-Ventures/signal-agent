import { NextRequest, NextResponse } from "next/server";
import { isAllowed, signMagicToken } from "@/lib/auth";
import { sendMagicLink } from "@/lib/email";

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => null);
  const email = typeof body?.email === "string" ? body.email.trim().toLowerCase() : "";
  if (!email) {
    return NextResponse.json({ error: "missing email" }, { status: 400 });
  }

  // Always return 200 even if the email isn't allowed — avoids leaking which
  // addresses are whitelisted. Just don't actually send.
  if (!isAllowed(email)) {
    return NextResponse.json({ ok: true });
  }

  const token = await signMagicToken(email);
  const base = process.env.APP_URL || `https://${process.env.VERCEL_URL}`;
  const link = `${base}/api/auth/verify?token=${encodeURIComponent(token)}`;

  try {
    await sendMagicLink(email, link);
  } catch (e) {
    console.error("magic link send failed:", e);
    return NextResponse.json({ error: "email send failed" }, { status: 500 });
  }
  return NextResponse.json({ ok: true });
}
