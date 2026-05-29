"use client";
import { useEffect, useState } from "react";
import Link from "next/link";

type Proposal = {
  id: string;
  type: string;
  target: { sheet: string; row_name: string; column: string };
  current: number;
  proposed: number;
  rationale: string;
  evidence: Record<string, unknown>;
};

type ProposalsFile = {
  generated_at: string | null;
  window_days: number;
  scored_digest_count: number;
  upvoted_digest_count: number;
  downvoted_digest_count: number;
  proposals: Proposal[];
};

export default function SuggestionsPage() {
  const [data, setData] = useState<ProposalsFile | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error" | "acting">("loading");
  const [msg, setMsg] = useState("");

  function load() {
    setStatus("loading");
    fetch("/api/suggestions")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).error || `error ${r.status}`);
        return r.json();
      })
      .then((d) => { setData(d); setStatus("ready"); })
      .catch((e) => { setStatus("error"); setMsg(e.message); });
  }
  useEffect(load, []);

  async function act(p: Proposal, action: "accept" | "reject") {
    const reason = action === "reject" ? (prompt("Why reject? (optional)") || "") : "";
    setStatus("acting"); setMsg("");
    try {
      const res = await fetch("/api/suggestions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: p.id, action, reason }),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `error ${res.status}`);
      setMsg(
        action === "accept"
          ? `Applied: ${p.target.row_name} ${p.current} → ${p.proposed}`
          : `Rejected ${p.id}`,
      );
      load();
    } catch (e: any) {
      setStatus("error"); setMsg(e.message);
    }
  }

  if (status === "loading") {
    return <Layout><p className="text-sm text-gray-500">Loading suggestions…</p></Layout>;
  }
  if (status === "error" && !data) {
    return <Layout><div className="p-4 bg-red-50 text-red-700 text-sm rounded">{msg}</div></Layout>;
  }
  if (!data) return null;

  const noData = data.generated_at === null;
  return (
    <Layout>
      {noData ? (
        <div className="p-6 bg-white border rounded text-sm text-gray-600">
          No <code>proposals/pending.json</code> in the repo yet. Run{" "}
          <code>python src/feedback_aggregator.py</code> on the agent machine,
          then <code>git push</code>.
        </div>
      ) : (
        <>
          <div className="mb-6 p-4 bg-gray-50 border rounded text-sm">
            <div className="text-gray-500 mb-1">
              Generated {new Date(data.generated_at!).toLocaleString()}{" "}
              · window: last {data.window_days} days
            </div>
            <div>
              <span className="font-medium">{data.scored_digest_count}</span>{" "}
              digests scored ·{" "}
              <span className="text-green-700">{data.upvoted_digest_count} upvoted</span>{" "}
              ·{" "}
              <span className="text-red-700">{data.downvoted_digest_count} downvoted</span>
            </div>
          </div>

          {msg && (
            <div className={`mb-4 p-3 text-sm rounded ${
              status === "error" ? "bg-red-50 text-red-700" : "bg-green-50 text-green-800"
            }`}>{msg}</div>
          )}

          {data.proposals.length === 0 ? (
            <div className="p-6 bg-white border rounded text-sm text-gray-600">
              No pending proposals. The aggregator needs at least 3 upvoted and 3
              downvoted digests with measurable divergence before it suggests
              changes.
            </div>
          ) : (
            <ul className="space-y-3">
              {data.proposals.map((p) => (
                <li key={p.id} className="p-4 bg-white border rounded">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1">
                      <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">
                        {p.type}
                      </div>
                      <div className="font-medium mb-1">
                        {p.target.sheet} · {p.target.row_name} · {p.target.column}
                      </div>
                      <div className="text-sm">
                        <span className="text-gray-500">{p.current}</span>{" "}
                        →{" "}
                        <span className="font-semibold">{p.proposed}</span>
                      </div>
                      <div className="text-sm text-gray-700 mt-2">{p.rationale}</div>
                    </div>
                    <div className="flex gap-2 shrink-0">
                      <button
                        disabled={status === "acting"}
                        onClick={() => act(p, "accept")}
                        className="px-3 py-1.5 text-sm bg-black text-white rounded hover:opacity-90 disabled:opacity-50"
                      >Accept</button>
                      <button
                        disabled={status === "acting"}
                        onClick={() => act(p, "reject")}
                        className="px-3 py-1.5 text-sm border rounded hover:bg-gray-50 disabled:opacity-50"
                      >Reject</button>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Layout>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold">Suggestions</h1>
            <p className="text-xs text-gray-500">Feedback-derived tuning proposals</p>
          </div>
          <Link href="/" className="text-sm text-gray-600 hover:text-gray-900">← Home</Link>
        </div>
      </header>
      <main className="max-w-5xl mx-auto px-6 py-10">{children}</main>
    </div>
  );
}
