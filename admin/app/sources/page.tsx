"use client";
import { useEffect, useState } from "react";
import Link from "next/link";

type Row = Record<string, string | number | null>;
type VoicesData = {
  publications: Row[];
  indiaVoices: Row[];
  usVoices: Row[];
  firms: Row[];
  newAdditions: Row[];
};

type Col = { key: string; label: string; w: string; number?: boolean };

// Column configs mirror lib/voices.ts (which mirrors src/query_planner.py).
const COLS: Record<keyof VoicesData, Col[]> = {
  publications: [
    { key: "name", label: "Publication", w: "1.2fr" },
    { key: "geography", label: "Geography", w: "90px" },
    { key: "type", label: "Type", w: "1fr" },
    { key: "author", label: "Run by", w: "1fr" },
    { key: "description", label: "What it covers", w: "1.4fr" },
    { key: "reach", label: "Reach", w: "1fr" },
    { key: "url", label: "URL (RSS auto-discovered)", w: "1.4fr" },
  ],
  indiaVoices: voiceCols(),
  usVoices: voiceCols(),
  firms: [
    { key: "name", label: "Organization", w: "1.2fr" },
    { key: "geography", label: "Geography", w: "100px" },
    { key: "type", label: "Type", w: "1fr" },
    { key: "description", label: "What they do", w: "1.4fr" },
    { key: "whyFollow", label: "Why follow", w: "1.4fr" },
    { key: "linkedinUrl", label: "LinkedIn URL", w: "1.2fr" },
  ],
  newAdditions: [
    { key: "category", label: "Category", w: "1fr" },
    { key: "firm", label: "Firm", w: "1fr" },
    { key: "hq", label: "HQ", w: "0.8fr" },
    { key: "stageTicket", label: "Stage / Ticket", w: "1fr" },
    { key: "thesis", label: "Healthcare thesis", w: "1.4fr" },
    { key: "portfolio", label: "Notable portfolio", w: "1.4fr" },
    { key: "source", label: "Source", w: "1.2fr" },
  ],
};

function voiceCols(): Col[] {
  return [
    { key: "name", label: "Name", w: "1fr" },
    { key: "category", label: "Category", w: "1fr" },
    { key: "subDomain", label: "Sub-domain", w: "1fr" },
    { key: "role", label: "Role / Org", w: "1.2fr" },
    { key: "why", label: "Why they matter", w: "1.4fr" },
    { key: "reach", label: "Reach", w: "1fr" },
    { key: "linkedinUrl", label: "LinkedIn URL", w: "1.2fr" },
    { key: "tier", label: "Tier", w: "60px", number: true },
    { key: "rssUrl", label: "RSS URL", w: "1.2fr" },
  ];
}

const TABS: [keyof VoicesData, string][] = [
  ["publications", "Publications"],
  ["indiaVoices", "India Voices"],
  ["usVoices", "US Voices"],
  ["firms", "Firms & Orgs"],
  ["newAdditions", "PE/VC Firms"],
];

const BLANK: Record<keyof VoicesData, () => Row> = {
  publications: () => ({ name: "", geography: "", type: "", author: "", description: "", reach: "", url: "" }),
  indiaVoices: () => blankVoice(),
  usVoices: () => blankVoice(),
  firms: () => ({ name: "", geography: "", type: "", description: "", whyFollow: "", linkedinUrl: "" }),
  newAdditions: () => ({ category: "", firm: "", hq: "", stageTicket: "", thesis: "", portfolio: "", source: "" }),
};
function blankVoice(): Row {
  return { name: "", category: "", subDomain: "", role: "", why: "", reach: "", linkedinUrl: "", tier: null, rssUrl: "" };
}

export default function SourcesPage() {
  const [data, setData] = useState<VoicesData | null>(null);
  const [tab, setTab] = useState<keyof VoicesData>("publications");
  const [status, setStatus] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    fetch("/api/sources")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).error || `error ${r.status}`);
        return r.json();
      })
      .then((d) => { setData(d); setStatus("ready"); })
      .catch((e) => { setStatus("error"); setMsg(e.message); });
  }, []);

  async function save() {
    if (!data) return;
    setStatus("saving"); setMsg("");
    try {
      const res = await fetch("/api/sources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
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

  if (status === "loading") return <Layout><p className="text-sm text-gray-500">Loading voices.xlsx…</p></Layout>;
  if (status === "error" && !data) {
    return <Layout><div className="p-4 bg-red-50 text-red-700 text-sm rounded">{msg}</div></Layout>;
  }
  if (!data) return null;

  const setRows = (rows: Row[]) => setData({ ...data, [tab]: rows });

  return (
    <Layout>
      <div className="flex gap-1 border-b mb-4 -mt-2 overflow-x-auto">
        {TABS.map(([id, label]) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`px-4 py-2 text-sm border-b-2 -mb-px whitespace-nowrap ${
              tab === id ? "border-black font-medium" : "border-transparent text-gray-500 hover:text-gray-900"
            }`}
          >
            {label} <span className="text-gray-400">({(data[id] as Row[]).length})</span>
          </button>
        ))}
      </div>

      <p className="text-sm text-gray-500 mb-3">
        Add or remove the sources the agent watches. For publications, put the
        homepage or section URL in the URL column — the fetcher auto-discovers its
        RSS feed. Save commits to the repo; the next morning’s digest uses it.
      </p>

      <EditableTable
        cols={COLS[tab]}
        rows={data[tab] as Row[]}
        onChange={setRows}
        onAdd={() => setRows([...(data[tab] as Row[]), BLANK[tab]()])}
      />

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

function EditableTable({
  cols, rows, onChange, onAdd,
}: {
  cols: Col[];
  rows: Row[];
  onChange: (rows: Row[]) => void;
  onAdd: () => void;
}) {
  const grid = `40px ${cols.map((c) => c.w).join(" ")} 80px`;
  const update = (i: number, key: string, v: string) => {
    const next = [...rows];
    const col = cols.find((c) => c.key === key)!;
    next[i] = { ...next[i], [key]: col.number ? (v === "" ? null : Number(v)) : v };
    onChange(next);
  };
  const remove = (i: number) => onChange(rows.filter((_, j) => j !== i));

  return (
    <div className="bg-white border rounded overflow-x-auto">
      <div
        className="grid gap-2 px-3 py-2 border-b bg-gray-50 text-xs font-medium text-gray-600 uppercase min-w-[900px]"
        style={{ gridTemplateColumns: grid }}
      >
        <div>#</div>
        {cols.map((c) => <div key={c.key}>{c.label}</div>)}
        <div />
      </div>
      {rows.map((r, i) => (
        <div
          key={i}
          className="grid gap-2 px-3 py-1.5 border-b items-center text-sm min-w-[900px]"
          style={{ gridTemplateColumns: grid }}
        >
          <div className="text-xs text-gray-400">{i + 1}</div>
          {cols.map((c) => (
            <input
              key={c.key}
              type={c.number ? "number" : "text"}
              value={r[c.key] == null ? "" : String(r[c.key])}
              onChange={(e) => update(i, c.key, e.target.value)}
              className="border rounded px-2 py-1 text-xs w-full"
            />
          ))}
          <button
            onClick={() => remove(i)}
            className="border rounded px-2 py-1 text-xs hover:bg-red-50 hover:text-red-700"
          >
            Remove
          </button>
        </div>
      ))}
      <div className="px-3 py-3">
        <button onClick={onAdd} className="text-sm border rounded px-3 py-1 hover:bg-gray-50">
          + Add row
        </button>
      </div>
    </div>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link href="/" className="text-sm text-gray-600 hover:text-gray-900">← Back</Link>
          <h1 className="text-lg font-semibold">Sources</h1>
          <div className="w-12" />
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-6 py-8 pb-32">{children}</main>
    </div>
  );
}
