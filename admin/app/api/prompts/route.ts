import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { readFile, writeFile } from "@/lib/github";
import { triggerDeploy } from "@/lib/deploy";

const RANKER_PATH = "prompts/ranker_system.md";
const RUBRIC_PATH = "prompts/magnitude_rubric.md";

export async function GET() {
  try {
    const [ranker, rubric] = await Promise.all([
      readFile(RANKER_PATH),
      readFile(RUBRIC_PATH),
    ]);
    return NextResponse.json({
      ranker_system: ranker.content.toString("utf-8"),
      magnitude_rubric: rubric.content.toString("utf-8"),
    });
  } catch (e: any) {
    console.error("GET /api/prompts failed:", e);
    return NextResponse.json({ error: e.message || "read failed" }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const session = await getSession();
  if (!session) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = await req.json().catch(() => null);
  if (
    !body ||
    typeof body.ranker_system !== "string" ||
    typeof body.magnitude_rubric !== "string"
  ) {
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }
  if (!body.ranker_system.trim() || !body.magnitude_rubric.trim()) {
    return NextResponse.json(
      { error: "Prompts cannot be empty." },
      { status: 400 },
    );
  }

  try {
    // Two commits so each prompt has its own audit trail.
    await writeFile(
      RANKER_PATH,
      body.ranker_system,
      `prompt: update ranker_system.md via admin UI (${session.email})`,
      session.email,
    );
    await writeFile(
      RUBRIC_PATH,
      body.magnitude_rubric,
      `prompt: update magnitude_rubric.md via admin UI (${session.email})`,
      session.email,
    );
    let deploy;
    try { deploy = await triggerDeploy(); }
    catch (e: any) { deploy = { triggered: false, detail: e.message || "trigger failed" }; }
    return NextResponse.json({ ok: true, deploy });
  } catch (e: any) {
    console.error("POST /api/prompts failed:", e);
    return NextResponse.json({ error: e.message || "write failed" }, { status: 500 });
  }
}
