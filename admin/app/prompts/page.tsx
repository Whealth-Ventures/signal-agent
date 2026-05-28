"use client";
import { useEffect, useState } from "react";
import Link from "next/link";

type Prompts = { ranker_system: string; magnitude_rubric: string };

export default function PromptsPage() {
  const [prompts, setPrompts] = useState<Prompts | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    fetch("/api/prompts")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).error || `error ${r.status}`);
        return r.json();
      })
      .then((p) => {
        setPrompts(p);
        setStatus("ready");
      })
      .catch((e) => {
        setStatus("error");
        setMsg(e.message);
      });
  }, []);

  async function save() {
    if (!prompts) return;
    setStatus("saving");
    setMsg("");
    try {
      const res = await fetch("/api/prompts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(prompts),
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

  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link href="/" className="text-sm text-gray-600 hover:text-gray-900">
            ← Back
          </Link>
          <h1 className="text-lg font-semibold">Prompts</h1>
          <div className="w-12" />
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8 pb-32">
        {status === "loading" && (
          <p className="text-sm text-gray-500">Loading prompts…</p>
        )}
        {status === "error" && !prompts && (
          <div className="p-4 bg-red-50 text-red-700 text-sm rounded">{msg}</div>
        )}
        {prompts && (
          <div className="space-y-8">
            <PromptEditor
              title="ranker_system.md"
              subtitle="System message sent to the ranking model. Controls tone, framing, and how strictly the model interprets the magnitude rubric."
              value={prompts.ranker_system}
              onChange={(v) => setPrompts({ ...prompts, ranker_system: v })}
            />
            <PromptEditor
              title="magnitude_rubric.md"
              subtitle="The S/A/B/C tier definitions. What counts as biggest news vs. noteworthy vs. skip. Highest-leverage lever."
              value={prompts.magnitude_rubric}
              onChange={(v) => setPrompts({ ...prompts, magnitude_rubric: v })}
            />

            <div className="flex items-center gap-4 sticky bottom-4">
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
          </div>
        )}
      </main>
    </div>
  );
}

function PromptEditor({
  title, subtitle, value, onChange,
}: {
  title: string;
  subtitle: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="bg-white border rounded overflow-hidden">
      <div className="px-4 py-3 border-b bg-gray-50">
        <div className="font-mono text-sm">{title}</div>
        <div className="text-xs text-gray-600 mt-0.5">{subtitle}</div>
      </div>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={20}
        className="w-full px-4 py-3 text-sm font-mono leading-relaxed resize-y outline-none"
      />
    </div>
  );
}
