import { NextResponse } from "next/server";
import { listRecentFeedback } from "@/lib/feedback-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Live view of recent Slack reactions, read straight from Vercel Blob. Lets an
// analyst confirm a 👍/👎 was captured without waiting for the daily cron.
export async function GET() {
  try {
    const data = await listRecentFeedback(50);
    return NextResponse.json(data);
  } catch (e: any) {
    console.error("GET /api/feedback/recent failed:", e?.message || e);
    return NextResponse.json(
      { error: e?.message || "failed to read feedback" },
      { status: 500 },
    );
  }
}
