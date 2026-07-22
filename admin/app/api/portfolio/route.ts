import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { readFile, writeFile } from "@/lib/github";
import { triggerDeploy } from "@/lib/deploy";
import { parsePortfolio, serializePortfolio, PortfolioData } from "@/lib/portfolio";

const PORTFOLIO_PATH = "inputs/portfolio.xlsx";

export async function GET() {
  try {
    const { content } = await readFile(PORTFOLIO_PATH);
    return NextResponse.json(await parsePortfolio(content));
  } catch (e: any) {
    console.error("GET /api/portfolio failed:", e);
    return NextResponse.json({ error: e.message || "read failed" }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const session = await getSession();
  if (!session) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const body = (await req.json().catch(() => null)) as PortfolioData | null;
  if (!body || !Array.isArray(body.companies)) {
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }

  try {
    // Re-read the current workbook so we rewrite the data region in place
    // (preserving the header row the agent's positional loader depends on).
    const { content } = await readFile(PORTFOLIO_PATH);
    const buf = await serializePortfolio(content, body);
    await writeFile(
      PORTFOLIO_PATH,
      buf,
      `portfolio: update portfolio.xlsx via admin UI (${session.email})`,
      session.email,
    );
    let deploy;
    try { deploy = await triggerDeploy(); }
    catch (e: any) { deploy = { triggered: false, detail: e.message || "trigger failed" }; }
    return NextResponse.json({ ok: true, deploy });
  } catch (e: any) {
    console.error("POST /api/portfolio failed:", e);
    return NextResponse.json({ error: e.message || "write failed" }, { status: 500 });
  }
}
