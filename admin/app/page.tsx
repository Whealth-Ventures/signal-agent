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
        <div className="grid md:grid-cols-3 gap-4">
          <Link
            href="/keywords"
            className="block p-6 bg-white border rounded-lg hover:border-gray-400"
          >
            <h2 className="font-semibold mb-1">Keywords</h2>
            <p className="text-sm text-gray-600">
              Edit the ~2,240 keywords (bucket, sub-bucket, geo) the agent
              clusters into each day’s Perplexity searches — its core input.
            </p>
          </Link>
          <Link
            href="/sources"
            className="block p-6 bg-white border rounded-lg hover:border-gray-400"
          >
            <h2 className="font-semibold mb-1">Sources</h2>
            <p className="text-sm text-gray-600">
              Add or remove the publications, voices, and firms the agent
              watches (e.g. ET, VCCircle). RSS feeds are auto-discovered.
            </p>
          </Link>
          <Link
            href="/content"
            className="block p-6 bg-white border rounded-lg hover:border-gray-400"
          >
            <h2 className="font-semibold mb-1">Content corpus</h2>
            <p className="text-sm text-gray-600">
              Add, edit, or remove the firm’s own published pieces — the taste
              profile the agent scores story relevance against.
            </p>
          </Link>
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
          <Link
            href="/suggestions"
            className="block p-6 bg-white border rounded-lg hover:border-gray-400"
          >
            <h2 className="font-semibold mb-1">Suggestions</h2>
            <p className="text-sm text-gray-600">
              Review proposed tuning changes derived from Slack reactions on
              recent digests. Accept to apply; reject to archive.
            </p>
          </Link>
        </div>

        <p className="mt-10 text-xs text-gray-500">
          Saved changes commit directly to{" "}
          <code>{process.env.GITHUB_OWNER || "signal-agent"}/
            {process.env.GITHUB_REPO || "signal-agent"}@
            {process.env.GITHUB_BRANCH || "main"}</code>.
          {" "}and, when auto-deploy is configured, kick a deploy so the change
          goes live within a few minutes (otherwise it applies on the next
          deploy).
        </p>
      </main>
    </div>
  );
}
