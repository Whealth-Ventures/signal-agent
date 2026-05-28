"use client";
import { useEffect, useState } from "react";
import Link from "next/link";

type SettingRow = { name: string; value: string | number | null; description: string };
type BoosterRow = { name: string; weight: number; pattern_regex: string; description: string };
type PriorityBucketRow = { key: string; display: string; sub_buckets: string; geos: string };
type SourceTierRow = { host: string };

type Tuning = {
  settings: SettingRow[];
  boosters: BoosterRow[];
  priorityBuckets: PriorityBucketRow[];
  sourceTiers: SourceTierRow[];
};

type Tab = "settings" | "boosters" | "buckets" | "tiers";

export default function TuningPage() {
  const [tuning, setTuning] = useState<Tuning | null>(null);
  const [tab, setTab] = useState<Tab>("settings");
  const [status, setStatus] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    fetch("/api/tuning")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).error || `error ${r.status}`);
        return r.json();
      })
      .then((t) => {
        setTuning(t);
        setStatus("ready");
      })
      .catch((e) => {
        setStatus("error");
        setMsg(e.message);
      });
  }, []);

  async function save() {
    if (!tuning) return;
    setStatus("saving");
    setMsg("");
    try {
      const res = await fetch("/api/tuning", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(tuning),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `error ${res.status}`);
      setStatus("ready");
      setMsg("Saved. Next cron run will pick it up.");
    } catch (e: any) {
      setStatus("error");
      setMsg(e.message);
    }
  }

  if (status === "loading") return <Layout><p className="text-sm text-gray-500">Loading tuning.xlsx…</p></Layout>;
  if (status === "error" && !tuning) {
    return (
      <Layout>
        <div className="p-4 bg-red-50 text-red-700 text-sm rounded">{msg}</div>
      </Layout>
    );
  }
  if (!tuning) return null;

  const update = <K extends keyof Tuning>(key: K, value: Tuning[K]) =>
    setTuning({ ...tuning, [key]: value });

  return (
    <Layout>
      <div className="flex gap-1 border-b mb-6 -mt-2">
        {[
          ["settings", "Settings"],
          ["boosters", "Boosters"],
          ["buckets", "Priority Buckets"],
          ["tiers", "Source Tiers"],
        ].map(([id, label]) => (
          <button
            key={id}
            onClick={() => setTab(id as Tab)}
            className={`px-4 py-2 text-sm border-b-2 -mb-px ${
              tab === id
                ? "border-black font-medium"
                : "border-transparent text-gray-500 hover:text-gray-900"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "settings" && (
        <SettingsTable rows={tuning.settings} onChange={(r) => update("settings", r)} />
      )}
      {tab === "boosters" && (
        <BoostersTable rows={tuning.boosters} onChange={(r) => update("boosters", r)} />
      )}
      {tab === "buckets" && (
        <BucketsTable
          rows={tuning.priorityBuckets}
          onChange={(r) => update("priorityBuckets", r)}
        />
      )}
      {tab === "tiers" && (
        <TiersTable rows={tuning.sourceTiers} onChange={(r) => update("sourceTiers", r)} />
      )}

      <div className="mt-8 flex items-center gap-4 sticky bottom-4">
        <button
          onClick={save}
          disabled={status === "saving"}
          className="bg-black text-white rounded px-6 py-2 text-sm font-medium hover:bg-gray-800 disabled:opacity-50 shadow"
        >
          {status === "saving" ? "Saving…" : "Save changes"}
        </button>
        {msg && (
          <span
            className={`text-sm ${
              status === "error" ? "text-red-600" : "text-green-700"
            }`}
          >
            {msg}
          </span>
        )}
      </div>
    </Layout>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link href="/" className="text-sm text-gray-600 hover:text-gray-900">
            ← Back
          </Link>
          <h1 className="text-lg font-semibold">Tuning</h1>
          <div className="w-12" />
        </div>
      </header>
      <main className="max-w-5xl mx-auto px-6 py-8 pb-32">{children}</main>
    </div>
  );
}

function SettingsTable({
  rows, onChange,
}: { rows: SettingRow[]; onChange: (r: SettingRow[]) => void }) {
  const update = (i: number, patch: Partial<SettingRow>) => {
    const next = [...rows];
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  return (
    <div className="bg-white border rounded overflow-hidden">
      <div className="grid grid-cols-[1fr_140px_2fr] gap-3 px-4 py-2 border-b bg-gray-50 text-xs font-medium text-gray-600 uppercase">
        <div>Name</div>
        <div>Value</div>
        <div>Description</div>
      </div>
      {rows.map((r, i) => (
        <div
          key={i}
          className="grid grid-cols-[1fr_140px_2fr] gap-3 px-4 py-2 border-b items-center text-sm"
        >
          <div className="font-mono text-xs">{r.name}</div>
          <input
            type="text"
            value={r.value == null ? "" : String(r.value)}
            onChange={(e) => {
              const v = e.target.value;
              const n = Number(v);
              update(i, { value: v !== "" && !Number.isNaN(n) ? n : v });
            }}
            className="border rounded px-2 py-1 text-sm"
          />
          <div className="text-xs text-gray-600">{r.description}</div>
        </div>
      ))}
    </div>
  );
}

function BoostersTable({
  rows, onChange,
}: { rows: BoosterRow[]; onChange: (r: BoosterRow[]) => void }) {
  const update = (i: number, patch: Partial<BoosterRow>) => {
    const next = [...rows];
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  return (
    <div className="bg-white border rounded overflow-hidden">
      <div className="grid grid-cols-[160px_90px_1fr_1.4fr] gap-3 px-4 py-2 border-b bg-gray-50 text-xs font-medium text-gray-600 uppercase">
        <div>Name</div>
        <div>Weight</div>
        <div>Regex (case-insensitive)</div>
        <div>Description</div>
      </div>
      {rows.map((r, i) => (
        <div
          key={i}
          className="grid grid-cols-[160px_90px_1fr_1.4fr] gap-3 px-4 py-2 border-b items-start text-sm"
        >
          <div className="font-mono text-xs pt-2">{r.name}</div>
          <input
            type="number"
            step="0.01"
            value={r.weight}
            onChange={(e) => update(i, { weight: parseFloat(e.target.value) || 0 })}
            className="border rounded px-2 py-1 text-sm"
          />
          <input
            type="text"
            value={r.pattern_regex}
            onChange={(e) => update(i, { pattern_regex: e.target.value })}
            placeholder={
              ["tier1_voice", "trusted_publication", "firm_mention"].includes(r.name)
                ? "(matched by name, leave blank)"
                : ""
            }
            className="border rounded px-2 py-1 text-xs font-mono"
            disabled={["tier1_voice", "trusted_publication", "firm_mention"].includes(r.name)}
          />
          <div className="text-xs text-gray-600 pt-1">{r.description}</div>
        </div>
      ))}
    </div>
  );
}

function BucketsTable({
  rows, onChange,
}: { rows: PriorityBucketRow[]; onChange: (r: PriorityBucketRow[]) => void }) {
  const update = (i: number, patch: Partial<PriorityBucketRow>) => {
    const next = [...rows];
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  return (
    <div className="bg-white border rounded overflow-hidden">
      <div className="grid grid-cols-[150px_200px_1fr_120px] gap-3 px-4 py-2 border-b bg-gray-50 text-xs font-medium text-gray-600 uppercase">
        <div>Key</div>
        <div>Display</div>
        <div>Sub-buckets (; separated)</div>
        <div>Geos (; separated)</div>
      </div>
      {rows.map((r, i) => (
        <div
          key={i}
          className="grid grid-cols-[150px_200px_1fr_120px] gap-3 px-4 py-2 border-b items-center text-sm"
        >
          <input
            type="text"
            value={r.key}
            onChange={(e) => update(i, { key: e.target.value })}
            className="border rounded px-2 py-1 text-xs font-mono"
          />
          <input
            type="text"
            value={r.display}
            onChange={(e) => update(i, { display: e.target.value })}
            className="border rounded px-2 py-1 text-sm"
          />
          <input
            type="text"
            value={r.sub_buckets}
            onChange={(e) => update(i, { sub_buckets: e.target.value })}
            className="border rounded px-2 py-1 text-xs"
          />
          <input
            type="text"
            value={r.geos}
            onChange={(e) => update(i, { geos: e.target.value })}
            className="border rounded px-2 py-1 text-xs"
            placeholder="India; US"
          />
        </div>
      ))}
      <p className="text-xs text-gray-500 px-4 py-3">
        Allowed geos: India, US, Global. Separate multiple with <code>;</code>.
      </p>
    </div>
  );
}

function TiersTable({
  rows, onChange,
}: { rows: SourceTierRow[]; onChange: (r: SourceTierRow[]) => void }) {
  const update = (i: number, host: string) => {
    const next = [...rows];
    next[i] = { host };
    onChange(next);
  };
  const remove = (i: number) => {
    onChange(rows.filter((_, j) => j !== i));
  };
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= rows.length) return;
    const next = [...rows];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };
  const add = () => onChange([...rows, { host: "" }]);

  return (
    <div className="bg-white border rounded overflow-hidden">
      <div className="grid grid-cols-[40px_1fr_160px] gap-3 px-4 py-2 border-b bg-gray-50 text-xs font-medium text-gray-600 uppercase">
        <div>#</div>
        <div>Host</div>
        <div>Order / actions</div>
      </div>
      {rows.map((r, i) => (
        <div
          key={i}
          className="grid grid-cols-[40px_1fr_160px] gap-3 px-4 py-1.5 border-b items-center text-sm"
        >
          <div className="text-xs text-gray-500">{i + 1}</div>
          <input
            type="text"
            value={r.host}
            onChange={(e) => update(i, e.target.value)}
            className="border rounded px-2 py-1 text-xs font-mono"
          />
          <div className="flex gap-1 text-xs">
            <button
              onClick={() => move(i, -1)}
              className="border rounded px-2 py-1 hover:bg-gray-50"
            >
              ↑
            </button>
            <button
              onClick={() => move(i, 1)}
              className="border rounded px-2 py-1 hover:bg-gray-50"
            >
              ↓
            </button>
            <button
              onClick={() => remove(i)}
              className="border rounded px-2 py-1 hover:bg-red-50 hover:text-red-700"
            >
              Remove
            </button>
          </div>
        </div>
      ))}
      <div className="px-4 py-3">
        <button
          onClick={add}
          className="text-sm border rounded px-3 py-1 hover:bg-gray-50"
        >
          + Add host
        </button>
        <p className="text-xs text-gray-500 mt-2">
          Order matters. When dedupe collapses URLs about the same story, the
          host appearing earliest in this list wins the canonical link.
        </p>
      </div>
    </div>
  );
}
