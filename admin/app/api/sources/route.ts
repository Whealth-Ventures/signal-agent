import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { readFile, writeFile } from "@/lib/github";
import { triggerDeploy } from "@/lib/deploy";
import { parseVoices, serializeVoices, VoicesData } from "@/lib/voices";

const VOICES_PATH = "inputs/voices.xlsx";

export async function GET() {
  try {
    const { content } = await readFile(VOICES_PATH);
    return NextResponse.json(await parseVoices(content));
  } catch (e: any) {
    console.error("GET /api/sources failed:", e);
    return NextResponse.json({ error: e.message || "read failed" }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const session = await getSession();
  if (!session) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = (await req.json().catch(() => null)) as VoicesData | null;
  if (
    !body ||
    !Array.isArray(body.publications) ||
    !Array.isArray(body.indiaVoices) ||
    !Array.isArray(body.usVoices) ||
    !Array.isArray(body.firms) ||
    !Array.isArray(body.newAdditions)
  ) {
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }

  try {
    // Re-read the current workbook so we rewrite data rows in place (preserving
    // banners/headers/styling the agent's positional loaders depend on).
    const { content } = await readFile(VOICES_PATH);
    const buf = await serializeVoices(content, body);
    await writeFile(
      VOICES_PATH,
      buf,
      `sources: update voices.xlsx via admin UI (${session.email})`,
      session.email,
    );
    // Commit succeeded; kick a deploy so the box picks it up. Don't fail the
    // save if the trigger itself errors — the commit is durable.
    let deploy;
    try { deploy = await triggerDeploy(); }
    catch (e: any) { deploy = { triggered: false, detail: e.message || "trigger failed" }; }
    return NextResponse.json({ ok: true, deploy });
  } catch (e: any) {
    console.error("POST /api/sources failed:", e);
    return NextResponse.json({ error: e.message || "write failed" }, { status: 500 });
  }
}
