"use client";
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";

type Row = { bucket: string; subBucket: string; keyword: string; geo: string };
const GEOS = ["Both", "India", "US"];
const blank = (): Row => ({ bucket: "", subBucket: "", keyword: "", geo: "Both" });

export default function KeywordsPage() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [msg, setMsg] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    fetch("/api/keywords")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).error || `error ${r.status}`);
        return r.json();
      })
      .then((d) => { setRows(d.rows); setStatus("ready"); })
      .catch((e) => { setStatus("error"); setMsg(e.message); });
  }, []);

  // Filter to a view of {row, idx} so edits/deletes map back to the real array.
  const view = useMemo(() => {
    if (!rows) return [];
    const needle = q.trim().toLowerCase();
    const paired = rows.map((row, idx) => ({ row, idx }));
    if (!needle) return paired;
    return paired.filter(({ row }) =>
      (row.bucket + " " + row.subBucket + " " + row.keyword + " " + row.geo)
        .toLowerCase()
        .includes(needle),
    );
  }, [rows, q]);

  function update(idx: number, key: keyof Row, v: string) {
    setRows((prev) => {
      if (!prev) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], [key]: v };
      return next;
    });
  }
  function remove(idx: number) {
    setRows((prev) => (prev ? prev.filter((_, j) => j !== idx) : prev));
  }
  function add() {
    setRows((prev) => (prev ? [...prev, blank()] : [blank()]));
    setQ(""); // show the new (empty) row
  }

  async function save() {
    if (!rows) return;
    setStatus("saving"); setMsg("");
    try {
      const res = await fetch("/api/keywords", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows }),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `error ${res.status}`);
      setStatus("ready");
      setMsg(
        j.deploy?.triggered
          ? `Saved. ${j.deploy.detail} — live in a few minutes.`
          : `Saved to git. ${j.deploy?.detail || "Run a deploy to apply."}`,
      );
    } catch (e: any) { setStatus("error"); setMsg(e.message); }
  }

  if (status === "loading") return <Layout><p className="text-sm text-gray-500">Loading keywords.xlsx…</p></Layout>;
  if (status === "error" && !rows) {
    return <Layout><div className="p-4 bg-red-50 text-red-700 text-sm rounded">{msg}</div></Layout>;
  }
  if (!rows) return null;

  const grid = "48px 1.2fr 1.2fr 1.6fr 90px 70px";

  return (
    <Layout>
      <p className="text-sm text-gray-500 mb-3">
        The ~2,240 keywords the query planner clusters into each day’s Perplexity
        searches. Bucket / Sub-bucket group them; Geo (India / US / Both) scopes
        which digest a keyword feeds. Save commits to the repo.
      </p>

      <div className="flex items-center gap-3 mb-3">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filter by bucket, sub-bucket, keyword…"
          className="border rounded px-3 py-1.5 text-sm w-80"
        />
        <span className="text-xs text-gray-500">
          {q ? `${view.length} of ${rows.length}` : `${rows.length} keywords`}
        </span>
      </div>

      <div className="bg-white border rounded overflow-x-auto">
        <div
          className="grid gap-2 px-3 py-2 border-b bg-gray-50 text-xs font-medium text-gray-600 uppercase min-w-[760px]"
          style={{ gridTemplateColumns: grid }}
        >
          <div>#</div><div>Bucket</div><div>Sub-bucket</div><div>Keyword</div><div>Geo</div><div />
        </div>
        {view.map(({ row, idx }) => (
          <div
            key={idx}
            className="grid gap-2 px-3 py-1.5 border-b items-center text-sm min-w-[760px]"
            style={{ gridTemplateColumns: grid }}
          >
            <div className="text-xs text-gray-400">{idx + 1}</div>
            <input value={row.bucket} onChange={(e) => update(idx, "bucket", e.target.value)}
              className="border rounded px-2 py-1 text-xs w-full" />
            <input value={row.subBucket} onChange={(e) => update(idx, "subBucket", e.target.value)}
              className="border rounded px-2 py-1 text-xs w-full" />
            <input value={row.keyword} onChange={(e) => update(idx, "keyword", e.target.value)}
              className="border rounded px-2 py-1 text-xs w-full" />
            <select value={GEOS.includes(row.geo) ? row.geo : "Both"}
              onChange={(e) => update(idx, "geo", e.target.value)}
              className="border rounded px-1 py-1 text-xs w-full">
              {GEOS.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
            <button onClick={() => remove(idx)}
              className="border rounded px-2 py-1 text-xs hover:bg-red-50 hover:text-red-700">
              Remove
            </button>
          </div>
        ))}
        {view.length === 0 && (
          <div className="px-3 py-4 text-sm text-gray-500">No keywords match “{q}”.</div>
        )}
        <div className="px-3 py-3">
          <button onClick={add} className="text-sm border rounded px-3 py-1 hover:bg-gray-50">
            + Add keyword
          </button>
        </div>
      </div>

      <div className="mt-8 flex items-center gap-4 sticky bottom-4">
        <button onClick={save} disabled={status === "saving"}
          className="bg-black text-white rounded px-6 py-2 text-sm font-medium hover:bg-gray-800 disabled:opacity-50 shadow">
          {status === "saving" ? "Saving…" : "Save changes"}
        </button>
        {msg && <span className={`text-sm ${status === "error" ? "text-red-600" : "text-green-700"}`}>{msg}</span>}
      </div>
    </Layout>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link href="/" className="text-sm text-gray-600 hover:text-gray-900">← Back</Link>
          <h1 className="text-lg font-semibold">Keywords</h1>
          <div className="w-12" />
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-6 py-8 pb-32">{children}</main>
    </div>
  );
}
