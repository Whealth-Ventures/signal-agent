import { NextRequest, NextResponse } from "next/server";
import { setSession, verifyCredentials } from "@/lib/auth";

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => null);
  const username = typeof body?.username === "string" ? body.username.trim() : "";
  const password = typeof body?.password === "string" ? body.password : "";
  if (!username || !password) {
    return NextResponse.json({ error: "missing username or password" }, { status: 400 });
  }

  let ok = false;
  try {
    ok = verifyCredentials(username, password);
  } catch (e) {
    console.error("auth not configured:", e);
    return NextResponse.json({ error: "auth not configured" }, { status: 500 });
  }
  if (!ok) {
    return NextResponse.json({ error: "invalid username or password" }, { status: 401 });
  }

  await setSession(username);
  return NextResponse.json({ ok: true });
}
