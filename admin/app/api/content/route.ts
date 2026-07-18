import { NextRequest, NextResponse } from "next/server";
import { getSession } from "@/lib/auth";
import { readFile, writeFile, listDir, deleteFile } from "@/lib/github";
import { triggerDeploy } from "@/lib/deploy";

const ROOT = "inputs/content";

// The agent embeds every *.md under inputs/content/ (rglob) as its taste
// profile. Guard the path so writes/deletes can't escape that tree.
function safePath(p: string): string {
  const path = (p || "").trim();
  if (!path.startsWith(ROOT + "/") || path.includes("..") || path.includes("//")) {
    throw new Error("path must be under inputs/content/");
  }
  if (!path.endsWith(".md")) throw new Error("only .md files are allowed");
  return path;
}

// GET               -> { folders: [{name, files:[{name,path}]}] }
// GET ?path=<file>  -> { path, content }
export async function GET(req: NextRequest) {
  const path = req.nextUrl.searchParams.get("path");
  try {
    if (path) {
      const p = safePath(path);
      const { content } = await readFile(p);
      return NextResponse.json({ path: p, content: content.toString("utf-8") });
    }
    const dirs = (await listDir(ROOT)).filter((d) => d.type === "dir");
    const folders = await Promise.all(
      dirs.map(async (d) => ({
        name: d.name,
        files: (await listDir(d.path))
          .filter((f) => f.type === "file" && f.name.endsWith(".md"))
          .map((f) => ({ name: f.name, path: f.path })),
      })),
    );
    return NextResponse.json({ folders });
  } catch (e: any) {
    console.error("GET /api/content failed:", e);
    return NextResponse.json({ error: e.message || "read failed" }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const session = await getSession();
  if (!session) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const body = (await req.json().catch(() => null)) as { path?: string; content?: string } | null;
  if (!body || typeof body.content !== "string") {
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }
  try {
    const p = safePath(body.path || "");
    if (!body.content.trim()) throw new Error("content is empty");
    await writeFile(p, body.content, `content: update ${p} via admin UI (${session.email})`, session.email);
    let deploy;
    try { deploy = await triggerDeploy(); }
    catch (e: any) { deploy = { triggered: false, detail: e.message || "trigger failed" }; }
    return NextResponse.json({ ok: true, deploy });
  } catch (e: any) {
    console.error("POST /api/content failed:", e);
    return NextResponse.json({ error: e.message || "write failed" }, { status: 500 });
  }
}

export async function DELETE(req: NextRequest) {
  const session = await getSession();
  if (!session) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  try {
    const p = safePath(req.nextUrl.searchParams.get("path") || "");
    await deleteFile(p, `content: delete ${p} via admin UI (${session.email})`, session.email);
    let deploy;
    try { deploy = await triggerDeploy(); }
    catch (e: any) { deploy = { triggered: false, detail: e.message || "trigger failed" }; }
    return NextResponse.json({ ok: true, deploy });
  } catch (e: any) {
    console.error("DELETE /api/content failed:", e);
    return NextResponse.json({ error: e.message || "delete failed" }, { status: 500 });
  }
}
