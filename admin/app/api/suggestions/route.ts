import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { readFile, writeFile } from "@/lib/github";
import { triggerDeploy } from "@/lib/deploy";
import { parseTuning, serializeTuning } from "@/lib/xlsx";

const PENDING_PATH = "proposals/pending.json";
const TUNING_PATH = "inputs/tuning.xlsx";

export type Proposal = {
  id: string;
  type: "booster_weight_adjustment" | string;
  target: { sheet: string; row_name: string; column: string };
  current: number;
  proposed: number;
  rationale: string;
  evidence: Record<string, unknown>;
};

export type ProposalsFile = {
  generated_at: string;
  window_days: number;
  scored_digest_count: number;
  upvoted_digest_count: number;
  downvoted_digest_count: number;
  proposals: Proposal[];
  digest_scores: unknown[];
  rejected?: Array<{ id: string; reason: string; at: string; by: string }>;
};

async function loadPending(): Promise<ProposalsFile | null> {
  try {
    const { content } = await readFile(PENDING_PATH);
    return JSON.parse(content.toString("utf-8")) as ProposalsFile;
  } catch (e: any) {
    if (e.status === 404) return null;
    throw e;
  }
}

export async function GET() {
  try {
    const data = await loadPending();
    return NextResponse.json(data ?? {
      proposals: [], digest_scores: [],
      scored_digest_count: 0,
      upvoted_digest_count: 0, downvoted_digest_count: 0,
      generated_at: null, window_days: 0,
    });
  } catch (e: any) {
    console.error("GET /api/suggestions failed:", e);
    return NextResponse.json({ error: e.message || "read failed" }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const session = await getSession();
  if (!session) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const body = (await req.json().catch(() => null)) as
    | { id: string; action: "accept" | "reject"; reason?: string }
    | null;
  if (!body || !body.id || !["accept", "reject"].includes(body.action)) {
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }

  const pending = await loadPending();
  if (!pending) {
    return NextResponse.json({ error: "no pending proposals" }, { status: 404 });
  }
  const idx = pending.proposals.findIndex((p) => p.id === body.id);
  if (idx < 0) {
    return NextResponse.json({ error: "proposal not found" }, { status: 404 });
  }
  const proposal = pending.proposals[idx];

  try {
    if (body.action === "accept") {
      await applyProposal(proposal, session.email);
    } else {
      pending.rejected = pending.rejected || [];
      pending.rejected.push({
        id: proposal.id,
        reason: body.reason || "",
        at: new Date().toISOString(),
        by: session.email,
      });
    }
    pending.proposals.splice(idx, 1);
    await writeFile(
      PENDING_PATH,
      JSON.stringify(pending, null, 2),
      `feedback: ${body.action} proposal ${proposal.id} (${session.email})`,
      session.email,
    );
    // Accepting a proposal edits tuning.xlsx — deploy so the box picks it up.
    // Rejecting only updates the proposals ledger (regenerated each run), so no
    // deploy is needed.
    let deploy;
    if (body.action === "accept") {
      try { deploy = await triggerDeploy(); }
      catch (e: any) { deploy = { triggered: false, detail: e.message || "trigger failed" }; }
    }
    return NextResponse.json({ ok: true, deploy });
  } catch (e: any) {
    console.error(`POST /api/suggestions ${body.action} failed:`, e);
    return NextResponse.json({ error: e.message || "action failed" }, { status: 500 });
  }
}

async function applyProposal(p: Proposal, authorEmail: string): Promise<void> {
  if (p.type !== "booster_weight_adjustment") {
    throw new Error(`unsupported proposal type: ${p.type}`);
  }
  if (p.target.sheet !== "Boosters" || p.target.column !== "weight") {
    throw new Error(`unexpected target: ${JSON.stringify(p.target)}`);
  }
  const { content } = await readFile(TUNING_PATH);
  const tuning = await parseTuning(content);
  const row = tuning.boosters.find((b) => b.name === p.target.row_name);
  if (!row) {
    throw new Error(`booster '${p.target.row_name}' not found in tuning.xlsx`);
  }
  row.weight = p.proposed;
  const buf = await serializeTuning(tuning);
  await writeFile(
    TUNING_PATH,
    buf,
    `tune: apply proposal ${p.id} — '${p.target.row_name}' ${p.current} → ${p.proposed}`,
    authorEmail,
  );
}
