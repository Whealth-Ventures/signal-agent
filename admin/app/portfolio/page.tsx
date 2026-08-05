"use client";
import { useEffect, useState } from "react";
import Link from "next/link";

type Row = Record<string, string | null>;
type Col = { key: string; label: string; w: string; multi?: boolean };

// Mirrors lib/portfolio.ts (which mirrors src/sector.py load_portfolio).
const COLS: Col[] = [
  { key: "company", label: "Company", w: "1fr" },
  { key: "sector", label: "Sector", w: "1fr" },
  { key: "business", label: "What they do", w: "2fr", multi: true },
  { key: "geo", label: "Geo", w: "80px" },
  { key: "website", label: "Website", w: "1fr" },
  { key: "competitors", label: "Key competitors", w: "1.5fr", multi: true },
  { key: "moves", label: "What moves them", w: "2.5fr", multi: true },
];

const blank = (): Row =>
  Object.fromEntries(COLS.map((c) => [c.key, ""]));

export default function PortfolioPage() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    fetch("/api/portfolio")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).error || `error ${r.status}`);
        return r.json();
      })
      .then((d) => { setRows(d.companies); setStatus("ready"); })
      .catch((e) => { setStatus("error"); setMsg(e.message); });
  }, []);

  async function save() {
    if (!rows) return;
    setStatus("saving"); setMsg("");
    try {
      const res = await fetch("/api/portfolio", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ companies: rows }),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `error ${res.status}`);
      setStatus("ready");
      setMsg(
        j.deploy?.triggered
          ? `Saved. ${j.deploy.detail} — live in a few minutes.`
          : `Saved to git. ${j.deploy?.detail || "Applies on the next weekly run."}`,
      );
    } catch (e: any) { setStatus("error"); setMsg(e.message); }
  }

  if (status === "loading") return <Layout><p className="text-sm text-gray-500">Loading portfolio.xlsx…</p></Layout>;
  if (status === "error" && !rows) {
    return <Layout><div className="p-4 bg-red-50 text-red-700 text-sm rounded">{msg}</div></Layout>;
  }
  if (!rows) return null;

  const grid = `40px ${COLS.map((c) => c.w).join(" ")} 80px`;
  const update = (i: number, key: string, v: string) => {
    const next = [...rows];
    next[i] = { ...next[i], [key]: v };
    setRows(next);
  };
  const remove = (i: number) => setRows(rows.filter((_, j) => j !== i));

  return (
    <Layout>
      <p className="text-sm text-gray-500 mb-3">
        The portfolio companies the weekly <strong>Sector Agent</strong> watches —
        add, remove, or edit rows here. Company name, sector, what it does, main
        geography (India / US / Global), optional website, plus the two fields the
        agent reasons with: <strong>Key competitors</strong> (named in its weekly
        search, so competitor moves get surfaced) and <strong>What moves them</strong>
        (the regulatory / sector / macro forces used to judge whether a story is
        material and whether the impact is positive or negative). Leaving those two
        blank still works — the digest is just blunter for that company. Save commits
        to the repo; the next weekly run uses it.
      </p>

      <div className="bg-white border rounded overflow-x-auto">
        <div
          className="grid gap-2 px-3 py-2 border-b bg-gray-50 text-xs font-medium text-gray-600 uppercase min-w-[1500px]"
          style={{ gridTemplateColumns: grid }}
        >
          <div>#</div>
          {COLS.map((c) => <div key={c.key}>{c.label}</div>)}
          <div />
        </div>
        {rows.map((r, i) => (
          <div
            key={i}
            className="grid gap-2 px-3 py-1.5 border-b items-start text-sm min-w-[1500px]"
            style={{ gridTemplateColumns: grid }}
          >
            <div className="text-xs text-gray-400">{i + 1}</div>
            {COLS.map((c) =>
              c.multi ? (
                <textarea
                  key={c.key}
                  rows={3}
                  value={r[c.key] == null ? "" : String(r[c.key])}
                  onChange={(e) => update(i, c.key, e.target.value)}
                  className="border rounded px-2 py-1 text-xs w-full resize-y font-normal"
                />
              ) : (
                <input
                  key={c.key}
                  type="text"
                  value={r[c.key] == null ? "" : String(r[c.key])}
                  onChange={(e) => update(i, c.key, e.target.value)}
                  className="border rounded px-2 py-1 text-xs w-full"
                />
              ),
            )}
            <button
              onClick={() => remove(i)}
              className="border rounded px-2 py-1 text-xs hover:bg-red-50 hover:text-red-700"
            >
              Remove
            </button>
          </div>
        ))}
        <div className="px-3 py-3">
          <button onClick={() => setRows([...rows, blank()])} className="text-sm border rounded px-3 py-1 hover:bg-gray-50">
            + Add company
          </button>
        </div>
      </div>

      <div className="mt-8 flex items-center gap-4 sticky bottom-4">
        <button
          onClick={save}
          disabled={status === "saving"}
          className="bg-black text-white rounded px-6 py-2 text-sm font-medium hover:bg-gray-800 disabled:opacity-50 shadow"
        >
          {status === "saving" ? "Saving…" : "Save changes"}
        </button>
        {msg && (
          <span className={`text-sm ${status === "error" ? "text-red-600" : "text-green-700"}`}>{msg}</span>
        )}
      </div>
    </Layout>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link href="/" className="text-sm text-gray-600 hover:text-gray-900">← Back</Link>
          <h1 className="text-lg font-semibold">Portfolio</h1>
          <div className="w-12" />
        </div>
      </header>
      <main className="max-w-7xl mx-auto px-6 py-8 pb-32">{children}</main>
    </div>
  );
}
