import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { readFile, writeFile } from "@/lib/github";
import { triggerDeploy } from "@/lib/deploy";
import { parseKeywords, serializeKeywords, KeywordRow } from "@/lib/keywords";

const KEYWORDS_PATH = "inputs/keywords.xlsx";

export async function GET() {
  try {
    const { content } = await readFile(KEYWORDS_PATH);
    return NextResponse.json(await parseKeywords(content));
  } catch (e: any) {
    console.error("GET /api/keywords failed:", e);
    return NextResponse.json({ error: e.message || "read failed" }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const session = await getSession();
  if (!session) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const body = (await req.json().catch(() => null)) as { rows: KeywordRow[] } | null;
  if (!body || !Array.isArray(body.rows)) {
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }

  try {
    const buf = await serializeKeywords(body);
    await writeFile(
      KEYWORDS_PATH,
      buf,
      `keywords: update keywords.xlsx via admin UI (${session.email})`,
      session.email,
    );
    let deploy;
    try { deploy = await triggerDeploy(); }
    catch (e: any) { deploy = { triggered: false, detail: e.message || "trigger failed" }; }
    return NextResponse.json({ ok: true, deploy });
  } catch (e: any) {
    console.error("POST /api/keywords failed:", e);
    return NextResponse.json({ error: e.message || "write failed" }, { status: 500 });
  }
}
