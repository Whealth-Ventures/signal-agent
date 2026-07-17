import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { readFile, writeFile } from "@/lib/github";
import { triggerDeploy } from "@/lib/deploy";
import { parseTuning, serializeTuning, Tuning } from "@/lib/xlsx";

const TUNING_PATH = "inputs/tuning.xlsx";

export async function GET() {
  try {
    const { content } = await readFile(TUNING_PATH);
    const tuning = await parseTuning(content);
    return NextResponse.json(tuning);
  } catch (e: any) {
    console.error("GET /api/tuning failed:", e);
    return NextResponse.json({ error: e.message || "read failed" }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const session = await getSession();
  if (!session) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = (await req.json().catch(() => null)) as Tuning | null;
  if (!body || !Array.isArray(body.settings) || !Array.isArray(body.boosters)) {
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }

  // Server-side validation: regex patterns must compile.
  for (const b of body.boosters) {
    if (b.pattern_regex) {
      try {
        new RegExp(b.pattern_regex, "i");
      } catch (e: any) {
        return NextResponse.json(
          { error: `Invalid regex on booster '${b.name}': ${e.message}` },
          { status: 400 },
        );
      }
    }
  }

  // Server-side validation: priority bucket geos.
  for (const p of body.priorityBuckets) {
    const geos = p.geos.split(";").map((g) => g.trim()).filter(Boolean);
    for (const g of geos) {
      if (!["India", "US", "Global"].includes(g)) {
        return NextResponse.json(
          { error: `Bucket '${p.key}' has invalid geo '${g}'. Allowed: India, US, Global.` },
          { status: 400 },
        );
      }
    }
    if (geos.length === 0) {
      return NextResponse.json(
        { error: `Bucket '${p.key}' has no geos.` },
        { status: 400 },
      );
    }
    if (!p.sub_buckets.trim()) {
      return NextResponse.json(
        { error: `Bucket '${p.key}' has no sub_buckets.` },
        { status: 400 },
      );
    }
  }

  try {
    const buf = await serializeTuning(body);
    await writeFile(
      TUNING_PATH,
      buf,
      `tune: update tuning.xlsx via admin UI (${session.email})`,
      session.email,
    );
    let deploy;
    try { deploy = await triggerDeploy(); }
    catch (e: any) { deploy = { triggered: false, detail: e.message || "trigger failed" }; }
    return NextResponse.json({ ok: true, deploy });
  } catch (e: any) {
    console.error("POST /api/tuning failed:", e);
    return NextResponse.json({ error: e.message || "write failed" }, { status: 500 });
  }
}
