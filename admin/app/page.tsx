import Link from "next/link";
import { getSession } from "@/lib/auth";

export default async function Home() {
  const session = await getSession();
  return (
    <div className="min-h-screen">
      <header className="border-b bg-white">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold">Signal Agent — Admin</h1>
            <p className="text-xs text-gray-500">{session?.email}</p>
          </div>
          <form action="/api/auth/logout" method="POST">
            <button
              formMethod="post"
              type="submit"
              className="text-sm text-gray-600 hover:text-gray-900"
            >
              Sign out
            </button>
          </form>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-10">
        <div className="grid md:grid-cols-2 gap-4">
          <Link
            href="/tuning"
            className="block p-6 bg-white border rounded-lg hover:border-gray-400"
          >
            <h2 className="font-semibold mb-1">Tuning</h2>
            <p className="text-sm text-gray-600">
              Edit every numeric knob: thresholds, dedup window, booster
              weights, priority categories, source tier list.
            </p>
          </Link>
          <Link
            href="/prompts"
            className="block p-6 bg-white border rounded-lg hover:border-gray-400"
          >
            <h2 className="font-semibold mb-1">Prompts</h2>
            <p className="text-sm text-gray-600">
              Edit the two LLM prompts: ranker tone &amp; magnitude rubric (what
              counts as Tier S / A / B / C).
            </p>
          </Link>
        </div>

        <p className="mt-10 text-xs text-gray-500">
          Saved changes commit directly to{" "}
          <code>{process.env.GITHUB_OWNER || "signal-agent"}/
            {process.env.GITHUB_REPO || "signal-agent"}@
            {process.env.GITHUB_BRANCH || "main"}</code>.
          The next cron run (10am IST) will pick them up.
        </p>
      </main>
    </div>
  );
}
