import { NextRequest, NextResponse } from "next/server";
import { isAllowed, setSession, verifyMagicToken } from "@/lib/auth";

export async function GET(req: NextRequest) {
  const token = req.nextUrl.searchParams.get("token");
  if (!token) {
    return NextResponse.redirect(new URL("/login?error=missing", req.url));
  }
  const email = await verifyMagicToken(token);
  if (!email || !isAllowed(email)) {
    return NextResponse.redirect(new URL("/login?error=invalid", req.url));
  }
  await setSession(email);
  return NextResponse.redirect(new URL("/", req.url));
}
