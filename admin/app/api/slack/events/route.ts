import { NextRequest, NextResponse } from "next/server";
import { verifySlackSignature } from "@/lib/slack-verify";
import { appendFeedbackEvent } from "@/lib/feedback-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const secret = process.env.SLACK_SIGNING_SECRET;
  if (!secret) {
    console.error("SLACK_SIGNING_SECRET not set");
    return NextResponse.json({ error: "server misconfigured" }, { status: 500 });
  }

  const raw = await req.text();
  const ok = verifySlackSignature({
    body: raw,
    timestamp: req.headers.get("x-slack-request-timestamp"),
    signature: req.headers.get("x-slack-signature"),
    signingSecret: secret,
  });
  if (!ok) {
    return NextResponse.json({ error: "invalid signature" }, { status: 401 });
  }

  let parsed: any;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }

  if (parsed?.type === "url_verification" && typeof parsed.challenge === "string") {
    return NextResponse.json({ challenge: parsed.challenge });
  }

  console.log("slack event received: outer type =", parsed?.type, "inner type =", parsed?.event?.type);

  if (parsed?.type === "event_callback") {
    const eventId: string =
      parsed.event_id || `${parsed.event?.type || "unknown"}-${Date.now()}`;
    try {
      await appendFeedbackEvent({
        received_at: new Date().toISOString(),
        event_id: eventId,
        team_id: parsed.team_id,
        api_app_id: parsed.api_app_id,
        type: parsed.event?.type || "unknown",
        event: parsed.event,
      });
      console.log("blob append ok: event_id =", eventId);
    } catch (e: any) {
      console.error("blob append FAILED:", e?.message || e, e?.stack);
    }
    return NextResponse.json({ ok: true });
  }

  console.log("slack event fell through (no handler): type =", parsed?.type);
  return NextResponse.json({ ok: true });
}
